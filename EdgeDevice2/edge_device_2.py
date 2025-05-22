# edge_device_2.py (Refactored for better architecture and functionality with English comments AND Google Calendar Integration)

import serial
import time
import json
import threading
import paho.mqtt.client as mqtt
import os # For the dummy API trigger file check

# --- Google Calendar API Imports ---
import datetime
import pytz # For timezone handling
from dateutil import parser as dateutil_parser # For robust date string parsing
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Configuration ---
SERIAL_PORT_ARDUINO2 = '/dev/tty.usbmodem11301'
#'/dev/ttyACM0'  # YOUR CORRECT RPi PORT for Arduino 2
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
THINGSBOARD_HOST = "mqtt.thingsboard.cloud" # Or your instance (ensure this is correct for Edge2)
THINGSBOARD_PORT = 1883
THINGSBOARD_ACCESS_TOKEN_EDGE2 = "rj6t94nd52Gk2vFrXXDO" # Your valid token for Edge 2's device

# --- Google Calendar Configuration ---
GCAL_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
GCAL_CREDENTIALS_FILE = 'credentials.json' # Should be in the same directory
GCAL_TOKEN_FILE = 'token.json'           # Will be created after first authorization
GCAL_CALENDAR_ID = 'primary'             # 'primary' for the main calendar, or a specific calendar ID
GCAL_ALARM_KEYWORD = '[BUZZER]'          # Keyword in event summary to trigger alarm
GCAL_CHECK_INTERVAL_SECONDS = 60 * 1     # Check calendar every 1 minute
GCAL_EVENT_LOOKAHEAD_MINUTES = 15        # How many minutes into the future to fetch events
GCAL_TRIGGER_WINDOW_MINUTES = 2          # Trigger buzzer if event starts within this many minutes from now
GCAL_BUZZER_DURATION_MS = 2000           # How long the buzzer should beep for a calendar event

# --- State variables ---
actuator_states_a2 = { # For actuators on Arduino 2
    "buzzer_status": False # True if ON, False if OFF
}
processed_calendar_event_ids = set() # To avoid re-triggering alarms for the same event

# --- Global Variables for Connections & Data ---
arduino2_ser = None
edge_mqtt_client = None
tb_mqtt_client = None
serial_lock_a2 = threading.Lock()
gcal_service_lock = threading.Lock() # To protect gcal_service initialization
gcal_service = None


