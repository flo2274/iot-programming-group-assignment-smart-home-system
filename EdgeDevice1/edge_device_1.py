# edge_device_1.py (Refactored for better architecture and functionality with English comments)

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
MQTT_CLIENT_ID_EDGE2_EXPECTED = "edge2-smart-home-inputs-22312" # Expected client ID from Edge 2

# MQTT Topics
TOPIC_PREFIX = "iot_project/groupXY" # Common prefix for your project (change groupXY)
TOPIC_EDGE1_A1_SENSORS = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE1}/arduino1/sensors"
TOPIC_EDGE1_A1_ACTUATOR_STATUS = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE1}/arduino1/actuator_status"
TOPIC_EDGE1_A1_CMD = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE1}/arduino1/cmd"
TOPIC_EDGE2_A2_INPUTS = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE2_EXPECTED}/arduino2/inputs"

# ThingsBoard Configuration
THINGSBOARD_HOST = "mqtt.thingsboard.cloud" # Or your instance
THINGSBOARD_PORT = 1883
THINGSBOARD_ACCESS_TOKEN_EDGE1 = "OkJ7XmIBLCRcpDcDJhJq" # Your valid token

# --- Rule Thresholds ---
TEMP_THRESHOLD_FAN_ON = 25.0
TEMP_THRESHOLD_FAN_OFF = 24.0
HUMIDITY_THRESHOLD_WINDOW_OPEN = 65.0
HUMIDITY_THRESHOLD_WINDOW_CLOSE = 60.0
LIGHT_THRESHOLD_LOW = 300
LIGHT_THRESHOLD_HIGH = 700

# --- State variables for actuators on Arduino 1 ---
actuator_states = {
    "fan_status": False,    # True if ON, False if OFF
    "window_status": False, # True if OPEN, False if CLOSED
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

# --- Interval for database insertion ---
DB_INSERT_INTERVAL = 60 # Seconds (e.g., insert data every 1 minute)
last_db_insert_time = 0


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
        print(f"THINGSBOARD STATUS UPDATE (Edge1 Actuators): {updated_statuses}")
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
        client.subscribe(TOPIC_EDGE2_A2_INPUTS) # Input data (IR/Button) from Edge 2
        print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE2_A2_INPUTS}")
    else:
        print(f"EDGE MQTT: Failed to connect, return code {rc}")

def on_message_edge_mqtt(client, userdata, msg):
    payload_str = msg.payload.decode('utf-8')
    try:
        data = json.loads(payload_str)
        if msg.topic == TOPIC_EDGE1_A1_CMD:
            handle_arduino1_command_from_mqtt(data) # Manual commands for A1 actuators
        elif msg.topic == TOPIC_EDGE2_A2_INPUTS:
            # Process IR code from Edge 2
            if "ir_code" in data and data["ir_code"]:
                process_ir_for_arduino1(data["ir_code"])

            # Process Button state from Edge 2
            if "button_state" in data and data["button_state"] == 1:
                # Act on press (rising edge).
                # Edge 2 should ideally only send on state change.
                print("Button on A2 pressed (received via MQTT), toggling Light on A1.")
                current_light_state = actuator_states.get('light_status', False)
                new_light_state = not current_light_state
                send_command_to_arduino1(f"LED:{'ON' if new_light_state else 'OFF'}")
                actuator_states['light_status'] = new_light_state
                publish_actuator_status({'light_status': new_light_state})
    except json.JSONDecodeError:
        print(f"EDGE MQTT: Error decoding JSON: {payload_str}")
    except Exception as e:
        print(f"EDGE MQTT: Error processing message: {e} on topic {msg.topic}")

