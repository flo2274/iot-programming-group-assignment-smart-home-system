# edge_device_2.py (Refactored for better architecture and functionality with English comments)

import serial
import time
import json
import threading
import paho.mqtt.client as mqtt
import os # For the dummy API trigger file check

# --- Configuration ---
SERIAL_PORT_ARDUINO2 = '/dev/ttyACM0'  # Your correct RPi port for Arduino 2
BAUD_RATE = 9600

# Edge-to-Edge MQTT Broker
MQTT_BROKER_HOST = "test.mosquitto.org"
MQTT_BROKER_PORT = 1883
MQTT_CLIENT_ID_EDGE2 = "edge2-smart-home-inputs-22312" # Unique ID for this device

# MQTT Topics
TOPIC_PREFIX = "iot_project/groupXY" # Common prefix for your project (change groupXY)
TOPIC_EDGE2_A2_INPUTS_PUB = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE2}/arduino2/inputs" # For publishing IR/Button data
TOPIC_EDGE2_A2_CMD_SUB = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE2}/arduino2/cmd"  # For subscribing to Buzzer commands

# ThingsBoard Configuration
THINGSBOARD_HOST = "mqtt.thingsboard.cloud" # Or your instance (ensure this is correct)
THINGSBOARD_PORT = 1883
THINGSBOARD_ACCESS_TOKEN_EDGE2 = "rj6t94nd52Gk2vFrXXDO" # Your valid token for Edge 2's device

# --- State variables ---
actuator_states_a2 = { # For actuators on Arduino 2
    "buzzer_status": False # True if ON, False if OFF
}

# --- Global Variables for Connections & Data ---
arduino2_ser = None
edge_mqtt_client = None
tb_mqtt_client = None
serial_lock_a2 = threading.Lock()

# Not strictly needed as global cache if data is processed immediately, but can be useful for debugging
latest_arduino2_input_data = {
    "ir_code": None, "button_state": None, "error": None, "api_alarm_triggered": False
}

DUMMY_API_TRIGGER_FILE = "trigger_buzzer_api.flag" # Dummy file for API trigger

# --- Helper function to send actuator status to ThingsBoard ---
def publish_buzzer_status_to_thingsboard(is_on_state):
    if tb_mqtt_client and tb_mqtt_client.is_connected():
        payload = {'buzzer_status': is_on_state}
        print(f"THINGSBOARD STATUS UPDATE (Edge2 - Buzzer): {payload}")
        tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(payload), qos=1)

# --- MQTT Callbacks (for Edge-to-Edge MQTT) ---
def on_connect_edge_mqtt(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"EDGE MQTT: Connected to {MQTT_BROKER_HOST} as {MQTT_CLIENT_ID_EDGE2}!")
        client.subscribe(TOPIC_EDGE2_A2_CMD_SUB) # Commands for this device's Arduino (Buzzer)
        print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE2_A2_CMD_SUB}")
    else:
        print(f"EDGE MQTT: Failed to connect, return code {rc}")

def on_message_edge_mqtt(client, userdata, msg):
    # global actuator_states_a2 # Modifying elements
    payload_str = msg.payload.decode('utf-8')
    # print(f"EDGE MQTT Received on Edge2: Topic: {msg.topic}, Payload: {payload_str}")
    try:
        data = json.loads(payload_str)
        if msg.topic == TOPIC_EDGE2_A2_CMD_SUB:
            if "actuator" in data and data["actuator"].upper() == "BUZZER":
                new_state_str = str(data.get("value", "")).upper()
                new_buzzer_on_state = (new_state_str == "ON")

                if new_state_str == "BEEP": # Handle BEEP command
                    duration = data.get("duration", 500) # Default 500ms
                    print(f"EDGE MQTT CMD for A2: BUZZER BEEP for {duration}ms")
                    send_command_to_arduino2(f"BUZZER_BEEP:{duration}")
                    # For a beep, we might not change the persistent 'buzzer_status'
                    # or we might briefly set it true then false. For now, no persistent state change for beep.
                elif actuator_states_a2['buzzer_status'] != new_buzzer_on_state:
                    print(f"EDGE MQTT CMD for A2: BUZZER {'ON' if new_buzzer_on_state else 'OFF'}")
                    send_command_to_arduino2(f"BUZZER:{'ON' if new_buzzer_on_state else 'OFF'}")
                    actuator_states_a2['buzzer_status'] = new_buzzer_on_state
                    publish_buzzer_status_to_thingsboard(new_buzzer_on_state)
            elif "raw_command" in data: # Less preferred
                 print(f"EDGE MQTT RAW CMD for A2: {data['raw_command']}")
                 send_command_to_arduino2(data["raw_command"])
                 # Difficult to update 'buzzer_status' from raw command
    except json.JSONDecodeError:
        print(f"EDGE MQTT: Error decoding JSON on Edge2: {payload_str}")
    except Exception as e:
        print(f"EDGE MQTT: Error processing message on Edge2: {e} on topic {msg.topic}")

