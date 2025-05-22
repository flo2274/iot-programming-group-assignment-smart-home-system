# edge_device_2.py (Calendar: Robust Timed Away & Alarms, Explicit UTC, Formatted TB Time)

import serial
import time
import json
import threading
import paho.mqtt.client as mqtt
import os
import re # For parsing duration from event summary

# --- Google Calendar API Imports ---
import datetime
import pytz 
from dateutil import parser as dateutil_parser
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Configuration ---
SERIAL_PORT_ARDUINO2 = '' #'/dev/tty.usbmodem11301' # Set if Arduino 2 (Buzzer) is used
BAUD_RATE = 9600

MQTT_BROKER_HOST = "test.mosquitto.org"
MQTT_BROKER_PORT = 1883
MQTT_CLIENT_ID_EDGE2 = "edge2-smart-home-calendar-22312"

TOPIC_PREFIX = "iot_project/groupXY" # !!! CHANGE groupXY to your actual group !!!
TOPIC_EDGE2_A2_CMD_SUB = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE2}/arduino2/cmd"
TOPIC_PRESENCE_STATUS_PUB = f"{TOPIC_PREFIX}/home/presence_status"

THINGSBOARD_HOST = "mqtt.thingsboard.cloud"
THINGSBOARD_PORT = 1883
THINGSBOARD_ACCESS_TOKEN_EDGE2 = "rj6t94nd52Gk2vFrXXDO"

GCAL_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
GCAL_CREDENTIALS_FILE = 'credentials.json'
GCAL_TOKEN_FILE = 'token.json'
GCAL_CALENDAR_ID = 'primary'
GCAL_ALARM_KEYWORD = '[ALARM]'
GCAL_AWAY_KEYWORD_PATTERN = r"\[AWAY(?:[:\s](\d+))?\]"
GCAL_AWAY_DEFAULT_DURATION_MINUTES = 30
GCAL_CHECK_INTERVAL_SECONDS = 30
GCAL_EVENT_LOOKAHEAD_MINUTES = 20 
GCAL_TRIGGER_WINDOW_MINUTES = 2 
GCAL_BUZZER_DURATION_MS = 2000

# --- State variables ---
processed_away_event_ids = set()
processed_alarm_event_ids = set()
initial_presence_published_flag = False # Global flag for initial publish logic

# --- Presence State & Timer ---
person_at_home_status = True
person_at_home_lock = threading.RLock()
away_timer = None
away_event_active_id = None # Stores summary of event causing current away state

# --- Global Variables ---
arduino2_ser = None
edge_mqtt_client = None
tb_mqtt_client = None
serial_lock_a2 = threading.Lock()
gcal_service_lock = threading.Lock()
gcal_service = None

# --- Helper Functions ---
def get_formatted_local_time():
    """Returns the current system local time, formatted nicely with timezone."""
    return datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')

def publish_presence_status(at_home, reason=""):
    global person_at_home_status, initial_presence_published_flag
    
    # Check if status needs to be updated or if it's the very first publish
    publish_needed = False
    #with person_at_home_lock:
    if not initial_presence_published_flag: # Always publish the first time
        publish_needed = True
        # initial_presence_published_flag is set to True in the calendar thread after this first call
    elif person_at_home_status != at_home: # Publish if status changed
        publish_needed = True
    
    if publish_needed:
        person_at_home_status = at_home
        print(f"PRESENCE INFO: Status changing to person_at_home={at_home}. Reason: {reason}")
    # else:
        # print(f"PRESENCE DEBUG: Status unchanged ({at_home}), not publishing. Reason: {reason}")

    if not publish_needed:
        return

    payload = {"person_at_home": at_home}
    # Publish to Edge MQTT
    if edge_mqtt_client and edge_mqtt_client.is_connected():
        try:
            # print(f"PRESENCE MQTT: Attempting publish to {TOPIC_PRESENCE_STATUS_PUB}: {payload}")
            edge_mqtt_client.publish(TOPIC_PRESENCE_STATUS_PUB, json.dumps(payload), qos=1, retain=True)
            # print(f"PRESENCE MQTT: Publish successful.")
        except Exception as e_mqtt_edge:
            print(f"PRESENCE MQTT ERROR: Failed to publish to Edge MQTT: {e_mqtt_edge}")
    else:
        print("PRESENCE MQTT WARN: Edge client not connected, cannot publish presence.")
    
    # Publish to ThingsBoard
    if tb_mqtt_client and tb_mqtt_client.is_connected():
        tb_payload = {
            "person_at_home_calendar": at_home,
            "last_presence_update_reason": reason,
            "last_presence_update_local_time": get_formatted_local_time()
            }
        try:
            # print(f"THINGSBOARD: Attempting presence update: {tb_payload}")
            tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(tb_payload), qos=1)
            print(f"THINGSBOARD: Sent presence update: {tb_payload}")
        except Exception as e_mqtt_tb:
            print(f"THINGSBOARD ERROR: Failed to publish presence: {e_mqtt_tb}")
    else:
        print("THINGSBOARD WARN: TB client not connected, cannot publish presence.")


