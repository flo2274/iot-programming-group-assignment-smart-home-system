# edge_device_2_mqtt_tb_rules.py
import serial
import time
import json
import threading
import paho.mqtt.client as mqtt

# --- Configuration (SAME AS BEFORE - ensure these are correct) ---
SERIAL_PORT_ARDUINO2 = '/dev/ttyACM0'  # <<<<<<< YOUR RASPBERRY PI ARDUINO2 PORT
BAUD_RATE = 9600

MQTT_BROKER_HOST = "test.mosquitto.org"
MQTT_BROKER_PORT = 1883
MQTT_CLIENT_ID_EDGE2 = "edge2-223151653621" # UNIQUE (different from Edge 1)
TOPIC_EDGE2_DATA_A2 = f"iot_project/groupXY/{MQTT_CLIENT_ID_EDGE2}/arduino2/data"
TOPIC_EDGE2_CMD_A2 = f"iot_project/groupXY/{MQTT_CLIENT_ID_EDGE2}/arduino2/cmd"
# TOPIC_EDGE1_CMD_A1 is not directly used for publishing from here now, Edge1 subscribes to TOPIC_EDGE2_DATA_A2 for IR/Button

THINGSBOARD_HOST = "demo.thingsboard.io"
THINGSBOARD_PORT = 1883
THINGSBOARD_ACCESS_TOKEN_EDGE2 = "rj6t94nd52Gk2vFrXXDO" # FROM THINGSBOARD

# --- Global Variables (Mostly same as before) ---
arduino2_ser = None
mqtt_client_edge2 = None
tb_client_edge2 = None
latest_arduino2_data = {
    "ir_code": None, "button_state": None, "error": None
}
serial_lock_a2 = threading.Lock()

# --- MQTT Callbacks (for Edge-to-Edge MQTT) ---
# on_connect_mqtt_edge, on_message_mqtt_edge (largely same, handles commands for A2)
def on_connect_mqtt_edge(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"EDGE MQTT: Connected to {MQTT_BROKER_HOST}!")
        client.subscribe(TOPIC_EDGE2_CMD_A2) # Commands for Arduino 2
        print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE2_CMD_A2}")
    else:
        print(f"EDGE MQTT: Failed to connect, return code {rc}")

def on_message_mqtt_edge(client, userdata, msg):
    payload_str = msg.payload.decode('utf-8')
    # print(f"EDGE MQTT Received: Topic: {msg.topic}, Payload: {payload_str}") # Can be noisy
    try:
        data = json.loads(payload_str)
        if msg.topic == TOPIC_EDGE2_CMD_A2: # Commands for Arduino 2
            handle_arduino2_command_from_mqtt(data)
    except json.JSONDecodeError:
        print(f"EDGE MQTT: Error decoding JSON: {payload_str}")
    except Exception as e:
        print(f"EDGE MQTT: Error processing message: {e} on topic {msg.topic}")

def handle_arduino2_command_from_mqtt(command_data): # Same as before
    """ Handles commands received over MQTT intended for Arduino 2 """
    if "actuator" in command_data and "value" in command_data:
        actuator = command_data["actuator"]
        value = command_data["value"]
        if actuator == "BUZZER":
            send_command_to_arduino2(f"BUZZER:{value}")
        elif actuator == "BUZZER_BEEP":
            send_command_to_arduino2(f"BUZZER_BEEP:{value}")
    elif "raw_command" in command_data:
        send_command_to_arduino2(command_data["raw_command"])

# --- ThingsBoard MQTT Callbacks (on_connect_tb, on_message_tb) ---
# These can remain largely the same as before, handling RPC from ThingsBoard for Buzzer.
def on_connect_tb(client, userdata, flags, rc, properties=None): # Same
    if rc == 0:
        print(f"THINGSBOARD: Connected to {THINGSBOARD_HOST}!")
        client.subscribe("v1/devices/me/rpc/request/+")
        print("THINGSBOARD: Subscribed to RPC requests.")
    else:
        print(f"THINGSBOARD: Failed to connect, return code {rc}")