# --- ThingsBoard MQTT Callbacks ---
def on_connect_tb(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"THINGSBOARD: Connected to {THINGSBOARD_HOST} (Edge2)!")
        client.subscribe("v1/devices/me/rpc/request/+")
        print("THINGSBOARD: Subscribed to RPC requests (Edge2).")
    else:
        print(f"THINGSBOARD: Failed to connect (Edge2), return code {rc}")

def on_message_tb(client, userdata, msg):
    # global actuator_states_a2 # Modifying elements
    print(f"THINGSBOARD RPC Received (Edge2): Topic: {msg.topic}, Payload: {msg.payload.decode()}")
    request_id = msg.topic.split('/')[-1]
    response_payload = {"status": "OK"}
    telemetry_update = {} # For status updates

    try:
        data = json.loads(msg.payload)
        method = data.get("method")
        params = data.get("params")

        if method == "setBuzzerState":
            new_state = bool(params)
            if actuator_states_a2['buzzer_status'] != new_state:
                send_command_to_arduino2(f"BUZZER:{'ON' if new_state else 'OFF'}")
                actuator_states_a2['buzzer_status'] = new_state
                telemetry_update['buzzer_status'] = new_state
            response_payload["buzzer_state_set_to"] = new_state
        elif method == "beepBuzzer":
            try:
                duration = int(params)
                send_command_to_arduino2(f"BUZZER_BEEP:{duration}")
                response_payload["buzzer_beeped_for_ms"] = duration
                # A beep doesn't usually change the persistent 'buzzer_status'
                # but you could send a temporary status if desired.
            except ValueError:
                 response_payload = {"status": "ERROR", "error": "Invalid duration for beep"}
        else:
            response_payload = {"status": "ERROR", "error": f"Unknown RPC method: {method}"}

        if telemetry_update: # If a persistent state changed
            publish_buzzer_status_to_thingsboard(actuator_states_a2['buzzer_status'])


    except Exception as e:
        print(f"THINGSBOARD RPC (Edge2): Error processing message: {e}")
        response_payload = {"status": "ERROR", "error": str(e)}

    if tb_mqtt_client and tb_mqtt_client.is_connected():
        tb_mqtt_client.publish(f"v1/devices/me/rpc/response/{request_id}", json.dumps(response_payload), qos=1)

# --- Arduino 2 Serial Communication ---
def connect_to_arduino2():
    global arduino2_ser
    try:
        if arduino2_ser and arduino2_ser.is_open: return True
        print(f"SERIAL A2: Attempting to connect to Arduino 2 on {SERIAL_PORT_ARDUINO2}...")
        if arduino2_ser: arduino2_ser.close()
        temp_ser = serial.Serial(SERIAL_PORT_ARDUINO2, BAUD_RATE, timeout=1)
        print(f"SERIAL A2: Successfully connected to Arduino 2 on {SERIAL_PORT_ARDUINO2}")
        time.sleep(2); temp_ser.flushInput(); arduino2_ser = temp_ser; return True
    except serial.SerialException as e:
        print(f"SERIAL A2: Failed to connect: {e}."); arduino2_ser = None; return False
    except Exception as ex:
        print(f"SERIAL A2: Unexpected error during connection: {ex}"); arduino2_ser = None; return False