# Cache for latest input data from Arduino 2, mainly for debugging or internal use
latest_arduino2_input_data = {
    "ir_code": None, "button_state": None, "error": None, "api_alarm_event_sent": False
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
    payload_str = msg.payload.decode('utf-8')
    try:
        data = json.loads(payload_str)
        if msg.topic == TOPIC_EDGE2_A2_CMD_SUB:
            if "actuator" in data and data["actuator"].upper() == "BUZZER":
                new_state_str = str(data.get("value", "")).upper()
                new_buzzer_on_state = (new_state_str == "ON")

                if new_state_str == "BEEP":
                    duration = data.get("duration", 500)
                    print(f"EDGE MQTT CMD for A2: BUZZER BEEP for {duration}ms")
                    send_command_to_arduino2(f"BUZZER_BEEP:{duration}")
                elif actuator_states_a2['buzzer_status'] != new_buzzer_on_state:
                    print(f"EDGE MQTT CMD for A2: BUZZER {'ON' if new_buzzer_on_state else 'OFF'}")
                    send_command_to_arduino2(f"BUZZER:{'ON' if new_buzzer_on_state else 'OFF'}")
                    actuator_states_a2['buzzer_status'] = new_buzzer_on_state
                    publish_buzzer_status_to_thingsboard(new_buzzer_on_state)
            elif "raw_command" in data:
                 print(f"EDGE MQTT RAW CMD for A2: {data['raw_command']}")
                 send_command_to_arduino2(data["raw_command"])
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
    print(f"THINGSBOARD RPC Received (Edge2): Topic: {msg.topic}, Payload: {msg.payload.decode()}")
    request_id = msg.topic.split('/')[-1]
    response_payload = {"status": "OK"}
    telemetry_update = {}

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
            except ValueError:
                 response_payload = {"status": "ERROR", "error": "Invalid duration for beep"}
        else:
            response_payload = {"status": "ERROR", "error": f"Unknown RPC method: {method}"}

        if telemetry_update:
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
    last_ir_code_sent_to_edge = None
    last_button_state_sent_to_edge = None

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
                        latest_arduino2_input_data.update(data)
                        tb_payload = {}
                        edge_payload = {}

                        if "ir_code" in data and data["ir_code"]:
                            tb_payload["ir_code_received"] = data["ir_code"]
                            if data["ir_code"] != last_ir_code_sent_to_edge:
                                edge_payload["ir_code"] = data["ir_code"]
                                last_ir_code_sent_to_edge = data["ir_code"]

                        if "button_state" in data:
                            tb_payload["button_state"] = data["button_state"]
                            if data["button_state"] != last_button_state_sent_to_edge:
                                edge_payload["button_state"] = data["button_state"]
                                last_button_state_sent_to_edge = data["button_state"]
                        
                        if "error" in data:
                            tb_payload["arduino2_error"] = data["error"]
                            print(f"SERIAL A2: Received error from Arduino: {data['error']}")

                        if edge_payload and edge_mqtt_client and edge_mqtt_client.is_connected():
                            edge_mqtt_client.publish(TOPIC_EDGE2_A2_INPUTS_PUB, json.dumps(edge_payload), qos=1)
                        
                        if tb_payload and tb_mqtt_client and tb_mqtt_client.is_connected():
                            tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(tb_payload), qos=1)

                    except json.JSONDecodeError: print(f"SERIAL A2: JSON Decode Error: {line}"); latest_arduino2_input_data["error"] = "JSON Decode Error"
                    except Exception as e_json: print(f"SERIAL A2: Error processing JSON ({line}): {e_json}"); latest_arduino2_input_data["error"] = f"Proc Error: {e_json}"
        except serial.SerialException as e: print(f"SERIAL A2: Serial error during read: {e}. Disconnecting."); arduino2_ser.close(); arduino2_ser = None; continue
        except AttributeError as ae: print(f"SERIAL A2: AttributeError (arduino2_ser None?): {ae}. Re-evaluating."); arduino2_ser = None; continue
        except Exception as e_read: print(f"SERIAL A2: Unexpected read error: {e_read}"); arduino2_ser.close(); arduino2_ser = None; time.sleep(1); continue
        time.sleep(0.1)