def on_message_tb(client, userdata, msg): # Same
    print(f"THINGSBOARD RPC Received: Topic: {msg.topic}, Payload: {msg.payload.decode()}")
    try:
        data = json.loads(msg.payload)
        method = data.get("method")
        params = data.get("params")
        request_id = msg.topic.split('/')[-1]
        response = {"status": "ERROR", "error": "Unknown method"}

        if method == "setBuzzerState":
            new_state = bool(params)
            send_command_to_arduino2(f"BUZZER:{'ON' if new_state else 'OFF'}")
            response = {"status": "OK", "buzzer_state_set_to": new_state}
        elif method == "beepBuzzer":
            duration = int(params)
            send_command_to_arduino2(f"BUZZER_BEEP:{duration}")
            response = {"status": "OK", "buzzer_beeped_for_ms": duration}

        tb_client_edge2.publish(f"v1/devices/me/rpc/response/{request_id}", json.dumps(response), qos=1)
    except Exception as e:
        print(f"THINGSBOARD RPC: Error processing message: {e}")
        if 'request_id' in locals():
            tb_client_edge2.publish(f"v1/devices/me/rpc/response/{request_id}", json.dumps({"status": "ERROR", "error": str(e)}), qos=1)


# --- Arduino 2 Serial Communication (connect_to_arduino2, send_command_to_arduino2, read_from_arduino2_thread_func) ---
# These functions remain largely the same, ensuring data is published to MQTT and ThingsBoard.
def connect_to_arduino2(): # Same
    global arduino2_ser
    while True:
        try:
            arduino2_ser = serial.Serial(SERIAL_PORT_ARDUINO2, BAUD_RATE, timeout=1)
            print(f"SERIAL A2: Connected to Arduino 2 on {SERIAL_PORT_ARDUINO2}")
            time.sleep(2)
            arduino2_ser.flushInput()
            return
        except serial.SerialException as e:
            print(f"SERIAL A2: Failed to connect: {e}. Retrying in 5s...")
            arduino2_ser = None
            time.sleep(5)

def send_command_to_arduino2(command): # Same
    if arduino2_ser and arduino2_ser.is_open:
        with serial_lock_a2:
            try:
                # print(f"SERIAL A2: Sending: {command}") # Can be noisy
                arduino2_ser.write((command + '\n').encode('utf-8'))
            except Exception as e:
                print(f"SERIAL A2: Error writing: {e}")
    else:
        print("SERIAL A2: Not connected.")

def read_from_arduino2_thread_func(): # Same
    global latest_arduino2_data
    connect_to_arduino2()
    while True:
        if arduino2_ser and arduino2_ser.is_open:
            try:
                if arduino2_ser.in_waiting > 0:
                    line = ""
                    with serial_lock_a2:
                        line = arduino2_ser.readline().decode('utf-8').rstrip()
                    if line:
                        try:
                            data = json.loads(line)
                            latest_arduino2_data.update(data)

                            # Publish to Edge MQTT (for Edge 1 to consume)
                            if mqtt_client_edge2 and "error" not in data:
                                mqtt_client_edge2.publish(TOPIC_EDGE2_DATA_A2, json.dumps(data), qos=1)

                            # Publish to ThingsBoard
                            if tb_client_edge2 and "error" not in data:
                                tb_client_edge2.publish("v1/devices/me/telemetry", json.dumps(data), qos=1)

                        except json.JSONDecodeError:
                            print(f"SERIAL A2: JSON Decode Error: {line}")
                        except Exception as e_json:
                            print(f"SERIAL A2: Error processing JSON: {e_json}")
            except serial.SerialException:
                print("SERIAL A2: Error. Reconnecting...")
                if arduino2_ser: arduino2_ser.close()
                arduino2_ser = None
                connect_to_arduino2()
            except Exception as e_read:
                print(f"SERIAL A2: Unexpected read error: {e_read}")
                time.sleep(1)
        else:
            connect_to_arduino2()
        time.sleep(0.1)


