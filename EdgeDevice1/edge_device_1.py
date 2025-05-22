# edge_device_1.py (Implementiert Status-Telemetrie an ThingsBoard)

import serial
import time
import json
import threading
import paho.mqtt.client as mqtt

# --- Configuration ---
SERIAL_PORT_ARDUINO1 = '/dev/tty.usbmodem11301' # Ihr korrekter Port
BAUD_RATE = 9600
MQTT_BROKER_HOST = "test.mosquitto.org"
MQTT_BROKER_PORT = 1883
MQTT_CLIENT_ID_EDGE1 = "edge1-223151653621" # Muss einzigartig sein
TOPIC_EDGE1_DATA_A1 = f"iot_project/groupXY/{MQTT_CLIENT_ID_EDGE1}/arduino1/data"
TOPIC_EDGE1_CMD_A1 = f"iot_project/groupXY/{MQTT_CLIENT_ID_EDGE1}/arduino1/cmd"
# Stellen Sie sicher, dass 'edge2-223151653621' die tatsächliche Client-ID von Edge 2 ist!
TOPIC_EDGE2_DATA_A2 = f"iot_project/groupXY/edge2-223151653621/arduino2/data"

THINGSBOARD_HOST = "mqtt.thingsboard.cloud"
THINGSBOARD_PORT = 1883
THINGSBOARD_ACCESS_TOKEN_EDGE1 = "OkJ7XmIBLCRcpDcDJhJq" # Ihr Token

# --- Rule Thresholds & State ---
TEMP_THRESHOLD_FAN_ON = 25.0
TEMP_THRESHOLD_FAN_OFF = 24.0

HUMIDITY_THRESHOLD_WINDOW_OPEN = 65.0
HUMIDITY_THRESHOLD_WINDOW_CLOSE = 60.0

LIGHT_THRESHOLD_LOW = 300
LIGHT_THRESHOLD_HIGH = 700

# State variables
fan_is_on = False
window_is_open = False
light_is_on_a1 = False

# --- Global Variables ---
arduino1_ser = None
mqtt_client_edge1 = None
tb_client_edge1 = None
serial_lock_a1 = threading.Lock()
latest_arduino1_data = {
    "temperature": None, "humidity": None, "light": None, "error": None
}
latest_arduino2_sensor_data = {
    "ir_code": None, "button_state": None # 1 for pressed, 0 for released
}

# --- Helper function to send status to ThingsBoard ---
def send_actuator_status_to_thingsboard(status_payload):
    if status_payload and tb_client_edge1 and tb_client_edge1.is_connected():
        print(f"STATUS UPDATE to ThingsBoard: {status_payload}")
        tb_client_edge1.publish("v1/devices/me/telemetry", json.dumps(status_payload), qos=1)

# --- MQTT Callbacks (for Edge-to-Edge MQTT) ---
def on_connect_mqtt_edge(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"EDGE MQTT: Connected to {MQTT_BROKER_HOST}!")
        client.subscribe(TOPIC_EDGE1_CMD_A1)
        print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE1_CMD_A1}")
        client.subscribe(TOPIC_EDGE2_DATA_A2)
        print(f"EDGE MQTT: Subscribed to {TOPIC_EDGE2_DATA_A2}")
    else:
        print(f"EDGE MQTT: Failed to connect, return code {rc}")

def on_message_mqtt_edge(client, userdata, msg):
    global latest_arduino2_sensor_data, light_is_on_a1
    payload_str = msg.payload.decode('utf-8')
    # print(f"EDGE MQTT Received: Topic: {msg.topic}, Payload: {payload_str}") # Kann laut sein
    try:
        data = json.loads(payload_str)
        if msg.topic == TOPIC_EDGE1_CMD_A1:
            handle_arduino1_command_from_mqtt(data) # Manuelle Befehle via MQTT
        elif msg.topic == TOPIC_EDGE2_DATA_A2:
            # IR Code Verarbeitung
            if "ir_code" in data and data["ir_code"]:
                # Verarbeite nur, wenn es ein neuer Code ist oder der letzte Code None war
                if data["ir_code"] != latest_arduino2_sensor_data.get("ir_code"):
                    latest_arduino2_sensor_data["ir_code"] = data["ir_code"]
                    process_ir_for_arduino1(data["ir_code"]) # Sendet Status-Telemetrie intern
                # Setze IR Code zurück, um wiederholte Aktionen zu vermeiden, bis ein neuer Code kommt
                # latest_arduino2_sensor_data["ir_code"] = None # Optional, je nach gewünschtem Verhalten

            # Button Verarbeitung
            if "button_state" in data:
                # Aktion nur bei steigender Flanke (von nicht-gedrückt zu gedrückt)
                if data["button_state"] == 1 and latest_arduino2_sensor_data.get("button_state") != 1:
                    print("Button on A2 pressed, toggling Light on A1.")
                    light_is_on_a1 = not light_is_on_a1
                    send_command_to_arduino1(f"LED:{'ON' if light_is_on_a1 else 'OFF'}")
                    send_actuator_status_to_thingsboard({'light_status': light_is_on_a1})
                latest_arduino2_sensor_data["button_state"] = data["button_state"]
    except json.JSONDecodeError:
        print(f"EDGE MQTT: Error decoding JSON: {payload_str}")
    except Exception as e:
        print(f"EDGE MQTT: Error processing message: {e} on topic {msg.topic}")