# --- Google Calendar Integration ---
def get_google_calendar_service():
    global gcal_service
    with gcal_service_lock: # Ensure only one thread initializes/refreshes service
        if gcal_service: # If service object already exists and is valid (simplified check)
            # A more robust check might involve trying a quick API call or checking token expiry
            return gcal_service

        creds = None
        if os.path.exists(GCAL_TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(GCAL_TOKEN_FILE, GCAL_SCOPES)
            except Exception as e:
                print(f"GCAL: Error loading token file: {e}")
                creds = None # Force re-auth

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    print("GCAL: Refreshing access token...")
                    creds.refresh(Request())
                except Exception as e:
                    print(f"GCAL: Error refreshing token: {e}. Manual re-auth may be needed.")
                    # If refresh fails, we might need to delete token.json and re-run flow
                    if os.path.exists(GCAL_TOKEN_FILE):
                        try:
                            os.remove(GCAL_TOKEN_FILE)
                            print(f"GCAL: Removed problematic {GCAL_TOKEN_FILE}. Please re-run for auth.")
                        except OSError as oe:
                            print(f"GCAL: Could not remove {GCAL_TOKEN_FILE}: {oe}")
                    creds = None # Force full flow
            else:
                if not os.path.exists(GCAL_CREDENTIALS_FILE):
                    print(f"GCAL ERROR: Credentials file '{GCAL_CREDENTIALS_FILE}' not found. Please download it from Google Cloud Console.")
                    return None
                try:
                    print("GCAL: No valid token found, running authorization flow...")
                    flow = InstalledAppFlow.from_client_secrets_file(GCAL_CREDENTIALS_FILE, GCAL_SCOPES)
                    # Run local server, user will be directed to browser.
                    # If running headless, you might need a different auth flow or use --noauth_local_webserver (see Google docs)
                    creds = flow.run_local_server(port=0) 
                except Exception as e:
                    print(f"GCAL: Error during authorization flow: {e}")
                    return None
            
            if creds:
                try:
                    with open(GCAL_TOKEN_FILE, 'w') as token_file:
                        token_file.write(creds.to_json())
                    print(f"GCAL: Token saved to {GCAL_TOKEN_FILE}")
                except Exception as e:
                    print(f"GCAL: Error saving token: {e}")

        if creds and creds.valid:
            try:
                service = build('calendar', 'v3', credentials=creds)
                gcal_service = service # Store for reuse
                print("GCAL: Google Calendar service created successfully.")
                return service
            except Exception as e:
                print(f"GCAL: Error building service: {e}")
                gcal_service = None
                return None
        else:
            print("GCAL: Could not obtain valid credentials.")
            gcal_service = None
            return None

def check_google_calendar_events_thread_func():
    global processed_calendar_event_ids
    print("GCAL: Google Calendar check thread started.")
    
    # Initial service acquisition
    service = get_google_calendar_service()
    if not service:
        print("GCAL: Failed to initialize Google Calendar service. Thread will retry periodically.")

    while True:
        if not service: # Attempt to re-initialize service if it failed previously
            print("GCAL: Attempting to re-initialize Google Calendar service...")
            service = get_google_calendar_service()
            if not service:
                print("GCAL: Re-initialization failed. Will try again later.")
                time.sleep(GCAL_CHECK_INTERVAL_SECONDS * 2) # Wait longer if service init fails
                continue
        
        try:
            now_utc = datetime.datetime.now(pytz.utc)
            time_min_iso = now_utc.isoformat()
            time_max_iso = (now_utc + datetime.timedelta(minutes=GCAL_EVENT_LOOKAHEAD_MINUTES)).isoformat()

            # print(f"GCAL: Checking for events between {time_min_iso} and {time_max_iso}")
            events_result = service.events().list(
                calendarId=GCAL_CALENDAR_ID,
                timeMin=time_min_iso,
                timeMax=time_max_iso,
                maxResults=10, # Max 10 events in the lookahead window
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            items = events_result.get('items', [])

            if not items:
                # print("GCAL: No upcoming events found in the lookahead window.")
                pass
            
            found_trigger_event = False
            for event in items:
                event_id = event['id']
                summary = event.get('summary', 'No Title')

                start_data = event['start']
                # Google Calendar API returns 'dateTime' for timed events, 'date' for all-day events
                start_str = start_data.get('dateTime', start_data.get('date'))
                
                try:
                    # dateutil.parser is good at handling various ISO 8601 formats
                    event_start_time_naive = dateutil_parser.isoparse(start_str)
                except ValueError:
                    print(f"GCAL: Could not parse start time for event '{summary}': {start_str}")
                    continue

                # Make it timezone-aware (UTC if no tzinfo, or convert to UTC)
                if event_start_time_naive.tzinfo is None or event_start_time_naive.tzinfo.utcoffset(event_start_time_naive) is None:
                    event_start_time_utc = pytz.utc.localize(event_start_time_naive)
                else:
                    event_start_time_utc = event_start_time_naive.astimezone(pytz.utc)

                # print(f"GCAL: Event: {summary} starts at {event_start_time_utc}")

                if GCAL_ALARM_KEYWORD.lower() in summary.lower():
                    # Check if event is starting now or very soon and hasn't been processed
                    time_until_event = (event_start_time_utc - now_utc).total_seconds()
                    
                    # Trigger if event starts within the window (e.g., -30s to +GCAL_TRIGGER_WINDOW_MINUTES)
                    # Allowing a small negative window handles events that might have just started
                    if -30 < time_until_event < (GCAL_TRIGGER_WINDOW_MINUTES * 60):
                        if event_id not in processed_calendar_event_ids:
                            print(f"GCAL ALARM: Event '{summary}' is starting soon! Triggering buzzer.")
                            send_command_to_arduino2(f"BUZZER_BEEP:{GCAL_BUZZER_DURATION_MS}")
                            processed_calendar_event_ids.add(event_id)
                            found_trigger_event = True

                            # Optionally send telemetry to ThingsBoard
                            if tb_mqtt_client and tb_mqtt_client.is_connected():
                                cal_alarm_payload = {
                                    "calendar_alarm_triggered": True,
                                    "event_summary": summary,
                                    "event_start_time_utc": event_start_time_utc.isoformat(),
                                    "timestamp": int(time.time() * 1000)
                                }
                                tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(cal_alarm_payload), qos=1)
                                print(f"THINGSBOARD EVENT (Edge2): Google Calendar Alarm for '{summary}' at { event_start_time_utc.isoformat() }.")
                        # else:
                            # print(f"GCAL: Event '{summary}' already processed.")
                    # else:
                        # print(f"GCAL: Event '{summary}' with keyword, but not in trigger window ({time_until_event/60 :.1f} mins away).")
            
            # Simple cleanup of old processed event IDs (e.g., older than 1 day to prevent unbounded growth)
            # More sophisticated cleanup might be needed for very long running scripts / many events
            if len(processed_calendar_event_ids) > 100: # Arbitrary limit
                 # This is a naive way to clean up. A better way would be to store timestamps with IDs.
                 # For now, if it gets too big, just clear a portion or all.
                 print("GCAL: Clearing some old processed event IDs to save memory.")
                 # Example: convert to list, keep recent N, convert back to set
                 # For simplicity here, just clear if it grows too large. This means events might re-trigger after a clear.
                 # A robust solution would involve storing event start times and removing those far in the past.
                 processed_calendar_event_ids.clear()


        except HttpError as error:
            print(f'GCAL: An API error occurred: {error}')
            if error.resp.status in [401, 403]: # Unauthorized or Forbidden
                print("GCAL: Authentication/Authorization error. Attempting to refresh/re-auth service.")
                service = None # Force re-authentication in the next loop
                if os.path.exists(GCAL_TOKEN_FILE): # Remove potentially bad token
                    try: os.remove(GCAL_TOKEN_FILE)
                    except OSError: pass
            # Other errors might be transient network issues.
            time.sleep(GCAL_CHECK_INTERVAL_SECONDS) # Wait before retrying after an error
        except Exception as e:
            print(f"GCAL: Unexpected error in calendar check thread: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(GCAL_CHECK_INTERVAL_SECONDS) # Wait before retrying

        time.sleep(GCAL_CHECK_INTERVAL_SECONDS)


# --- API Integration Placeholder & Buzzer Control ---
def check_for_api_alarm_trigger(): # This is the DUMMY file trigger
    if os.path.exists(DUMMY_API_TRIGGER_FILE):
        try:
            os.remove(DUMMY_API_TRIGGER_FILE)
            print("API DUMMY: Trigger file found and removed.")
            return True
        except OSError as e:
            print(f"API DUMMY: Error removing trigger file: {e}")
            return False
    return False

def api_alarm_thread_func(): # DUMMY file trigger thread
    print("API DUMMY Alarm Thread started. To trigger, create 'trigger_buzzer_api.flag'")
    while True:
        if check_for_api_alarm_trigger():
            print("API DUMMY RULE: Alarm condition met. Triggering BUZZER BEEP on A2 for 1s.")
            send_command_to_arduino2("BUZZER_BEEP:1000")
            if tb_mqtt_client and tb_mqtt_client.is_connected():
                api_alarm_event_payload = {"dummy_api_alarm_event": True, "timestamp": int(time.time() * 1000)}
                tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(api_alarm_event_payload), qos=1)
                print(f"THINGSBOARD EVENT (Edge2): Dummy API Alarm Triggered.")
        time.sleep(3)


if __name__ == "__main__":
    print("Starting Edge Device 2 (Refactored with Google Calendar)...")

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
    # dummy_api_thread = threading.Thread(target=api_alarm_thread_func, daemon=True, name="ApiAlarmThread") # Kept the dummy one for now
    google_calendar_check_thread = threading.Thread(target=check_google_calendar_events_thread_func, daemon=True, name="GoogleCalendarCheckThread")

    arduino_reader_thread.start()
    # dummy_api_thread.start()
    google_calendar_check_thread.start()

    print(f"Edge Device 2 (Refactored) now running.")
    print(f"Listening for Arduino 2 on {SERIAL_PORT_ARDUINO2}")
    print(f"Google Calendar check for keyword '{GCAL_ALARM_KEYWORD}' in calendar '{GCAL_CALENDAR_ID}' is active.")
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