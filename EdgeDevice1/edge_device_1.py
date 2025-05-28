# edge_device_1.py (Refactored for better architecture, functionality, DB, and Presence Awareness)

import serial
import time
import json
import threading
import paho.mqtt.client as mqtt
import database_utils # Import your new database utility module

# --- Configuration ---
SERIAL_PORT_ARDUINO1 = '/dev/tty.usbmodem11301' # YOUR CORRECT PORT for Arduino 1
BAUD_RATE = 9600

# Edge-to-Edge MQTT Broker
MQTT_BROKER_HOST = "test.mosquitto.org"
MQTT_BROKER_PORT = 1883
MQTT_CLIENT_ID_EDGE1 = "edge1-smart-home-controller-22312" # Unique ID for this device
# Ensure the following client ID matches Edge Device 2's actual client ID!
MQTT_CLIENT_ID_EDGE2_EXPECTED = "edge2-smart-home-calendar-22312" # Updated to match Edge2's presence client ID

# MQTT Topics
TOPIC_PREFIX = "iot_project/groupXY" # Common prefix for your project (change groupXY)
TOPIC_EDGE1_A1_SENSORS = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE1}/arduino1/sensors"
TOPIC_EDGE1_A1_ACTUATOR_STATUS = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE1}/arduino1/actuator_status"
TOPIC_EDGE1_A1_CMD = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE1}/arduino1/cmd"
TOPIC_EDGE2_A2_INPUTS = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE2_EXPECTED}/arduino2/inputs" # For IR/Button from A2
# NEW: Subscribe to presence status from Edge Device 2 (calendar based)
TOPIC_PRESENCE_STATUS_SUB = f"{TOPIC_PREFIX}/home/calendar_presence"

# NEW: Topics for interaction with EdgeDevice2 regarding Arduino2 events/commands
# EdgeDevice1 subscribes to these topics from EdgeDevice2
TOPIC_EDGE2_EXTERNAL_LED_TOGGLE_SUB = f"{TOPIC_PREFIX}/edge2/external_led/toggle_request" # From EdgeDevice2
TOPIC_EDGE2_ARDUINO2_IR_EVENT_SUB = f"{TOPIC_PREFIX}/edge2/arduino2/ir_event"       # From EdgeDevice2

# EdgeDevice1 publishes to this topic to command Arduino2 (via EdgeDevice2)
# This MUST match TOPIC_ARDUINO2_CMD_SUB in EdgeDevice2
MQTT_CLIENT_ID_EDGE2_ACTUAL = "edge2-gcal-simplified" # Actual Client ID of EdgeDevice2
TOPIC_EDGE1_TO_ARDUINO2_CMD_PUB = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE2_ACTUAL}/arduino2/cmd"

# ThingsBoard Configuration
THINGSBOARD_HOST = "mqtt.thingsboard.cloud" # Or your instance
THINGSBOARD_PORT = 1883
THINGSBOARD_ACCESS_TOKEN_EDGE1 = "OkJ7XmIBLCRcpDcDJhJq" # Your valid token

# --- Rule Thresholds ---
TEMP_THRESHOLD_FAN_ON = 24.0
TEMP_THRESHOLD_FAN_OFF = 24.0 # Corrected: was 23.0, changed to 24.0 for consistency with FAN_ON
HUMIDITY_THRESHOLD_WINDOW_OPEN = 65.0
HUMIDITY_THRESHOLD_WINDOW_CLOSE = 60.0
LIGHT_THRESHOLD_LOW = 50
LIGHT_THRESHOLD_HIGH = 60

# --- State variables for actuators on Arduino 1 ---
actuator_states = {
    "fan_status": False,    # True if ON, False if OFF
    "window_status": False, # True if OPEN, False if CLOSED (meaning servo at 90 or 0)
    "light_status": False   # True if ON, False if OFF
}