def handle_arduino1_command_from_mqtt(command_data):
    telemetry_update = {}
    action_taken = False

    if "actuator" in command_data and "value" in command_data:
        actuator = command_data["actuator"].upper()
        value_str = str(command_data["value"])

        print(f"MQTT CMD for A1: Actuator: {actuator}, Value: {value_str}")

        if actuator == "LED":
            new_state = (value_str.upper() == "ON")
            if actuator_states['light_status'] != new_state:
                send_command_to_arduino1(f"LED:{'ON' if new_state else 'OFF'}")
                actuator_states['light_status'] = new_state
                telemetry_update['light_status'] = new_state
                action_taken = True
        elif actuator == "WINDOW":
            try:
                angle = int(value_str)
                new_window_open_state = (angle > 0)
                send_command_to_arduino1(f"WINDOW:{angle}") # Send command regardless to set specific angle
                if actuator_states['window_status'] != new_window_open_state:
                    actuator_states['window_status'] = new_window_open_state
                    telemetry_update['window_status'] = new_window_open_state
                elif not telemetry_update.get('window_status'): # if state same, still send if no other update
                     telemetry_update['window_status'] = new_window_open_state
                action_taken = True
            except ValueError: print(f"MQTT CMD Error: Invalid window angle '{value_str}'")
        elif actuator == "FAN":
            new_state = (value_str.upper() == "ON")
            if actuator_states['fan_status'] != new_state:
                if new_state:
                    send_command_to_arduino1("FAN_SPEED:10")
                    send_command_to_arduino1("FAN_STEPS:200")
                else:
                    send_command_to_arduino1("FAN_OFF")
                actuator_states['fan_status'] = new_state
                telemetry_update['fan_status'] = new_state
                action_taken = True
        # Add other actuators like FAN_SPEED, FAN_STEPS if needed

    elif "raw_command" in command_data:
        print(f"MQTT RAW CMD for A1: {command_data['raw_command']}")
        send_command_to_arduino1(command_data["raw_command"])
        action_taken = True
        # Difficult to update specific states from raw_command, consider sending all current states
        # telemetry_update.update(actuator_states)

    if action_taken and telemetry_update:
        publish_actuator_status(telemetry_update)


def process_ir_for_arduino1(ir_code_hex):
    telemetry_update = {}
    action_taken = False
    code = ir_code_hex.upper()
    print(f"Processing IR code {code} for Arduino 1 actuators...")

    # --- REPLACE THESE IR CODES WITH YOUR ACTUAL REMOTE'S CODES ---
    if code == "0XFF629D": # Example: Toggle Light (e.g., CH- button)
        new_light_state = not actuator_states['light_status']
        cmd = f"LED:{'ON' if new_light_state else 'OFF'}"
        print(f"IR Action: {cmd} on Arduino 1")
        send_command_to_arduino1(cmd)
        actuator_states['light_status'] = new_light_state
        telemetry_update['light_status'] = new_light_state
        action_taken = True
    elif code == "0XFFA25D": # Example: Toggle Fan (e.g., CH button)
        new_fan_state = not actuator_states['fan_status']
        if new_fan_state:
            print("IR Action: Fan ON (speed 10) on Arduino 1")
            send_command_to_arduino1("FAN_SPEED:10")
            send_command_to_arduino1("FAN_STEPS:200")
        else:
            print("IR Action: Fan OFF on Arduino 1")
            send_command_to_arduino1("FAN_OFF")
        actuator_states['fan_status'] = new_fan_state
        telemetry_update['fan_status'] = new_fan_state
        action_taken = True
    # Add more IR mappings if needed
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
    print(f"THINGSBOARD RPC Received (Edge1): Topic: {msg.topic}, Payload: {msg.payload.decode()}")
    request_id = msg.topic.split('/')[-1]
    telemetry_update = {}
    response_payload = {"status": "OK"}

    try:
        data = json.loads(msg.payload)
        method = data.get("method")
        params = data.get("params")

        if method == "setLedState":
            new_state = bool(params)
            if actuator_states['light_status'] != new_state:
                send_command_to_arduino1(f"LED:{'ON' if new_state else 'OFF'}")
                actuator_states['light_status'] = new_state
                telemetry_update['light_status'] = new_state
            response_payload["led_state_set_to"] = new_state
        elif method == "setWindowAngle":
            try:
                angle = int(params)
                new_window_open_state = (angle > 0)
                send_command_to_arduino1(f"WINDOW:{angle}")
                if actuator_states['window_status'] != new_window_open_state:
                    actuator_states['window_status'] = new_window_open_state
                    telemetry_update['window_status'] = new_window_open_state
                elif not telemetry_update.get('window_status'): # if state same, still send if no other update
                     telemetry_update['window_status'] = new_window_open_state
                response_payload["window_angle_set_to"] = angle
            except ValueError:
                response_payload = {"status": "ERROR", "error": "Invalid angle parameter"}
        elif method == "setFanState":
            new_fan_state = bool(params)
            if actuator_states['fan_status'] != new_fan_state:
                if new_fan_state:
                    send_command_to_arduino1("FAN_SPEED:10")
                    send_command_to_arduino1("FAN_STEPS:100")
                else:
                    send_command_to_arduino1("FAN_OFF")
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
        temp_ser = serial.Serial(SERIAL_PORT_ARDUINO1, BAUD_RATE, timeout=1)
        print(f"SERIAL A1: Successfully connected to Arduino 1 on {SERIAL_PORT_ARDUINO1}")
        time.sleep(2); temp_ser.flushInput(); arduino1_ser = temp_ser; return True
    except serial.SerialException as e:
        print(f"SERIAL A1: Failed to connect: {e}."); arduino1_ser = None; return False
    except Exception as ex:
        print(f"SERIAL A1: Unexpected error during connection: {ex}"); arduino1_ser = None; return False