def handle_arduino1_command_from_mqtt(command_data):
    global fan_is_on, window_is_open, light_is_on_a1
    telemetry_update = {}
    if "actuator" in command_data and "value" in command_data:
        actuator = command_data["actuator"].upper()
        value_str = str(command_data["value"]).upper()

        if actuator == "LED":
            send_command_to_arduino1(f"LED:{value_str}")
            light_is_on_a1 = (value_str == "ON")
            telemetry_update['light_status'] = light_is_on_a1
        elif actuator == "LED_BRIGHTNESS": # Annahme: Arduino-Code für LED_BRIGHTNESS existiert
            try:
                brightness = int(command_data["value"])
                send_command_to_arduino1(f"LED_BRIGHTNESS:{brightness}")
                light_is_on_a1 = (brightness > 0)
                telemetry_update['light_status'] = light_is_on_a1 # Status basiert auf Helligkeit > 0
            except ValueError: print(f"MQTT CMD Error: Invalid brightness {command_data['value']}")
        elif actuator == "WINDOW":
            try:
                angle = int(command_data["value"])
                send_command_to_arduino1(f"WINDOW:{angle}")
                window_is_open = (angle > 0) # Vereinfacht: offen wenn Winkel > 0
                telemetry_update['window_status'] = window_is_open
            except ValueError: print(f"MQTT CMD Error: Invalid window angle {command_data['value']}")
        elif actuator == "FAN_SPEED": # Annahme: Arduino-Code für FAN_SPEED existiert
            try:
                speed = int(command_data["value"])
                send_command_to_arduino1(f"FAN_SPEED:{speed}")
                if speed > 0:
                    fan_is_on = True
                    # send_command_to_arduino1("FAN_STEPS:100") # Optional
                else:
                    send_command_to_arduino1("FAN_OFF") # FAN_OFF ist expliziter
                    fan_is_on = False
                telemetry_update['fan_status'] = fan_is_on
            except ValueError: print(f"MQTT CMD Error: Invalid fan speed {command_data['value']}")
        elif actuator == "FAN_STEPS": # Annahme: Arduino-Code für FAN_STEPS existiert
            try:
                steps = int(command_data["value"])
                send_command_to_arduino1(f"FAN_STEPS:{steps}")
                if steps != 0 : fan_is_on = True # Gehe davon aus, dass der Fan an ist, wenn Schritte gesendet werden
                # Beachte: fan_is_on wird hier nicht false, wenn steps = 0. FAN_OFF verwenden.
                telemetry_update['fan_status'] = fan_is_on
            except ValueError: print(f"MQTT CMD Error: Invalid fan steps {command_data['value']}")

    elif "raw_command" in command_data:
        send_command_to_arduino1(command_data["raw_command"])
        # Versuche, den Status basierend auf Raw-Befehlen zu aktualisieren
        raw_cmd_upper = command_data["raw_command"].upper()
        if "LED:ON" in raw_cmd_upper: light_is_on_a1 = True; telemetry_update['light_status'] = True
        elif "LED:OFF" in raw_cmd_upper: light_is_on_a1 = False; telemetry_update['light_status'] = False
        elif "FAN_OFF" in raw_cmd_upper: fan_is_on = False; telemetry_update['fan_status'] = False
        elif "WINDOW:0" in raw_cmd_upper: window_is_open = False; telemetry_update['window_status'] = False
        elif "WINDOW:" in raw_cmd_upper and "WINDOW:0" not in raw_cmd_upper : window_is_open = True; telemetry_update['window_status'] = True


    send_actuator_status_to_thingsboard(telemetry_update)