# --- Global Variables for Connections & Data ---
arduino1_ser = None
edge_mqtt_client = None
tb_mqtt_client = None
serial_lock_a1 = threading.Lock() # Lock for serial port access

latest_arduino1_sensor_data = { # Cache for sensor data from Arduino 1
    "temperature": None, "humidity": None, "light": None, "error": None
}

# --- Presence State Variable ---
person_is_at_home = True # Default to True, updated by MQTT messages from Edge 2
person_is_at_home_lock = threading.Lock()

# --- Interval for database insertion ---
DB_INSERT_INTERVAL = 60 # Seconds (e.g., insert data every 1 minute)
last_db_insert_time = 0

# --- LED Override State ---
led_override_active = False
led_override_end_time = 0
led_override_lock = threading.Lock() # To protect these shared variables


# --- Helper function to send actuator status to ThingsBoard AND Edge MQTT ---
def publish_actuator_status(updated_statuses):
    """
    Publishes the new state of actuators to ThingsBoard telemetry
    and also to a general Edge MQTT topic for other devices if needed.
    'updated_statuses' is a dictionary like {'light_status': True}.
    """
    if not updated_statuses: # Nothing to send
        return

    # Send to ThingsBoard
    if tb_mqtt_client and tb_mqtt_client.is_connected():
        # print(f"THINGSBOARD STATUS UPDATE (Edge1 Actuators): {updated_statuses}")
        tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(updated_statuses), qos=1)

    # Send to general Edge MQTT actuator status topic (optional, for other edge devices to know status)
    if edge_mqtt_client and edge_mqtt_client.is_connected():
        # print(f"EDGE MQTT ACTUATOR STATUS (Edge1): {updated_statuses}") # Can be noisy
        edge_mqtt_client.publish(TOPIC_EDGE1_A1_ACTUATOR_STATUS, json.dumps(updated_statuses), qos=1)


# --- MQTT Callbacks (for Edge-to-Edge MQTT) ---
def on_connect_edge_mqtt(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"EDGE MQTT: Connected to {MQTT_BROKER_HOST} as {MQTT_CLIENT_ID_EDGE1}!")
        client.subscribe(TOPIC_EDGE1_A1_CMD) # Commands for this device's Arduino
        print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE1_A1_CMD}")
        # client.subscribe(TOPIC_EDGE2_A2_INPUTS) # May not be needed if specific topics are used
        # print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE2_A2_INPUTS}")
        # NEW: Subscribe to presence status
        client.subscribe(TOPIC_PRESENCE_STATUS_SUB)
        print(f"EDGE MQTT: Subscribed to {TOPIC_PRESENCE_STATUS_SUB}")
        # NEW: Subscribe to events from EdgeDevice2
        client.subscribe(TOPIC_EDGE2_EXTERNAL_LED_TOGGLE_SUB)
        print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE2_EXTERNAL_LED_TOGGLE_SUB}")
        client.subscribe(TOPIC_EDGE2_ARDUINO2_IR_EVENT_SUB)
        print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE2_ARDUINO2_IR_EVENT_SUB}")
    else:
        print(f"EDGE MQTT: Failed to connect, return code {rc}")