def set_person_at_home():
    global away_timer, away_event_active_id
    with person_at_home_lock:
        current_away_event_summary = away_event_active_id
        reason_log = f"Away timer expired for event '{current_away_event_summary}'"
        print(f"PRESENCE TIMER: {reason_log}. Setting person AT HOME.")
        publish_presence_status(True, reason_log)
        
        if tb_mqtt_client and tb_mqtt_client.is_connected() and current_away_event_summary:
            tb_payload_away_ended = {
                "calendar_away_event_active": False,
                "ended_away_event_summary": current_away_event_summary,
                "away_ended_local_time": get_formatted_local_time(),
                "timestamp": int(time.time() * 1000)
            }
            try:
                tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(tb_payload_away_ended), qos=1)
                print(f"THINGSBOARD: Sent away event ended: {tb_payload_away_ended}")
            except Exception as e: print(f"THINGSBOARD ERROR: Failed to publish away_ended: {e}")
        
        away_timer = None
        away_event_active_id = None

def start_away_timer(duration_minutes, event_id_triggering, event_summary_for_log):
    global away_timer, away_event_active_id
    with person_at_home_lock:
        if away_timer:
            print(f"PRESENCE TIMER: Cancelling existing away timer (for event '{away_event_active_id}') "
                  f"due to new event '{event_summary_for_log}' (ID: {event_id_triggering}).")
            away_timer.cancel()
        
        reason_log = f"AWAY event '{event_summary_for_log}' (ID: {event_id_triggering})"
        print(f"PRESENCE TIMER: Setting person NOT AT HOME for {duration_minutes} minutes. Trigger: {reason_log}")
        publish_presence_status(False, reason_log) # This will try to publish
        
        if tb_mqtt_client and tb_mqtt_client.is_connected():
            tb_payload_away = {
                "calendar_away_event_active": True,
                "away_event_summary": event_summary_for_log,
                "away_duration_minutes": duration_minutes,
                "away_triggered_local_time": get_formatted_local_time(),
                "timestamp": int(time.time() * 1000)
            }
            try:
                tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(tb_payload_away), qos=1)
                print(f"THINGSBOARD: Sent away event active: {tb_payload_away}")
            except Exception as e: print(f"THINGSBOARD ERROR: Failed to publish away_active: {e}")

        away_duration_seconds = duration_minutes * 60
        away_timer = threading.Timer(away_duration_seconds, set_person_at_home)
        away_timer.daemon = True
        away_timer.start()
        away_event_active_id = event_summary_for_log

# --- MQTT Callbacks ---
def on_connect_edge_mqtt(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"EDGE MQTT: Connected to {MQTT_BROKER_HOST} as {MQTT_CLIENT_ID_EDGE2}!")
        client.subscribe(TOPIC_EDGE2_A2_CMD_SUB)
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
                if new_state_str == "BEEP":
                    duration = data.get("duration", GCAL_BUZZER_DURATION_MS)
                    print(f"EDGE MQTT CMD for A2: BUZZER BEEP for {duration}ms")
                    if SERIAL_PORT_ARDUINO2: send_command_to_arduino2(f"BUZZER_BEEP:{duration}")
            elif "raw_command" in data:
                 if SERIAL_PORT_ARDUINO2: send_command_to_arduino2(data["raw_command"])
    except Exception as e: print(f"EDGE MQTT: Error processing msg on topic {msg.topic}: {e}")