def process_ir_for_arduino1(ir_code_hex):
    global light_is_on_a1, fan_is_on, window_is_open
    telemetry_update = {}

    print(f"Processing IR code {ir_code_hex} for Arduino 1 actuators...")
    code = ir_code_hex.upper() # Einmal konvertieren

    # --- ERSETZEN SIE DIESE IR-CODES MIT IHREN TATSÄCHLICHEN CODES ---
    if code == "0XFF629D": # Beispiel: Licht An/Aus (CH-)
        light_is_on_a1 = not light_is_on_a1
        cmd = f"LED:{'ON' if light_is_on_a1 else 'OFF'}"
        print(f"IR Action: {cmd} on Arduino 1")
        send_command_to_arduino1(cmd)
        telemetry_update['light_status'] = light_is_on_a1
    elif code == "0XFFA25D": # Beispiel: Fan An/Aus (CH)
        fan_is_on = not fan_is_on
        if fan_is_on:
            print("IR Action: Fan ON (speed 10) on Arduino 1")
            send_command_to_arduino1("FAN_SPEED:10")
            send_command_to_arduino1("FAN_STEPS:200") # Damit er sich auch bewegt
        else:
            print("IR Action: Fan OFF on Arduino 1")
            send_command_to_arduino1("FAN_OFF")
        telemetry_update['fan_status'] = fan_is_on
    # elif code == "0XFFE01F": # Beispiel: Fenster Auf (EQ Button)
    #     send_command_to_arduino1("WINDOW:90")
    #     window_is_open = True
    #     telemetry_update['window_status'] = window_is_open
    # elif code == "0XFF906F": # Beispiel: Fenster Zu (+ Button)
    #     send_command_to_arduino1("WINDOW:0")
    #     window_is_open = False
    #     telemetry_update['window_status'] = window_is_open
    else:
        print(f"IR code {code} not mapped to an action for Arduino 1.")

    send_actuator_status_to_thingsboard(telemetry_update)

# --- ThingsBoard MQTT Callbacks ---
def on_connect_tb(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"THINGSBOARD: Connected to {THINGSBOARD_HOST}!")
        client.subscribe("v1/devices/me/rpc/request/+")
        print("THINGSBOARD: Subscribed to RPC requests.")
    else:
        print(f"THINGSBOARD: Failed to connect, return code {rc}")

def on_message_tb(client, userdata, msg):
    global light_is_on_a1, fan_is_on, window_is_open
    print(f"THINGSBOARD RPC Received: Topic: {msg.topic}, Payload: {msg.payload.decode()}")
    request_id = msg.topic.split('/')[-1]
    telemetry_update = {}
    response_payload = {"status": "ERROR", "error": "Unknown method"} # Default error response

    try:
        data = json.loads(msg.payload)
        method = data.get("method")
        params = data.get("params")

        if method == "setLedState":
            new_state = bool(params)
            send_command_to_arduino1(f"LED:{'ON' if new_state else 'OFF'}")
            light_is_on_a1 = new_state
            telemetry_update['light_status'] = light_is_on_a1
            response_payload = {"status": "OK", "led_state_set_to": new_state}
        elif method == "setWindowAngle":
            try:
                angle = int(params)
                send_command_to_arduino1(f"WINDOW:{angle}")
                window_is_open = (angle > 0)
                telemetry_update['window_status'] = window_is_open
                response_payload = {"status": "OK", "window_angle_set_to": angle}
            except ValueError:
                response_payload = {"status": "ERROR", "error": "Invalid angle parameter"}
        elif method == "setFanState":
            new_fan_state = bool(params)
            if new_fan_state:
                send_command_to_arduino1("FAN_SPEED:10")
                send_command_to_arduino1("FAN_STEPS:100") # Minimalbewegung
            else:
                send_command_to_arduino1("FAN_OFF")
            fan_is_on = new_fan_state
            telemetry_update['fan_status'] = fan_is_on
            response_payload = {"status": "OK", "fan_state_set_to": new_fan_state}
        # Fügen Sie hier weitere RPC-Methoden hinzu

        send_actuator_status_to_thingsboard(telemetry_update)

    except Exception as e:
        print(f"THINGSBOARD RPC: Error processing message: {e}")
        response_payload = {"status": "ERROR", "error": str(e)} # Send detailed error back

    if tb_client_edge1 and tb_client_edge1.is_connected():
        tb_client_edge1.publish(f"v1/devices/me/rpc/response/{request_id}", json.dumps(response_payload), qos=1)

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
        print(f"SERIAL A1: Failed to connect to Arduino 1: {e}."); arduino1_ser = None; return False
    except Exception as ex:
        print(f"SERIAL A1: An unexpected error during Arduino 1 connection: {ex}"); arduino1_ser = None; return False