def on_message_edge_mqtt(client, userdata, msg):
    global person_is_at_home # Declare global to modify it
    global led_override_active, led_override_end_time # Globals for LED override

    payload_str = msg.payload.decode('utf-8')
    print(f"EDGE MQTT RX (Edge1): Topic: {msg.topic}, Payload: {payload_str}") # Enhanced log

    try:
        data = json.loads(payload_str)
        if msg.topic == TOPIC_EDGE1_A1_CMD:
            handle_arduino1_command_from_mqtt(data) # Manual commands for A1 actuators
        
        elif msg.topic == TOPIC_PRESENCE_STATUS_SUB:
            print(f"PRESENCE MQTT (Edge1): Received presence status update: {payload_str}")
            if "person_at_home" in data:
                with person_is_at_home_lock:
                    new_status = bool(data["person_at_home"])
                    if person_is_at_home != new_status:
                        person_is_at_home = new_status
                        print(f"PRESENCE UPDATE (Edge1): Person at home status changed to: {person_is_at_home}")
                        # Optionally, publish this received status to Edge1's ThingsBoard device
                        if tb_mqtt_client and tb_mqtt_client.is_connected():
                            tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps({"system_person_at_home": person_is_at_home}), qos=1)
            else:
                print(f"PRESENCE MQTT (Edge1): Received message on {TOPIC_PRESENCE_STATUS_SUB} without 'person_at_home' key: {data}")

        elif msg.topic == TOPIC_EDGE2_EXTERNAL_LED_TOGGLE_SUB:
            print(f"EDGE MQTT (Edge1): Received request to toggle external LED (Arduino1 LED) from EdgeDevice2.")
            # Assuming the 'light_status' refers to the LED on Arduino1 that Edge1 controls
            current_a1_light_state = actuator_states.get('light_status', False)
            new_a1_light_state = not current_a1_light_state
            cmd_to_a1 = f"LED:{'ON' if new_a1_light_state else 'OFF'}"
            
            print(f"EDGE MQTT (Edge1): Toggling Arduino1 LED. Command: {cmd_to_a1}. Activating 1-min sensor override.")
            send_command_to_arduino1(cmd_to_a1)
            actuator_states['light_status'] = new_a1_light_state
            publish_actuator_status({'light_status': new_a1_light_state})
            if tb_mqtt_client and tb_mqtt_client.is_connected():
                 tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps({"arduino1_led_toggled_by_a2_button": new_a1_light_state}), qos=1)

            # Activate 1-minute override for sensor-based control
            with led_override_lock:
                led_override_active = True
                led_override_end_time = time.time() + 60 # 60 seconds

        # NEW: Handle IR Events from Arduino2 (relayed by EdgeDevice2)
        elif msg.topic == TOPIC_EDGE2_ARDUINO2_IR_EVENT_SUB:
            ir_event_type = data.get("ir_event_type")
            print(f"EDGE MQTT (Edge1): Received IR event from Arduino2 (via Edge2): {ir_event_type}")
            
            tb_payload_a2_buzzer = {}
            mqtt_cmd_for_a2_buzzer = None

            if ir_event_type == "ALARM_ON":
                print("EDGE MQTT (Edge1): Processing ALARM_ON for Arduino2's buzzer.")
                mqtt_cmd_for_a2_buzzer = {"actuator": "BUZZER", "value": "ON"}
                tb_payload_a2_buzzer = {"arduino2_buzzer_commanded_state": 1, "arduino2_buzzer_last_ir_trigger": "ON"}
            elif ir_event_type == "ALARM_OFF" or ir_event_type == "ALARM_OFF_OLD":
                print("EDGE MQTT (Edge1): Processing ALARM_OFF for Arduino2's buzzer.")
                mqtt_cmd_for_a2_buzzer = {"actuator": "BUZZER", "value": "OFF"}
                tb_payload_a2_buzzer = {"arduino2_buzzer_commanded_state": 0, "arduino2_buzzer_last_ir_trigger": "OFF"}
            else:
                print(f"EDGE MQTT (Edge1): Unknown ir_event_type received: {ir_event_type}")

            if mqtt_cmd_for_a2_buzzer and edge_mqtt_client and edge_mqtt_client.is_connected():
                try:
                    edge_mqtt_client.publish(TOPIC_EDGE1_TO_ARDUINO2_CMD_PUB, json.dumps(mqtt_cmd_for_a2_buzzer), qos=1)
                    print(f"EDGE MQTT (Edge1): Published command to {TOPIC_EDGE1_TO_ARDUINO2_CMD_PUB} for Arduino2 buzzer: {mqtt_cmd_for_a2_buzzer}")
                except Exception as e_pub_a2_cmd:
                    print(f"EDGE MQTT ERROR (Edge1): Failed to publish command for Arduino2 buzzer: {e_pub_a2_cmd}")
            
            if tb_payload_a2_buzzer and tb_mqtt_client and tb_mqtt_client.is_connected():
                tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(tb_payload_a2_buzzer), qos=1)
                print(f"THINGSBOARD (Edge1): Sent telemetry for Arduino2 buzzer: {tb_payload_a2_buzzer}")


    except json.JSONDecodeError:
        print(f"EDGE MQTT: Error decoding JSON: {payload_str} on topic {msg.topic}")
    except Exception as e:
        print(f"EDGE MQTT: Error processing message: {e} on topic {msg.topic}")