# --- ThingsBoard MQTT Callbacks ---
def on_connect_tb(client, userdata, flags, rc, properties=None):
    if rc == 0: print(f"THINGSBOARD: Connected to {THINGSBOARD_HOST} (Edge2)!")
    else: print(f"THINGSBOARD: Failed to connect (Edge2), rc {rc}")

def on_message_tb(client, userdata, msg): pass

# --- Arduino 2 Serial Communication ---
def connect_to_arduino2():
    global arduino2_ser
    if not SERIAL_PORT_ARDUINO2: return False
    try:
        if arduino2_ser and arduino2_ser.is_open: return True
        if arduino2_ser: arduino2_ser.close()
        arduino2_ser = serial.Serial(SERIAL_PORT_ARDUINO2, BAUD_RATE, timeout=1)
        print(f"SERIAL A2: Successfully connected to Arduino 2 on {SERIAL_PORT_ARDUINO2}.")
        time.sleep(2); arduino2_ser.flushInput(); return True
    except Exception as e: print(f"SERIAL A2: Connect failed for {SERIAL_PORT_ARDUINO2}: {e}"); arduino2_ser = None; return False

def send_command_to_arduino2(command):
    if not arduino2_ser or not arduino2_ser.is_open: return
    with serial_lock_a2:
        try: arduino2_ser.write((command + '\n').encode('utf-8'))
        except Exception as e: print(f"SERIAL A2: Error writing '{command}': {e}")

def read_from_arduino2_thread_func():
    if not SERIAL_PORT_ARDUINO2: return
    while True:
        if not connect_to_arduino2(): time.sleep(5); continue
        time.sleep(1)