def send_command_to_arduino1(command):
    if arduino1_ser and arduino1_ser.is_open:
        with serial_lock_a1:
            try:
                # print(f"SERIAL A1: Sending: {command}") # Kann laut sein
                arduino1_ser.write((command + '\n').encode('utf-8'))
            except serial.SerialException as e: print(f"SERIAL A1: Error writing: {e}. Conn lost?."); # arduino1_ser = None # Optional
            except Exception as e: print(f"SERIAL A1: Unexpected error writing: {e}")
    else:
        print(f"SERIAL A1: Not connected. Cannot send command: {command}")

def read_from_arduino1_thread_func():
    global latest_arduino1_data, arduino1_ser
    while True:
        if not (arduino1_ser and arduino1_ser.is_open):
            if not connect_to_arduino1(): time.sleep(5); continue
        try:
            if arduino1_ser.in_waiting > 0:
                line = ""
                with serial_lock_a1: line = arduino1_ser.readline().decode('utf-8').rstrip()
                if line:
                    try:
                        data = json.loads(line); latest_arduino1_data.update(data)
                        if mqtt_client_edge1 and mqtt_client_edge1.is_connected() and "error" not in data:
                            mqtt_client_edge1.publish(TOPIC_EDGE1_DATA_A1, json.dumps(data), qos=1)
                        if tb_client_edge1 and tb_client_edge1.is_connected() and "error" not in data:
                            tb_client_edge1.publish("v1/devices/me/telemetry", json.dumps(data), qos=1)
                    except json.JSONDecodeError: print(f"SERIAL A1: JSON Decode Error: {line}"); latest_arduino1_data["error"] = "JSON Decode Error"
                    except Exception as e_json: print(f"SERIAL A1: Error processing JSON ({line}): {e_json}"); latest_arduino1_data["error"] = f"Proc Error: {e_json}"
        except serial.SerialException as e: print(f"SERIAL A1: Serial error during read: {e}. Disconnecting."); arduino1_ser.close(); arduino1_ser = None; continue
        except AttributeError as ae: print(f"SERIAL A1: AttributeError (arduino1_ser None?): {ae}. Re-evaluating."); arduino1_ser = None; continue
        except Exception as e_read: print(f"SERIAL A1: Unexpected read error: {e_read}"); arduino1_ser.close(); arduino1_ser = None; time.sleep(1); continue
        time.sleep(0.1)