def handle_arduino1_command_from_mqtt(command_data):
    global led_override_active # Access to potentially modify override (e.g., manual cmd cancels it)
    telemetry_update = {}
    action_taken = False

    if "actuator" in command_data and "value" in command_data:
        actuator = command_data["actuator"].upper()
        value_str = str(command_data["value"])

        # print(f"MQTT CMD for A1: Actuator: {actuator}, Value: {value_str}")

        if actuator == "LED":
            # Optional: Decide if manual MQTT command should cancel the sensor override
            # with led_override_lock:
            #     if led_override_active:
            #         print("MQTT CMD (Edge1): Manual LED command received, cancelling sensor override.")
            #         led_override_active = False
            new_state = (value_str.upper() == "ON")
            if actuator_states['light_status'] != new_state:
                send_command_to_arduino1(f"LED:{'ON' if new_state else 'OFF'}")
                actuator_states['light_status'] = new_state
                telemetry_update['light_status'] = new_state
                action_taken = True
        elif actuator == "WINDOW":
            try:
                if value_str.upper() == "OPEN": angle = 90
                elif value_str.upper() == "CLOSE": angle = 0
                else: angle = int(value_str)
                new_window_open_state = (angle > 0)
                print(f"MQTT CMD for A1: Setting WINDOW to {angle}")
                send_command_to_arduino1(f"WINDOW:{angle}")
                if actuator_states['window_status'] != new_window_open_state:
                    actuator_states['window_status'] = new_window_open_state
                    telemetry_update['window_status'] = new_window_open_state
                elif 'window_status' not in telemetry_update :
                     telemetry_update['window_status'] = new_window_open_state
                action_taken = True
            except ValueError: print(f"MQTT CMD Error: Invalid window angle/command '{value_str}'")
        elif actuator == "FAN":
            new_state = (value_str.upper() == "ON")
            if actuator_states['fan_status'] != new_state:
                if new_state:
                    send_command_to_arduino1("FAN_ON")
                else:
                    send_command_to_arduino1("FAN_OFF")
                actuator_states['fan_status'] = new_state
                telemetry_update['fan_status'] = new_state
                action_taken = True
    elif "raw_command" in command_data:
        print(f"MQTT RAW CMD for A1: {command_data['raw_command']}")
        send_command_to_arduino1(command_data["raw_command"])
        action_taken = True # Raw commands might not have direct telemetry mapping

    if action_taken and telemetry_update:
        publish_actuator_status(telemetry_update)