def send_command_to_arduino1(command):
    if arduino1_ser and arduino1_ser.is_open:
        with serial_lock_a1:
            try:
                # print(f"SERIAL A1: Sending: {command}")
                arduino1_ser.write((command + '\n').encode('utf-8'))
            except serial.SerialException as e: print(f"SERIAL A1: Error writing: {e}. Conn lost?.");
            except Exception as e: print(f"SERIAL A1: Unexpected error writing: {e}")
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
                            # Publish raw sensor data to Edge MQTT
                            if edge_mqtt_client and edge_mqtt_client.is_connected():
                                edge_mqtt_client.publish(TOPIC_EDGE1_A1_SENSORS, json.dumps(data), qos=1)
                            # Publish raw sensor data to ThingsBoard
                            if tb_mqtt_client and tb_mqtt_client.is_connected():
                                tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(data), qos=1)

                            # --- DATABASE INSERTION LOGIC ---
                            current_time = time.time()
                            if (current_time - last_db_insert_time > DB_INSERT_INTERVAL):
                                temp = latest_arduino1_sensor_data.get("temperature")
                                hum = latest_arduino1_sensor_data.get("humidity")
                                light = latest_arduino1_sensor_data.get("light")
                                if None not in [temp, hum, light]:
                                    print(f"DB WRITE: Interval. Writing: T={temp}, H={hum}, L={light}")
                                    try:
                                        database_utils.insert_sensor_data(temp, hum, light)
                                        last_db_insert_time = current_time
                                    except Exception as db_e: print(f"DB WRITE: Failed: {db_e}")
                                else: print("DB WRITE: Skip due to missing sensor values.")
                        else: # Handle Arduino error string
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
    telemetry_update = {}

    temp = latest_arduino1_sensor_data.get("temperature")
    humidity = latest_arduino1_sensor_data.get("humidity")
    light_level = latest_arduino1_sensor_data.get("light")

    # Rule: Temperature for Fan
    if temp is not None:
        if temp > TEMP_THRESHOLD_FAN_ON and not actuator_states['fan_status']:
            print(f"RULE A1: Temp ({temp}°C) > {TEMP_THRESHOLD_FAN_ON}°C. Turning FAN ON.")
            send_command_to_arduino1("FAN_SPEED:10"); send_command_to_arduino1("FAN_STEPS:200")
            actuator_states['fan_status'] = True; telemetry_update['fan_status'] = True
        elif temp < TEMP_THRESHOLD_FAN_OFF and actuator_states['fan_status']:
            print(f"RULE A1: Temp ({temp}°C) < {TEMP_THRESHOLD_FAN_OFF}°C. Turning FAN OFF.")
            send_command_to_arduino1("FAN_OFF")
            actuator_states['fan_status'] = False; telemetry_update['fan_status'] = False

    # Rule: Humidity for Window
    if humidity is not None:
        if humidity > HUMIDITY_THRESHOLD_WINDOW_OPEN and not actuator_states['window_status']:
            print(f"RULE A1: Humidity ({humidity}%) > {HUMIDITY_THRESHOLD_WINDOW_OPEN}%. Opening WINDOW.")
            send_command_to_arduino1("WINDOW:90")
            actuator_states['window_status'] = True; telemetry_update['window_status'] = True
        elif humidity < HUMIDITY_THRESHOLD_WINDOW_CLOSE and actuator_states['window_status']:
            print(f"RULE A1: Humidity ({humidity}%) < {HUMIDITY_THRESHOLD_WINDOW_CLOSE}%. Closing WINDOW.")
            send_command_to_arduino1("WINDOW:0")
            actuator_states['window_status'] = False; telemetry_update['window_status'] = False

    # Rule: Light level for LED
    if light_level is not None:
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
    print("Starting Edge Device 1 (Refactored with DB)...")

    # ---- INITIALIZE DATABASE SCHEMA HERE ----
    print("Initializing database schema...")
    if database_utils.initialize_database_schema(): # Call the function from database_utils
        print("Database schema initialization successful or table already exists.")
    else:
        print("CRITICAL: Database schema initialization failed. Check DB connection and permissions.")
        # import sys
        # sys.exit("Exiting due to database initialization failure.") # Optional: exit if DB is critical
    # ---- END OF DATABASE INITIALIZATION ----

    # Edge-to-Edge MQTT Client
    edge_mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID_EDGE1)
    edge_mqtt_client.on_connect = on_connect_edge_mqtt
    edge_mqtt_client.on_message = on_message_edge_mqtt
    try:
        print(f"EDGE MQTT: Connecting to {MQTT_BROKER_HOST} as {MQTT_CLIENT_ID_EDGE1}")
        edge_mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        edge_mqtt_client.loop_start()
    except Exception as e: print(f"EDGE MQTT: Connection failed during setup: {e}")

    # ThingsBoard MQTT Client
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

    # Start threads
    arduino_reader_thread = threading.Thread(target=read_from_arduino1_thread_func, daemon=True, name="Arduino1ReaderThread")
    rules_thread = threading.Thread(target=rule_engine_thread_func, daemon=True, name="RuleEngineThread")
    arduino_reader_thread.start()
    rules_thread.start()

    print(f"Edge Device 1 (Refactored with DB) now running.")
    print(f"Listening for Arduino 1 on {SERIAL_PORT_ARDUINO1}")
    print(f"Database inserts will occur approx. every {DB_INSERT_INTERVAL} seconds for valid data.")
    print("Ctrl+C to exit.")

    try:
        while True: time.sleep(10) # Keep main thread alive
    except KeyboardInterrupt: print("\nExiting Edge Device 1...")
    finally:
        print("Cleaning up Edge Device 1 resources...")
        if arduino1_ser and arduino1_ser.is_open: print("Closing Arduino 1 serial port."); arduino1_ser.close()
        if edge_mqtt_client and edge_mqtt_client.is_connected(): print("Stopping Edge MQTT."); edge_mqtt_client.loop_stop(); edge_mqtt_client.disconnect()
        if tb_mqtt_client and tb_mqtt_client.is_connected(): print("Stopping ThingsBoard MQTT."); tb_mqtt_client.loop_stop(); tb_mqtt_client.disconnect()
        print("Edge Device 1 stopped.")