# --- Rules Engine Logic ---
def apply_rules_arduino1():
    global fan_is_on, window_is_open, light_is_on_a1
    telemetry_update = {} # Sammle Statusänderungen für einmaliges Senden

    temp = latest_arduino1_data.get("temperature")
    humidity = latest_arduino1_data.get("humidity")
    light_level = latest_arduino1_data.get("light")

    # Regel: Temperatur für Fan
    if temp is not None:
        if temp > TEMP_THRESHOLD_FAN_ON and not fan_is_on:
            print(f"RULE A1: Temp ({temp}°C) > {TEMP_THRESHOLD_FAN_ON}°C. Turning FAN ON.")
            send_command_to_arduino1("FAN_SPEED:10"); send_command_to_arduino1("FAN_STEPS:200")
            fan_is_on = True; telemetry_update['fan_status'] = True
        elif temp < TEMP_THRESHOLD_FAN_OFF and fan_is_on:
            print(f"RULE A1: Temp ({temp}°C) < {TEMP_THRESHOLD_FAN_OFF}°C. Turning FAN OFF.")
            send_command_to_arduino1("FAN_OFF")
            fan_is_on = False; telemetry_update['fan_status'] = False

    # Regel: Feuchtigkeit für Fenster
    if humidity is not None:
        if humidity > HUMIDITY_THRESHOLD_WINDOW_OPEN and not window_is_open:
            print(f"RULE A1: Humidity ({humidity}%) > {HUMIDITY_THRESHOLD_WINDOW_OPEN}%. Opening WINDOW.")
            send_command_to_arduino1("WINDOW:90")
            window_is_open = True; telemetry_update['window_status'] = True
        elif humidity < HUMIDITY_THRESHOLD_WINDOW_CLOSE and window_is_open:
            print(f"RULE A1: Humidity ({humidity}%) < {HUMIDITY_THRESHOLD_WINDOW_CLOSE}%. Closing WINDOW.")
            send_command_to_arduino1("WINDOW:0")
            window_is_open = False; telemetry_update['window_status'] = False

    # Regel: Lichtlevel für LED
    if light_level is not None:
        if light_level < LIGHT_THRESHOLD_LOW and not light_is_on_a1:
            print(f"RULE A1: Light ({light_level}) < {LIGHT_THRESHOLD_LOW}. Turning LIGHT ON.")
            send_command_to_arduino1("LED:ON")
            light_is_on_a1 = True; telemetry_update['light_status'] = True
        elif light_level > LIGHT_THRESHOLD_HIGH and light_is_on_a1:
            print(f"RULE A1: Light ({light_level}) > {LIGHT_THRESHOLD_HIGH}. Turning LIGHT OFF.")
            send_command_to_arduino1("LED:OFF")
            light_is_on_a1 = False; telemetry_update['light_status'] = False

    send_actuator_status_to_thingsboard(telemetry_update)


def rule_engine_thread_func():
    while True:
        apply_rules_arduino1()
        time.sleep(2)


if __name__ == "__main__":
    print("Starting Edge Device 1...")

    # Edge-to-Edge MQTT Client
    mqtt_client_edge1 = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID_EDGE1)
    mqtt_client_edge1.on_connect = on_connect_mqtt_edge
    mqtt_client_edge1.on_message = on_message_mqtt_edge
    try:
        print(f"EDGE MQTT: Connecting to {MQTT_BROKER_HOST} as {MQTT_CLIENT_ID_EDGE1}")
        mqtt_client_edge1.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        mqtt_client_edge1.loop_start()
    except Exception as e: print(f"EDGE MQTT: Connection failed: {e}")

    # ThingsBoard MQTT Client
    tb_mqtt_client_id = f"tb_{MQTT_CLIENT_ID_EDGE1}"
    tb_client_edge1 = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=tb_mqtt_client_id)
    tb_client_edge1.username_pw_set(THINGSBOARD_ACCESS_TOKEN_EDGE1)
    tb_client_edge1.on_connect = on_connect_tb
    tb_client_edge1.on_message = on_message_tb
    try:
        print(f"THINGSBOARD: Connecting to {THINGSBOARD_HOST} (token: {THINGSBOARD_ACCESS_TOKEN_EDGE1[:5]}...)")
        tb_client_edge1.connect(THINGSBOARD_HOST, THINGSBOARD_PORT, 60)
        tb_client_edge1.loop_start()
    except Exception as e: print(f"THINGSBOARD: Connection failed: {e}")

    # Start threads
    arduino_reader_thread = threading.Thread(target=read_from_arduino1_thread_func, daemon=True, name="Arduino1ReaderThread")
    rules_thread = threading.Thread(target=rule_engine_thread_func, daemon=True, name="RuleEngineThread")
    arduino_reader_thread.start()
    rules_thread.start()

    print(f"Edge Device 1 (Status Telemetry Enabled) now running.")
    print(f"Listening for Arduino 1 on {SERIAL_PORT_ARDUINO1}")
    print("Ctrl+C to exit.")

    try:
        while True: time.sleep(10)
    except KeyboardInterrupt: print("\nExiting Edge Device 1...")
    finally:
        print("Cleaning up Edge Device 1 resources...")
        if arduino1_ser and arduino1_ser.is_open: print("Closing Arduino 1 serial port."); arduino1_ser.close()
        if mqtt_client_edge1 and mqtt_client_edge1.is_connected(): print("Stopping Edge MQTT."); mqtt_client_edge1.loop_stop(); mqtt_client_edge1.disconnect()
        if tb_client_edge1 and tb_client_edge1.is_connected(): print("Stopping ThingsBoard MQTT."); tb_client_edge1.loop_stop(); tb_client_edge1.disconnect()
        print("Edge Device 1 stopped.")