def process_ir_for_arduino1(ir_code_hex):
    # This function is not called in the provided snippet, but if it were,
    # and if IR could control the LED, similar logic for override might be needed or
    # IR commands could also cancel the override. For now, assuming it's not interacting
    # with the LED override logic.
    telemetry_update = {}
    action_taken = False
    code = ir_code_hex.upper()

    if code == "0XFF629D": # Example: Toggle Light
        new_light_state = not actuator_states['light_status']
        cmd = f"LED:{'ON' if new_light_state else 'OFF'}"
        print(f"IR Action: {cmd} on Arduino 1")
        send_command_to_arduino1(cmd)
        actuator_states['light_status'] = new_light_state
        telemetry_update['light_status'] = new_light_state
        action_taken = True
        # Optional: IR command could also cancel the sensor override
        # with led_override_lock:
        #     if led_override_active:
        #         print("IR Action (Edge1): IR LED command received, cancelling sensor override.")
        #         led_override_active = False
    elif code == "0XFFA25D": # Example: Toggle Fan
        new_fan_state = not actuator_states['fan_status']
        if new_fan_state:
            print("IR Action: Fan ON (speed 10) on Arduino 1")
            send_command_to_arduino1("FAN_SPEED:10"); send_command_to_arduino1("FAN_STEPS:200")
        else:
            print("IR Action: Fan OFF on Arduino 1")
            send_command_to_arduino1("FAN_OFF")
        actuator_states['fan_status'] = new_fan_state
        telemetry_update['fan_status'] = new_fan_state
        action_taken = True
    else:
        print(f"IR code {code} not mapped to an action for Arduino 1.")

    if action_taken and telemetry_update:
        publish_actuator_status(telemetry_update)

# --- ThingsBoard MQTT Callbacks ---
def on_connect_tb(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"THINGSBOARD: Connected to {THINGSBOARD_HOST} (Edge1)!")
        client.subscribe("v1/devices/me/rpc/request/+")
        print("THINGSBOARD: Subscribed to RPC requests (Edge1).")
    else:
        print(f"THINGSBOARD: Failed to connect (Edge1), return code {rc}")

def on_message_tb(client, userdata, msg):
    global led_override_active # Access to potentially modify override
    request_id = msg.topic.split('/')[-1]
    telemetry_update = {}
    response_payload = {"status": "OK"}

    try:
        data = json.loads(msg.payload)
        method = data.get("method")
        params = data.get("params")

        if method == "setLedState":
            # Optional: Decide if RPC command should cancel the sensor override
            # with led_override_lock:
            #     if led_override_active:
            #         print("RPC (Edge1): Manual LED command received, cancelling sensor override.")
            #         led_override_active = False
            new_state = bool(params)
            if actuator_states['light_status'] != new_state:
                send_command_to_arduino1(f"LED:{'ON' if new_state else 'OFF'}")
                actuator_states['light_status'] = new_state
                telemetry_update['light_status'] = new_state
            response_payload["led_state_set_to"] = new_state
        elif method == "setWindowAngle":
            try:
                angle = 0
                if isinstance(params, bool): angle = 90 if params else 0
                elif isinstance(params, (int, float)): angle = int(params)
                elif isinstance(params, str) and params.upper() == "OPEN": angle = 90
                elif isinstance(params, str) and params.upper() == "CLOSE": angle = 0
                else: raise ValueError("Invalid parameter type for setWindowAngle/State")
                new_window_open_state = (angle > 0)
                print(f"RPC for A1: Setting WINDOW to {angle}")
                send_command_to_arduino1(f"WINDOW:{angle}")
                if actuator_states['window_status'] != new_window_open_state:
                    actuator_states['window_status'] = new_window_open_state
                    telemetry_update['window_status'] = new_window_open_state
                elif 'window_status' not in telemetry_update:
                     telemetry_update['window_status'] = new_window_open_state
                response_payload["window_angle_set_to"] = angle
            except ValueError as e:
                response_payload = {"status": "ERROR", "error": f"Invalid angle/state parameter: {e}"}
        elif method == "setFanState":
            new_fan_state = bool(params)
            if actuator_states['fan_status'] != new_fan_state:
                if new_fan_state:
                    send_command_to_arduino1("FAN_SPEED:10"); send_command_to_arduino1("FAN_STEPS:100")
                else: send_command_to_arduino1("FAN_OFF")
                actuator_states['fan_status'] = new_fan_state
                telemetry_update['fan_status'] = new_fan_state
            response_payload["fan_state_set_to"] = new_fan_state
        else:
            response_payload = {"status": "ERROR", "error": f"Unknown RPC method: {method}"}

        if telemetry_update:
            publish_actuator_status(telemetry_update)

    except Exception as e:
        print(f"THINGSBOARD RPC (Edge1): Error processing message: {e}")
        response_payload = {"status": "ERROR", "error": str(e)}

    if tb_mqtt_client and tb_mqtt_client.is_connected():
        tb_mqtt_client.publish(f"v1/devices/me/rpc/response/{request_id}", json.dumps(response_payload), qos=1)