def send_command_to_arduino2(command):
    if arduino2_ser and arduino2_ser.is_open:
        with serial_lock_a2:
            try:
                # print(f"SERIAL A2: Sending: {command}")
                arduino2_ser.write((command + '\n').encode('utf-8'))
            except serial.SerialException as e: print(f"SERIAL A2: Error writing: {e}. Conn lost?.");
            except Exception as e: print(f"SERIAL A2: Unexpected error writing: {e}")
    else:
        print(f"SERIAL A2: Not connected. Cannot send command: {command}")

def read_from_arduino2_thread_func():
    global latest_arduino2_input_data, arduino2_ser
    last_ir_code_sent_to_edge = None # Send IR code to Edge1 only on change
    last_button_state_sent_to_edge = None # Send button state to Edge1 only on change

    while True:
        if not (arduino2_ser and arduino2_ser.is_open):
            if not connect_to_arduino2(): time.sleep(5); continue
        try:
            if arduino2_ser.in_waiting > 0:
                line = "";
                with serial_lock_a2: line = arduino2_ser.readline().decode('utf-8').rstrip()
                if line:
                    try:
                        data = json.loads(line)
                        latest_arduino2_input_data.update(data) # Update local cache

                        tb_payload = {}     # Data for this device's (Edge2) ThingsBoard telemetry
                        edge_payload = {}   # Data to be sent to Edge1 via MQTT

                        if "ir_code" in data and data["ir_code"]:
                            tb_payload["ir_code_received"] = data["ir_code"] # Log all received IR to TB
                            if data["ir_code"] != last_ir_code_sent_to_edge:
                                edge_payload["ir_code"] = data["ir_code"]
                                last_ir_code_sent_to_edge = data["ir_code"]

                        if "button_state" in data:
                            tb_payload["button_state"] = data["button_state"] # Log all button states to TB
                            if data["button_state"] != last_button_state_sent_to_edge:
                                edge_payload["button_state"] = data["button_state"]
                                last_button_state_sent_to_edge = data["button_state"]
                        
                        if "error" in data: # Log Arduino errors to TB
                            tb_payload["arduino2_error"] = data["error"]

                        # Publish to Edge MQTT (for Edge1 to consume) if there's new data
                        if edge_payload and edge_mqtt_client and edge_mqtt_client.is_connected():
                            edge_mqtt_client.publish(TOPIC_EDGE2_A2_INPUTS_PUB, json.dumps(edge_payload), qos=1)
                        
                        # Publish to ThingsBoard (this device's own telemetry) if there's data
                        if tb_payload and tb_mqtt_client and tb_mqtt_client.is_connected():
                            tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(tb_payload), qos=1)

                    except json.JSONDecodeError: print(f"SERIAL A2: JSON Decode Error: {line}"); latest_arduino2_input_data["error"] = "JSON Decode Error"
                    except Exception as e_json: print(f"SERIAL A2: Error processing JSON ({line}): {e_json}"); latest_arduino2_input_data["error"] = f"Proc Error: {e_json}"
        except serial.SerialException as e: print(f"SERIAL A2: Serial error during read: {e}. Disconnecting."); arduino2_ser.close(); arduino2_ser = None; continue
        except AttributeError as ae: print(f"SERIAL A2: AttributeError (arduino2_ser None?): {ae}. Re-evaluating."); arduino2_ser = None; continue
        except Exception as e_read: print(f"SERIAL A2: Unexpected read error: {e_read}"); arduino2_ser.close(); arduino2_ser = None; time.sleep(1); continue
        time.sleep(0.1)


# --- API Integration Placeholder & Buzzer Control ---
def check_for_api_alarm_trigger():
    if os.path.exists(DUMMY_API_TRIGGER_FILE):
        try:
            os.remove(DUMMY_API_TRIGGER_FILE)
            print("API DUMMY: Trigger file found and removed.")
            return True
        except OSError as e:
            print(f"API DUMMY: Error removing trigger file: {e}")
            return False
    return False