# --- API Integration Placeholder & Buzzer Control ---
def check_for_api_alarm_trigger():
    """
    Placeholder function.
    In a real scenario, this would check an API (e.g., Discord bot new message flag,
    a message queue, or poll an API endpoint).
    Returns True if an alarm condition from API is met.
    """
    # SIMULATE API TRIGGER - Replace with actual API check
    # For testing, you could make this return True after some time or based on a file flag.
    # Example: if os.path.exists("trigger_alarm.flag"): return True
    return False

def api_alarm_thread_func():
    """ Periodically checks the API and triggers buzzer if needed. """
    while True:
        if check_for_api_alarm_trigger():
            print("API RULE: Alarm condition met. Triggering BUZZER on A2.")
            send_command_to_arduino2("BUZZER:ON") # Or BUZZER_BEEP:1000 for a timed beep
            # Potentially send an MQTT message to acknowledge or log this
            # mqtt_client_edge2.publish(TOPIC_EDGE2_DATA_A2, json.dumps({"api_alarm_triggered": True}))

            # Reset the trigger or wait before re-triggering
            # if os.path.exists("trigger_alarm.flag"): os.remove("trigger_alarm.flag")
            time.sleep(10) # Don't re-trigger immediately
        else:
            # Optional: send BUZZER:OFF if the API condition clears,
            # but this might conflict with other buzzer uses (like high temp alarm).
            # Careful design needed if buzzer has multiple triggers.
            pass
        time.sleep(5) # Check API every 5 seconds


# --- Main Execution ---
if __name__ == "__main__":
    # Initialize Edge-to-Edge MQTT Client
    mqtt_client_edge2 = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID_EDGE2)
    mqtt_client_edge2.on_connect = on_connect_mqtt_edge
    mqtt_client_edge2.on_message = on_message_mqtt_edge
    try:
        print(f"EDGE MQTT: Attempting to connect to {MQTT_BROKER_HOST} as {MQTT_CLIENT_ID_EDGE2}")
        mqtt_client_edge2.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        mqtt_client_edge2.loop_start()
    except Exception as e:
        print(f"EDGE MQTT: Connection failed: {e}")

    # Initialize ThingsBoard MQTT Client
    tb_client_edge2 = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tb_client_" + MQTT_CLIENT_ID_EDGE2)
    tb_client_edge2.username_pw_set(THINGSBOARD_ACCESS_TOKEN_EDGE2)
    tb_client_edge2.on_connect = on_connect_tb
    tb_client_edge2.on_message = on_message_tb
    try:
        print(f"THINGSBOARD: Attempting to connect to {THINGSBOARD_HOST} with token {THINGSBOARD_ACCESS_TOKEN_EDGE2[:5]}...")
        tb_client_edge2.connect(THINGSBOARD_HOST, THINGSBOARD_PORT, 60)
        tb_client_edge2.loop_start()
    except Exception as e:
        print(f"THINGSBOARD: Connection failed: {e}")

    # Start threads
    threading.Thread(target=read_from_arduino2_thread_func, daemon=True).start()
    threading.Thread(target=api_alarm_thread_func, daemon=True).start() # Thread for API-triggered alarm

    print("Edge Device 2 (Rules Placeholder for API) started.")
    print(f"Listening for Arduino 2 on {SERIAL_PORT_ARDUINO2}")
    print(f"Connecting to MQTT broker {MQTT_BROKER_HOST} with client ID {MQTT_CLIENT_ID_EDGE2}")
    print(f"Connecting to ThingsBoard {THINGSBOARD_HOST}")
    print("Ctrl+C to exit.")
    try:
        while True:
            # Optional: print status
            # print(f"A2: {latest_arduino2_data}")
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nExiting Edge Device 2...")
    finally:
        if arduino2_ser and arduino2_ser.is_open:
            # send_command_to_arduino2("BUZZER:OFF") # Optional cleanup
            arduino2_ser.close()
        if mqtt_client_edge2:
            mqtt_client_edge2.loop_stop()
            mqtt_client_edge2.disconnect()
        if tb_client_edge2:
            tb_client_edge2.loop_stop()
            tb_client_edge2.disconnect()
        print("Edge Device 2 stopped.")