# --- Google Calendar Integration ---
def get_google_calendar_service():
    global gcal_service
    with gcal_service_lock:
        if gcal_service: return gcal_service
        creds = None
        if os.path.exists(GCAL_TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(GCAL_TOKEN_FILE, GCAL_SCOPES)
            except Exception as e: print(f"GCAL AUTH: Error loading token: {e}"); creds = None
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: print("GCAL AUTH: Refreshing token..."); creds.refresh(Request())
                except Exception as e:
                    print(f"GCAL AUTH: Refresh failed: {e}. Re-auth needed.")
                    if os.path.exists(GCAL_TOKEN_FILE):
                        try: os.remove(GCAL_TOKEN_FILE)
                        except OSError: pass
                    creds = None
            else:
                if not os.path.exists(GCAL_CREDENTIALS_FILE):
                    print(f"GCAL ERROR: Credentials file '{GCAL_CREDENTIALS_FILE}' missing."); return None
                try:
                    print("GCAL AUTH: Running authorization flow...");
                    flow = InstalledAppFlow.from_client_secrets_file(GCAL_CREDENTIALS_FILE, GCAL_SCOPES)
                    creds = flow.run_local_server(port=0)
                except Exception as e: print(f"GCAL AUTH: Flow error: {e}"); return None
            if creds:
                try:
                    with open(GCAL_TOKEN_FILE, 'w') as token_file: token_file.write(creds.to_json())
                    print(f"GCAL AUTH: Token saved.")
                except Exception as e: print(f"GCAL AUTH: Token save error: {e}")
        if creds and creds.valid:
            try:
                gcal_service = build('calendar', 'v3', credentials=creds)
                print("GCAL: Service created."); return gcal_service
            except Exception as e: print(f"GCAL: Build service error: {e}"); gcal_service = None; return None
        else: print("GCAL AUTH: No valid credentials."); gcal_service = None; return None

def parse_event_time(time_data_str):
    try:
        dt_obj = dateutil_parser.isoparse(time_data_str)
        if dt_obj.tzinfo is None or dt_obj.tzinfo.utcoffset(dt_obj) is None:
            return pytz.utc.localize(dt_obj)
        else:
            return dt_obj.astimezone(pytz.utc)
    except ValueError as e:
        print(f"GCAL PARSE ERROR: Could not parse time string: '{time_data_str}'. Error: {e}")
        return None

def check_google_calendar_events_thread_func():
    global processed_away_event_ids, processed_alarm_event_ids, initial_presence_published_flag
    global person_at_home_status, away_timer, away_event_active_id # Ensure all globals used are declared
    
    print("GCAL: Calendar check thread started.") # PRINT S1
    
    print("GCAL THREAD DEBUG: Initial sleep for MQTT client connections...")
    time.sleep(5) 
    print("GCAL THREAD DEBUG: Initial sleep finished.")

    service = get_google_calendar_service()
    if not service: print("GCAL WARN: GCal service init failed on first attempt. Thread will retry.")

    while True: 
        print("GCAL THREAD DEBUG: Top of while True loop.") # PRINT A
        if not service:
            print("GCAL THREAD DEBUG: Service is None, attempting to get service.") # PRINT B
            service = get_google_calendar_service()
            if not service:
                print(f"GCAL WARN: Still no GCal service. Retrying in {GCAL_CHECK_INTERVAL_SECONDS * 2}s.") # PRINT C
                time.sleep(GCAL_CHECK_INTERVAL_SECONDS * 2)
                print("GCAL THREAD DEBUG: Woke up after service retry sleep.") # PRINT D
                continue
            print("GCAL THREAD DEBUG: Service re-acquired.") # PRINT E
        
        now_local_system_aware = datetime.datetime.now().astimezone()
        now_utc = now_local_system_aware.astimezone(pytz.utc)
        
        print(f"\nGCAL THREAD: Loop iteration. System Local: {now_local_system_aware.isoformat()}, Derived UTC: {now_utc.isoformat()}") # PRINT 1

        time_min_iso_check = now_utc.isoformat() 
        time_max_iso_check = (now_utc + datetime.timedelta(minutes=GCAL_EVENT_LOOKAHEAD_MINUTES)).isoformat()
        
        try:
            print(f"GCAL API: Attempting to fetch events (UTC timeMin={time_min_iso_check}, UTC timeMax={time_max_iso_check})") # PRINT 2
            events_result = service.events().list(
                calendarId=GCAL_CALENDAR_ID,
                timeMin=time_min_iso_check,
                timeMax=time_max_iso_check,
                maxResults=10,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            items = events_result.get('items', [])
            
            if items: print(f"GCAL API: Successfully fetched {len(items)} events.") # PRINT 3
            else: print("GCAL API: Fetched 0 events for the current window.") # PRINT 3
            
            processed_away_this_cycle = False
            print("GCAL THREAD: Starting event processing loop...") # PRINT 4

            for event_idx, event in enumerate(items): 
                event_id = event['id']
                summary = event.get('summary', 'No Title')
                print(f"GCAL THREAD: Processing event {event_idx+1}/{len(items)}: '{summary}'") # PRINT 5A
                
                start_data = event['start']
                raw_start_str = start_data.get('dateTime', start_data.get('date'))
                event_start_time_utc = parse_event_time(raw_start_str)
                
                if not event_start_time_utc:
                    print(f"GCAL WARN: Parse start time failed for '{summary}'. Skipping.")
                    continue

                time_until_event_starts_seconds = (event_start_time_utc - now_utc).total_seconds()

                event_is_imminent_or_just_started = (
                    time_until_event_starts_seconds <= (GCAL_TRIGGER_WINDOW_MINUTES * 60) and 
                    time_until_event_starts_seconds > (-1 * GCAL_CHECK_INTERVAL_SECONDS - 15)
                )
                
                print(f"GCAL DEBUG EVENT INFO: '{summary}' (ID: {event_id})")
                print(f"GCAL DEBUG EVENT TIME: event_start_utc={event_start_time_utc.isoformat()}, time_until_starts_sec={time_until_event_starts_seconds:.2f}")
                print(f"GCAL DEBUG EVENT COND: imminent_or_started={event_is_imminent_or_just_started}, away_processed={event_id in processed_away_event_ids}, alarm_processed={event_id in processed_alarm_event_ids}")

                # --- [AWAY] Logic ---
                away_match = re.search(GCAL_AWAY_KEYWORD_PATTERN, summary, re.IGNORECASE)
                if away_match and not processed_away_this_cycle:
                    if event_is_imminent_or_just_started:
                        if event_id not in processed_away_event_ids:
                            duration_minutes_str = away_match.group(1)
                            try:
                                if duration_minutes_str: duration_minutes = int(duration_minutes_str)
                                else: duration_minutes = GCAL_AWAY_DEFAULT_DURATION_MINUTES; print(f"GCAL INFO: [AWAY] for '{summary}', default {duration_minutes}m.")
                                if duration_minutes <= 0: print(f"GCAL WARN: Invalid AWAY duration for '{summary}'."); continue
                                print(f"GCAL ACTION: [AWAY] '{summary}' imminent. Timer ({duration_minutes}m).")
                                start_away_timer(duration_minutes, event_id, summary)
                                processed_away_event_ids.add(event_id)
                                processed_away_this_cycle = True 
                            except ValueError: print(f"GCAL ERROR: Parse AWAY duration fail: '{summary}'")
                            except Exception as e: print(f"GCAL ERROR: Processing AWAY '{summary}': {e}")
                        else: print(f"GCAL DEBUG: [AWAY] '{summary}' (ID: {event_id}) already in processed_away_ids.")
                    else: print(f"GCAL DEBUG: [AWAY] '{summary}' (ID: {event_id}) not in activation window for timer.")
                
                # --- [ALARM] Logic (Independent IF) ---
                if GCAL_ALARM_KEYWORD.lower() in summary.lower():
                    if event_is_imminent_or_just_started:
                        if event_id not in processed_alarm_event_ids:
                            print(f"GCAL ALARM: '{summary}' imminent! Triggering buzzer.")
                            if SERIAL_PORT_ARDUINO2: send_command_to_arduino2(f"BUZZER_BEEP:{GCAL_BUZZER_DURATION_MS}")
                            processed_alarm_event_ids.add(event_id)
                            if tb_mqtt_client and tb_mqtt_client.is_connected():
                                tb_payload_alarm = {"calendar_alarm_triggered": True, "event_summary": summary,
                                                    "event_start_time_utc": event_start_time_utc.isoformat(),
                                                    "alarm_triggered_local_time": get_formatted_local_time(),
                                                    "timestamp": int(time.time() * 1000)}
                                try:
                                    tb_mqtt_client.publish("v1/devices/me/telemetry", json.dumps(tb_payload_alarm), qos=1)
                                    print(f"THINGSBOARD: Sent alarm event: {tb_payload_alarm}")
                                except Exception as e: print(f"THINGSBOARD ERROR: Failed to publish alarm: {e}")
                        else: print(f"GCAL DEBUG: [ALARM] '{summary}' (ID: {event_id}) already in processed_alarm_ids.")
                    else: print(f"GCAL DEBUG: [ALARM] '{summary}' (ID: {event_id}) not in activation window for buzzer.")
                print(f"GCAL THREAD: Finished processing event '{summary}'") # PRINT 5B

            print("GCAL THREAD DEBUG: Loop for event processing finished.") # PRINT 6

            # Initial presence publish logic
            with person_at_home_lock: # Lock needed for initial_presence_published_flag as well
                if not initial_presence_published_flag:
                    print("GCAL THREAD DEBUG: Entering initial_presence_published block.") # PRINT 7_PRE
                    if away_timer is None and person_at_home_status == True: # Check current actual status
                        print("GCAL THREAD DEBUG: Condition for initial publish True met.") # PRINT 7A
                        publish_presence_status(True, "Initial state, no active away timer")
                    # else: # If away_timer is active, publish_presence_status(False) was already called
                        # print("GCAL THREAD DEBUG: Initial publish - away_timer active or status already False.") # PRINT 7B
                    initial_presence_published_flag = True # Set flag after first attempt
                    print("GCAL THREAD DEBUG: initial_presence_published_flag set to True.") # PRINT 7C
                # else:
                    # print("GCAL THREAD DEBUG: Skipping initial_presence_published block (already done).") # PRINT 7D
            
            print("GCAL THREAD DEBUG: Starting cleanup of processed_away_event_ids.") # PRINT 8_PRE_AWAY
            if len(processed_away_event_ids) > 20: 
                processed_away_event_ids = set(list(processed_away_event_ids)[-15:])
                print("GCAL THREAD DEBUG: Cleaned up processed_away_event_ids.") # PRINT 8A
            
            print("GCAL THREAD DEBUG: Starting cleanup of processed_alarm_event_ids.") # PRINT 8_PRE_ALARM
            if len(processed_alarm_event_ids) > 20: 
                processed_alarm_event_ids = set(list(processed_alarm_event_ids)[-15:])
                print("GCAL THREAD DEBUG: Cleaned up processed_alarm_event_ids.") # PRINT 8B

            print(f"GCAL THREAD DEBUG: About to call time.sleep({GCAL_CHECK_INTERVAL_SECONDS}).") # PRINT 9

        except HttpError as error:
            print(f'GCAL ERROR: API HttpError: {error}') # PRINT E1
            if error.resp.status in [401, 403]: service = None
            if os.path.exists(GCAL_TOKEN_FILE) and error.resp.status in [401,403]:
                try: os.remove(GCAL_TOKEN_FILE)
                except OSError: pass
        except Exception as e:
            print(f"GCAL ERROR: Unexpected exception in GCal thread's try-block: {e}") # PRINT E2
            import traceback
            traceback.print_exc()
        
        print(f"GCAL THREAD DEBUG: End of try-except block for GCal API. Preparing to sleep for {GCAL_CHECK_INTERVAL_SECONDS}s.") # PRINT F
        time.sleep(GCAL_CHECK_INTERVAL_SECONDS) 
        print("GCAL THREAD DEBUG: Woke up from sleep. Restarting loop.") # PRINT G

# --- Main Execution ---
if __name__ == "__main__":
    print("Starting Edge Device 2 (Calendar: Robust Timed Away & Alarms)...") # Updated title

    edge_mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID_EDGE2)
    edge_mqtt_client.on_connect = on_connect_edge_mqtt
    edge_mqtt_client.on_message = on_message_edge_mqtt
    try: edge_mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60); edge_mqtt_client.loop_start()
    except Exception as e: print(f"EDGE MQTT CRITICAL: Connect fail: {e}")

    tb_mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"tb_edge2_{MQTT_CLIENT_ID_EDGE2}")
    tb_mqtt_client.username_pw_set(THINGSBOARD_ACCESS_TOKEN_EDGE2)
    tb_mqtt_client.on_connect = on_connect_tb
    tb_mqtt_client.on_message = on_message_tb
    try: tb_mqtt_client.connect(THINGSBOARD_HOST, THINGSBOARD_PORT, 60); tb_mqtt_client.loop_start()
    except Exception as e: print(f"THINGSBOARD CRITICAL: Connect fail: {e}")

    if SERIAL_PORT_ARDUINO2:
        threading.Thread(target=read_from_arduino2_thread_func, daemon=True, name="Arduino2Comm").start()
    else:
        print("INFO: SERIAL_PORT_ARDUINO2 not set. Arduino 2 (buzzer) disabled.")

    threading.Thread(target=check_google_calendar_events_thread_func, daemon=True, name="GCalCheck").start()

    print(f"Edge Device 2 running. ALARM: '{GCAL_ALARM_KEYWORD}', AWAY: pattern like '[AWAY:XX]' or '[AWAY]' (default {GCAL_AWAY_DEFAULT_DURATION_MINUTES}m).")
    print(f"Presence MQTT topic: {TOPIC_PRESENCE_STATUS_PUB}")
    print(f"Check interval: {GCAL_CHECK_INTERVAL_SECONDS}s, Lookahead: {GCAL_EVENT_LOOKAHEAD_MINUTES}m, Trigger Window: {GCAL_TRIGGER_WINDOW_MINUTES}m.")
    print("Ctrl+C to exit.")

    try:
        while True: time.sleep(10) 
    except KeyboardInterrupt: print("\nExiting...")
    finally:
        print("Cleaning up...")
        if away_timer: away_timer.cancel(); print("Active AWAY timer cancelled.")
        if arduino2_ser and arduino2_ser.is_open: arduino2_ser.close()
        if edge_mqtt_client and edge_mqtt_client.is_connected(): 
            edge_mqtt_client.loop_stop(timeout=1.0)
            edge_mqtt_client.disconnect()
        if tb_mqtt_client and tb_mqtt_client.is_connected(): 
            tb_mqtt_client.loop_stop(timeout=1.0)
            tb_mqtt_client.disconnect()
        print("Stopped.")