# --- Arduino 1 Serial Communication ---
def connect_to_arduino1():
    global arduino1_ser
    try:
        if arduino1_ser and arduino1_ser.is_open: return True
        print(f"SERIAL A1: Attempting to connect to Arduino 1 on {SERIAL_PORT_ARDUINO1}...")
        if arduino1_ser: arduino1_ser.close()
        arduino1_ser = serial.Serial(SERIAL_PORT_ARDUINO1, BAUD_RATE, timeout=1)
        print(f"SERIAL A1: Successfully connected to Arduino 1 on {SERIAL_PORT_ARDUINO1}")
        time.sleep(2); arduino1_ser.flushInput(); return True
    except serial.SerialException as e:
        print(f"SERIAL A1: Failed to connect: {e}."); arduino1_ser = None; return False
    except Exception as ex:
        print(f"SERIAL A1: Unexpected error during connection: {ex}"); arduino1_ser = None; return False

def send_command_to_arduino1(command):
    if arduino1_ser and arduino1_ser.is_open:
        with serial_lock_a1:
            try:
                print(f"SERIAL A1: Sending: {command}")
                arduino1_ser.write((command + '\n').encode('utf-8'))
            except serial.SerialException as e: print(f"SERIAL A1: Error writing: {e}. Conn lost?.");
            except Exception as ex: print(f"SERIAL A1: Unexpected error writing: {ex}")
    else:
        print(f"SERIAL A1: Not connected. Cannot send command: {command}")

def read_from_arduino1_thread_func():
    global latest_arduino1_sensor_data, arduino1_ser, last_db_insert_time
    while True:
        if not (arduino1_ser and arduino1_ser.is_open):
            if not connect_to_arduino1(): time.sleep(5); continue
        try:
            if arduino1_ser.in_waiting > 0:
                line = "";
                with serial_lock_a1: line = arduino1_ser.readline().decode('utf-8').rstrip()
                if line:
                    try:
                        data = json.loads(line); latest_arduino1_sensor_data.update(data)
                        if "error" not in data:
                            if edge_mqtt_client and edge_mqtt_client.is_connected():
                                edge_mqtt_client.publish(TOPIC_EDGE1_A1_SENSORS, json.dumps(data), qos=1)
                            if tb_mqtt_client and tb_mqtt_client.is_connected():
                                tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(data), qos=1)

                            current_time = time.time()
                            if (current_time - last_db_insert_time > DB_INSERT_INTERVAL):
                                temp = latest_arduino1_sensor_data.get("temperature")
                                hum = latest_arduino1_sensor_data.get("humidity")
                                light = latest_arduino1_sensor_data.get("light")
                                if None not in [temp, hum, light]:
                                    try:
                                        database_utils.insert_sensor_data(temp, hum, light)
                                        last_db_insert_time = current_time
                                    except Exception as db_e: print(f"DB WRITE: Failed: {db_e}")
                        else:
                            print(f"SERIAL A1: Received error from Arduino: {data.get('error')}")
                            latest_arduino1_sensor_data["error"] = data.get("error")
                    except json.JSONDecodeError: print(f"SERIAL A1: JSON Decode Error: {line}"); latest_arduino1_sensor_data["error"] = "JSON Decode Error"
                    except Exception as e_json: print(f"SERIAL A1: Error processing JSON ({line}): {e_json}"); latest_arduino1_sensor_data["error"] = f"Proc Error: {e_json}"
        except serial.SerialException as e: print(f"SERIAL A1: Serial error during read: {e}. Disconnecting."); arduino1_ser.close(); arduino1_ser = None; continue
        except AttributeError as ae: print(f"SERIAL A1: AttributeError (arduino1_ser None?): {ae}. Re-evaluating."); arduino1_ser = None; continue
        except Exception as e_read: print(f"SERIAL A1: Unexpected read error: {e_read}"); arduino1_ser.close(); arduino1_ser = None; time.sleep(1); continue
        time.sleep(0.1)