def api_alarm_thread_func():
    # global actuator_states_a2 # Modifying elements
    print("API Alarm Thread started. To trigger dummy API, create a file named 'trigger_buzzer_api.flag'")
    while True:
        if check_for_api_alarm_trigger():
            print("API RULE: Dummy Alarm condition met. Triggering BUZZER BEEP on A2 for 1s.")
            send_command_to_arduino2("BUZZER_BEEP:1000") # Beep for 1 second
            # A beep is momentary, so we don't set actuator_states_a2['buzzer_status'] to True permanently.
            # If the API call should turn the buzzer ON until explicitly turned OFF:
            # if not actuator_states_a2['buzzer_status']:
            #     send_command_to_arduino2("BUZZER:ON")
            #     actuator_states_a2['buzzer_status'] = True
            #     publish_buzzer_status_to_thingsboard(True)
            #
            # Also send a telemetry point that the API alarm was triggered
            if tb_mqtt_client and tb_mqtt_client.is_connected():
                 tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps({"api_alarm_event": True, "timestamp": time.time()}), qos=1)

        time.sleep(3) # Check for API trigger every 3 seconds


if __name__ == "__main__":
    print("Starting Edge Device 2 (Refactored)...")

    # Edge-to-Edge MQTT Client
    edge_mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID_EDGE2)
    edge_mqtt_client.on_connect = on_connect_edge_mqtt
    edge_mqtt_client.on_message = on_message_edge_mqtt
    try:
        print(f"EDGE MQTT: Connecting to {MQTT_BROKER_HOST} as {MQTT_CLIENT_ID_EDGE2}")
        edge_mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        edge_mqtt_client.loop_start()
    except Exception as e: print(f"EDGE MQTT: Connection failed: {e}")

    # ThingsBoard MQTT Client
    tb_mqtt_client_id = f"tb_edge2_{MQTT_CLIENT_ID_EDGE2}"
    tb_mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=tb_mqtt_client_id)
    tb_mqtt_client.username_pw_set(THINGSBOARD_ACCESS_TOKEN_EDGE2)
    tb_mqtt_client.on_connect = on_connect_tb
    tb_mqtt_client.on_message = on_message_tb
    try:
        print(f"THINGSBOARD: Connecting to {THINGSBOARD_HOST} (token: {THINGSBOARD_ACCESS_TOKEN_EDGE2[:5]}...)")
        tb_mqtt_client.connect(THINGSBOARD_HOST, THINGSBOARD_PORT, 60)
        tb_mqtt_client.loop_start()
    except Exception as e: print(f"THINGSBOARD: Connection failed: {e}")

    # Start threads
    arduino_reader_thread = threading.Thread(target=read_from_arduino2_thread_func, daemon=True, name="Arduino2ReaderThread")
    api_thread = threading.Thread(target=api_alarm_thread_func, daemon=True, name="ApiAlarmThread")
    arduino_reader_thread.start()
    api_thread.start()

    print(f"Edge Device 2 (Refactored) now running.")
    print(f"Listening for Arduino 2 on {SERIAL_PORT_ARDUINO2}")
    print(f"To trigger dummy API alarm for buzzer, create file: {DUMMY_API_TRIGGER_FILE} in the script's directory.")
    print("Ctrl+C to exit.")

    try:
        while True: time.sleep(10)
    except KeyboardInterrupt: print("\nExiting Edge Device 2...")
    finally:
        print("Cleaning up Edge Device 2 resources...")
        if arduino2_ser and arduino2_ser.is_open: print("Closing Arduino 2 serial port."); arduino2_ser.close()
        if edge_mqtt_client and edge_mqtt_client.is_connected(): print("Stopping Edge MQTT."); edge_mqtt_client.loop_stop(); edge_mqtt_client.disconnect()
        if tb_mqtt_client and tb_mqtt_client.is_connected(): print("Stopping ThingsBoard MQTT."); tb_mqtt_client.loop_stop(); tb_mqtt_client.disconnect()
        print("Edge Device 2 stopped.")