# --- Rules Engine Logic ---
def apply_rules_arduino1():
    global person_is_at_home, led_override_active, led_override_end_time # Ensure access to LED override state
    telemetry_update = {}

    temp = latest_arduino1_sensor_data.get("temperature")
    humidity = latest_arduino1_sensor_data.get("humidity")
    light_level = latest_arduino1_sensor_data.get("light")

    # Rule: Temperature for Fan
    if temp is not None:
        if temp > TEMP_THRESHOLD_FAN_ON and not actuator_states['fan_status']:
            print(f"RULE A1: Temp ({temp}°C) > {TEMP_THRESHOLD_FAN_ON}°C. Turning FAN ON.")
            send_command_to_arduino1("FAN_ON")
            actuator_states['fan_status'] = True; telemetry_update['fan_status'] = True
        elif temp <= TEMP_THRESHOLD_FAN_OFF and actuator_states['fan_status']: # Changed to <= for clarity
            print(f"RULE A1: Temp ({temp}°C) <= {TEMP_THRESHOLD_FAN_OFF}°C. Turning FAN OFF.")
            send_command_to_arduino1("FAN_OFF")
            actuator_states['fan_status'] = False; telemetry_update['fan_status'] = False

    # Rule: Humidity for Window (NOW WITH PRESENCE CHECK)
    if humidity is not None:
        with person_is_at_home_lock: local_person_is_at_home = person_is_at_home
        
        if humidity > HUMIDITY_THRESHOLD_WINDOW_OPEN and not actuator_states['window_status']:
            if local_person_is_at_home:
                print(f"RULE A1: Humidity ({humidity}%) > {HUMIDITY_THRESHOLD_WINDOW_OPEN}% AND Person AT HOME. Opening WINDOW.")
                send_command_to_arduino1("WINDOW:90")
                actuator_states['window_status'] = True; telemetry_update['window_status'] = True
            else:
                print(f"RULE A1: Humidity ({humidity}%) > {HUMIDITY_THRESHOLD_WINDOW_OPEN}%, BUT Person NOT AT HOME. Window remains closed.")
        elif humidity < HUMIDITY_THRESHOLD_WINDOW_CLOSE and actuator_states['window_status']:
            print(f"RULE A1: Humidity ({humidity}%) < {HUMIDITY_THRESHOLD_WINDOW_CLOSE}%. Closing WINDOW.")
            send_command_to_arduino1("WINDOW:0")
            actuator_states['window_status'] = False; telemetry_update['window_status'] = False

    # Rule: Light level for LED (WITH OVERRIDE CHECK)
    if light_level is not None:
        apply_led_sensor_rule = True # Flag to control if sensor rule should be applied

        with led_override_lock: # Thread-safe access to override state
            if led_override_active:
                if time.time() < led_override_end_time:
                    # Override is active and within the 1-minute window
                    remaining_time = led_override_end_time - time.time()
                    print(f"RULE A1: LED sensor override active for Arduino1 LED. Current state: {'ON' if actuator_states['light_status'] else 'OFF'}. Sensor rule for LED skipped. Override ends in {remaining_time:.0f}s.")
                    apply_led_sensor_rule = False # Do not apply sensor rule
                else:
                    # Override period has expired
                    print("RULE A1: LED sensor override period expired. Resuming sensor-based control for LED.")
                    led_override_active = False # Deactivate override
                    # apply_led_sensor_rule remains True, so sensor rule will apply now
        
        if apply_led_sensor_rule:
            if light_level < LIGHT_THRESHOLD_LOW and not actuator_states['light_status']:
                print(f"RULE A1: Light ({light_level}) < {LIGHT_THRESHOLD_LOW}. Turning LIGHT ON.")
                send_command_to_arduino1("LED:ON")
                actuator_states['light_status'] = True; telemetry_update['light_status'] = True
            elif light_level > LIGHT_THRESHOLD_HIGH and actuator_states['light_status']:
                print(f"RULE A1: Light ({light_level}) > {LIGHT_THRESHOLD_HIGH}. Turning LIGHT OFF.")
                send_command_to_arduino1("LED:OFF")
                actuator_states['light_status'] = False; telemetry_update['light_status'] = False

    if telemetry_update:
        publish_actuator_status(telemetry_update)


def rule_engine_thread_func():
    while True:
        apply_rules_arduino1()
        time.sleep(2) # Check automation rules every 2 seconds


if __name__ == "__main__":
    print("Starting Edge Device 1 (Controller with DB & Presence)...")

    print("Initializing database schema...")
    if database_utils.initialize_database_schema():
        print("Database schema initialization successful or table already exists.")
    else:
        print("CRITICAL: Database schema initialization failed.")
        # import sys; sys.exit("Exiting due to database initialization failure.")

    edge_mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID_EDGE1)
    edge_mqtt_client.on_connect = on_connect_edge_mqtt
    edge_mqtt_client.on_message = on_message_edge_mqtt
    try:
        print(f"EDGE MQTT: Connecting to {MQTT_BROKER_HOST} as {MQTT_CLIENT_ID_EDGE1}")
        edge_mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        edge_mqtt_client.loop_start()
    except Exception as e: print(f"EDGE MQTT: Connection failed during setup: {e}")

    tb_mqtt_client_id = f"tb_edge1_{MQTT_CLIENT_ID_EDGE1}" 
    tb_mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=tb_mqtt_client_id)
    tb_mqtt_client.username_pw_set(THINGSBOARD_ACCESS_TOKEN_EDGE1)
    tb_mqtt_client.on_connect = on_connect_tb
    tb_mqtt_client.on_message = on_message_tb
    try:
        print(f"THINGSBOARD: Connecting to {THINGSBOARD_HOST} (token: {THINGSBOARD_ACCESS_TOKEN_EDGE1[:5]}...)")
        tb_mqtt_client.connect(THINGSBOARD_HOST, THINGSBOARD_PORT, 60)
        tb_mqtt_client.loop_start()
    except Exception as e: print(f"THINGSBOARD: Connection failed during setup: {e}")

    arduino_reader_thread = threading.Thread(target=read_from_arduino1_thread_func, daemon=True, name="Arduino1ReaderThread")
    rules_thread = threading.Thread(target=rule_engine_thread_func, daemon=True, name="RuleEngineThread")
    arduino_reader_thread.start()
    rules_thread.start()

    print(f"Edge Device 1 (Controller with DB & Presence) now running.")
    print(f"Listening for Arduino 1 on {SERIAL_PORT_ARDUINO1}")
    print(f"Subscribing to presence status on MQTT topic: {TOPIC_PRESENCE_STATUS_SUB}")
    print("Ctrl+C to exit.")

    try:
        while True: time.sleep(10)
    except KeyboardInterrupt: print("\nExiting Edge Device 1...")
    finally:
        print("Cleaning up Edge Device 1 resources...")
        if arduino1_ser and arduino1_ser.is_open: print("Closing Arduino 1 serial port."); arduino1_ser.close()
        if edge_mqtt_client and edge_mqtt_client.is_connected(): print("Stopping Edge MQTT."); edge_mqtt_client.loop_stop(); edge_mqtt_client.disconnect()
        if tb_mqtt_client and tb_mqtt_client.is_connected(): print("Stopping ThingsBoard MQTT."); tb_mqtt_client.loop_stop(); tb_mqtt_client.disconnect()
        print("Edge Device 1 stopped.")