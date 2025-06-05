import serial
import time
import json
import threading
import paho.mqtt.client as mqtt
import os
import re 

import datetime
import pytz 
from dateutil import parser as dateutil_parser
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SERIAL_PORT_ARDUINO2 = "/dev/cu.usbmodem101"
BAUD_RATE = 9600
MQTT_BROKER_HOST = "test.mosquitto.org"
MQTT_BROKER_PORT = 1883
MQTT_CLIENT_ID_EDGE2 = "edge2-gcal-simplified" 

TOPIC_PREFIX = "iot_project/test" 
CALENDAR_PUB = f"{TOPIC_PREFIX}/home/calendar_presence"
CMD_SUB = f"{TOPIC_PREFIX}/{MQTT_CLIENT_ID_EDGE2}/arduino2/cmd"

LED_TOGGLE_PUB = f"{TOPIC_PREFIX}/edge2/external_led/toggle_request" # For Edge Device 1
IR_EVENT_PUB = f"{TOPIC_PREFIX}/edge2/arduino2/ir_event"

THINGSBOARD_HOST = "mqtt.thingsboard.cloud"
THINGSBOARD_PORT = 1883
THINGSBOARD_ACCESS_TOKEN_EDGE2 = "rj6t94nd52Gk2vFrXXDO" # ehhhh

GCAL_SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
GCAL_CREDENTIALS_FILE = 'credentials.json'
GCAL_TOKEN_FILE = 'token.json'
GCAL_CALENDAR_ID = 'primary'

GCAL_ALARM_EVENT_KEYWORD = '[ALARM]'
GCAL_AWAY_EVENT_PATTERN = r"\[AWAY(?:[:\s](\d+))?\]"
GCAL_AWAY_DEFAULT_DURATION_MINUTES = 30

GCAL_CHECK_INTERVAL_SECONDS = 30
GCAL_EVENT_API_LOOKAHEAD_MINUTES = 10 
GCAL_EVENT_TRIGGER_WINDOW_MINUTES = 2
GCAL_PAST_EVENT_TOLERANCE_SECONDS = GCAL_CHECK_INTERVAL_SECONDS + 15 
GCAL_BUZZER_DURATION_MS = 2000

processed_event_actions = {} 

is_person_at_home_by_calendar = True
away_timer_object = None
presence_lock = threading.RLock()

gcal_service_instance = None
gcal_service_init_lock = threading.Lock()
mqtt_edge_client = None
mqtt_tb_client = None
arduino2_serial_connection = None
arduino2_serial_lock = threading.Lock()
is_initial_presence_published = False

def get_current_local_time_formatted():
    return datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')

def publish_to_thingsboard(payload_values, event_timestamp_utc=None):
    if not mqtt_tb_client or not mqtt_tb_client.is_connected(): return
    telemetry_ts = int(event_timestamp_utc.timestamp() * 1000) if event_timestamp_utc else int(time.time() * 1000)
    telemetry_package = {"ts": telemetry_ts, "values": payload_values}
    try:
        mqtt_tb_client.publish("v1/devices/me/telemetry", json.dumps(telemetry_package), qos=1)
        print(f"THINGSBOARD PUBLISH: {telemetry_package}")
    except Exception as e:
        print(f"THINGSBOARD ERROR: Failed to publish telemetry: {e}")

def update_calendar_presence_status(is_at_home, reason_log=""):
    global is_person_at_home_by_calendar, is_initial_presence_published
    
    actual_publish_needed = False
    with presence_lock:
        if not is_initial_presence_published: 
            actual_publish_needed = True
            is_initial_presence_published = True 
        elif is_person_at_home_by_calendar != is_at_home: 
            actual_publish_needed = True
        
        if actual_publish_needed:
            is_person_at_home_by_calendar = is_at_home 
            print(f"PRESENCE STATUS UPDATE: person_at_home_by_calendar = {is_at_home}. Reason: {reason_log}")
    
    if not actual_publish_needed: return

    edge_payload = {"person_at_home": is_at_home}
    if mqtt_edge_client and mqtt_edge_client.is_connected():
        print(f"MQTT EDGE: Publishing presence status: {edge_payload}")
        try: mqtt_edge_client.publish(CALENDAR_PUB, json.dumps(edge_payload), qos=1, retain=True)
        except Exception as e: print(f"MQTT EDGE ERROR: Failed to publish presence: {e}")

    tb_payload_values = {
        "overall_calendar_presence": is_at_home,
        "presence_last_reason": reason_log
    }
    publish_to_thingsboard(tb_payload_values) 

def on_away_timer_expired():
    global away_timer_object, current_away_event_summary_for_log
    with presence_lock:
        reason = f"AWAY timer expired for: '{current_away_event_summary_for_log}'"
        print(f"PRESENCE CONTROL: {reason}. Setting person AT HOME.")
        update_calendar_presence_status(True, reason)
        away_timer_object = None
        current_away_event_summary_for_log = None

def start_away_mode(duration_minutes, event_data):
    global away_timer_object, current_away_event_summary_for_log
    
    summary = event_data.get('summary', 'No Title')
    event_start_utc = parse_gcal_event_time_to_utc(event_data['start'].get('dateTime', event_data['start'].get('date')))
    if not event_start_utc: print(f"GCAL ERROR: Could not parse start time in start_away_mode for '{summary}'."); return

    with presence_lock:
        if away_timer_object:
            print(f"PRESENCE CONTROL: Cancelling existing AWAY timer for '{current_away_event_summary_for_log}' by new event '{summary}'.")
            away_timer_object.cancel()

        current_away_event_summary_for_log = summary 
        reason = f"'{summary}' [AWAY:{duration_minutes}m] triggered."
        print(f"PRESENCE CONTROL: {reason}. Setting person NOT AT HOME for {duration_minutes} minutes.")
        update_calendar_presence_status(False, reason)

        system_local_tz = datetime.datetime.now().astimezone().tzinfo
        tb_payload_away_values = {
            "calendar_away_summary": summary, 
            "calendar_away_duration_minutes": duration_minutes,
            "calendar_away_start_local": event_start_utc.astimezone(system_local_tz).strftime('%H:%M (%d.%m)'),
        }
        publish_to_thingsboard(tb_payload_away_values, event_timestamp_utc=event_start_utc) 

        away_duration_seconds = duration_minutes * 60
        away_timer_object = threading.Timer(away_duration_seconds, on_away_timer_expired)
        away_timer_object.daemon = True
        away_timer_object.start()

def trigger_alarm_action(event_data):
    summary = event_data.get('summary', 'No Title')
    event_start_utc = parse_gcal_event_time_to_utc(event_data['start'].get('dateTime', event_data['start'].get('date')))
    if not event_start_utc: print(f"GCAL ERROR: Could not parse start time in trigger_alarm_action for '{summary}'."); return
    
    system_local_tz = datetime.datetime.now().astimezone().tzinfo
    local_event_start_formatted_simple = event_start_utc.astimezone(system_local_tz).strftime('%H:%M (%d.%m)')

    print(f"ALARM ACTION: Event '{summary}' (planned start: {local_event_start_formatted_simple}) triggered!")
    if SERIAL_PORT_ARDUINO2:
        send_command_to_arduino2("BUZZER:BEEP2000")

    tb_payload_alarm_values = {
        "calendar_alarm_summary": summary, # Title of the ALARM event
        "calendar_alarm_start_local": local_event_start_formatted_simple,
    }
    publish_to_thingsboard(tb_payload_alarm_values, event_timestamp_utc=event_start_utc) 

def on_connect_mqtt_edge(client, return_code):
    if return_code == 0: print(f"MQTT EDGE: Connected to {MQTT_BROKER_HOST}."); client.subscribe(CMD_SUB)
    else: print(f"MQTT EDGE ERROR: Connection failed, return_code {return_code}")

def on_message_mqtt_edge(msg):
    payload_str = msg.payload.decode('utf-8'); 
    try:
        data = json.loads(payload_str)
        if msg.topic == CMD_SUB:
            actuator = data.get("actuator","").upper()
            value = str(data.get("value","")).upper()

            if actuator == "BUZZER":
                if value == "BEEP":
                    duration = data.get("duration", GCAL_BUZZER_DURATION_MS)
                    print(f"MQTT EDGE CMD (Edge2 for Arduino2): Buzzer BEEP for {duration}ms. Arduino2 uses fixed BEEP2000.")
                    if "duration" in data:
                        if SERIAL_PORT_ARDUINO2: send_command_to_arduino2(f"BUZZER_BEEP:{duration}")
                    else:
                        if SERIAL_PORT_ARDUINO2: send_command_to_arduino2("BUZZER:BEEP2000")
                elif value == "ON":
                    print(f"MQTT EDGE CMD (Edge2 for Arduino2): Buzzer ON.")
                    if SERIAL_PORT_ARDUINO2: send_command_to_arduino2("BUZZER:ON")
                elif value == "OFF":
                    print(f"MQTT EDGE CMD (Edge2 for Arduino2): Buzzer OFF.")
                    if SERIAL_PORT_ARDUINO2: send_command_to_arduino2("BUZZER:OFF")
                else:
                    print(f"MQTT EDGE CMD (Edge2 for Arduino2): Unknown BUZZER value '{value}'.")
            else:
                print(f"MQTT EDGE CMD (Edge2 for Arduino2): Unknown actuator '{actuator}'.")

    except Exception as e: print(f"MQTT EDGE ERROR (Edge2): Processing msg: {e}")

def on_connect_mqtt_tb(return_code):
    if return_code == 0: print(f"MQTT TB: Connected to {THINGSBOARD_HOST}.")
    else: print(f"MQTT TB ERROR: Connection failed, return_code {return_code}")

def connect_arduino2():
    global arduino2_serial_connection
    if not SERIAL_PORT_ARDUINO2: 
        return False
    try:
        if arduino2_serial_connection and arduino2_serial_connection.is_open: 
            return True
        if arduino2_serial_connection: 
            arduino2_serial_connection.close()
        arduino2_serial_connection = serial.Serial(SERIAL_PORT_ARDUINO2, BAUD_RATE, timeout=1)
        time.sleep(2); 
        arduino2_serial_connection.flushInput(); 
        return True
    except Exception as e: 
        arduino2_serial_connection = None; 
        return False

def send_command_to_arduino2(command):
    if not arduino2_serial_connection or not arduino2_serial_connection.is_open: return
    with arduino2_serial_lock:
        try: arduino2_serial_connection.write((command + '\n').encode('utf-8'))
        except Exception as e: print(f"ARDUINO2 SERIAL ERROR: Write failed for '{command}': {e}")

def get_gcal_service():
    global gcal_service_instance
    with gcal_service_init_lock:
        if gcal_service_instance: return gcal_service_instance
        creds = None
        if os.path.exists(GCAL_TOKEN_FILE):
            try: creds = Credentials.from_authorized_user_file(GCAL_TOKEN_FILE, GCAL_SCOPES)
            except Exception as e: print(f"GCAL AUTH WARN: Load token failed: {e}"); creds = None
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try: print("GCAL AUTH: Refreshing token..."); creds.refresh(Request())
                except Exception as e:
                    print(f"GCAL AUTH ERROR: Refresh failed: {e}. Re-auth needed.")
                    if os.path.exists(GCAL_TOKEN_FILE):
                        try: os.remove(GCAL_TOKEN_FILE)
                        except OSError: pass
                    creds = None
            if not creds: 
                if not os.path.exists(GCAL_CREDENTIALS_FILE):
                    print(f"GCAL CRITICAL: Credentials file '{GCAL_CREDENTIALS_FILE}' missing."); return None
                try:
                    print("GCAL AUTH: Running authorisation flow...");
                    flow = InstalledAppFlow.from_client_secrets_file(GCAL_CREDENTIALS_FILE, GCAL_SCOPES)
                    creds = flow.run_local_server(port=0)
                except Exception as e: 
                    print(f"GCAL AUTH ERROR: Flow failed: {e}"); 
                    return None
            if creds: 
                try:
                    with open(GCAL_TOKEN_FILE, 'w') as token_file: token_file.write(creds.to_json())
                    print(f"GCAL AUTH: Token saved.")
                except Exception as e: print(f"GCAL AUTH ERROR: Token save failed: {e}")
        if creds and creds.valid:
            try:
                gcal_service_instance = build('calendar', 'v3', credentials=creds)
                print("GCAL: Service created successfully."); 
                return gcal_service_instance
            except Exception as e: 
                print(f"GCAL ERROR: Build service failed: {e}"); 
                return None
        else: 
            print("GCAL: No valid credentials obtained."); 
            return None

def parse_gcal_event_time_to_utc(time_data_str):
    try:
        dt_obj = dateutil_parser.isoparse(time_data_str)
        return dt_obj.astimezone(pytz.utc) if dt_obj.tzinfo else pytz.utc.localize(dt_obj)
    except ValueError: 
        return None

def read_from_arduino2():
    global arduino2_serial_connection, mqtt_edge_client, mqtt_tb_client
    
    if not SERIAL_PORT_ARDUINO2:
        print("ARDUINO2 SERIAL: Port not configured. Reader thread will not start.")
        return

    print("ARDUINO2 SERIAL: Reader thread started.")

    while True:
        if not arduino2_serial_connection or not arduino2_serial_connection.is_open:
            print("ARDUINO2 SERIAL: Connection lost or not open. Attempting to connect/reconnect...")
            if not connect_arduino2(): # connect_arduino2() attempts to open/reopen
                print("ARDUINO2 SERIAL: Connection attempt failed. Retrying in 5s.")
                time.sleep(5) 
                continue
            else:
                print("ARDUINO2 SERIAL: Successfully connected/reconnected to Arduino 2.")
                time.sleep(2) 
        
        try:
            if arduino2_serial_connection.in_waiting > 0:
                line_bytes = b'' 
                try:
                    line_bytes = arduino2_serial_connection.readline()
                    line = line_bytes.decode('utf-8', errors='ignore').strip()
                except serial.SerialException as ser_read_err:
                    print(f"ARDUINO2 SERIAL READ EXCEPTION: {ser_read_err}. Closing port and retrying connection.")
                    if arduino2_serial_connection:
                        try: arduino2_serial_connection.close()
                        except: pass
                    arduino2_serial_connection = None
                    time.sleep(5)
                    continue 
                except UnicodeDecodeError as e_unicode:
                    print(f"ARDUINO2 SERIAL ERROR: UnicodeDecodeError - {e_unicode}. Line was: {line_bytes!r}")
                    continue 
                if not line:
                    time.sleep(0.01) 
                    continue
                print(f"ARDUINO2: {line}") 

                try:
                    data = json.loads(line)

                    if "status" in data and data["status"] == "Arduino 2 Simplified Ready":
                        print(f"ARDUINO2 HANDSHAKE: Received ready signal: {data}")
                        publish_to_thingsboard({"arduino2_status": "Ready", "arduino2_last_seen_local": get_current_local_time_formatted()})

                    elif "button_action" in data:
                        if data["button_action"] == "TOGGLE_EXTERNAL_LED":
                            print("ARDUINO2 EVENT: Button pressed for external LED toggle.")
                            action_time_utc = datetime.datetime.now(pytz.utc)
                            
                            if mqtt_edge_client and mqtt_edge_client.is_connected():
                                ext_led_payload = {"request_soureturn_codee": MQTT_CLIENT_ID_EDGE2, "timestamp_utc": action_time_utc.isoformat()}
                                try:
                                    mqtt_edge_client.publish(LED_TOGGLE_PUB, json.dumps(ext_led_payload), qos=1)
                                    print(f"MQTT EDGE: Published external LED toggle request to {LED_TOGGLE_PUB}: {ext_led_payload}")
                                except Exception as e_mqtt_pub:
                                    print(f"MQTT EDGE ERROR: Failed to publish ext LED toggle: {e_mqtt_pub}")
                            else:
                                print("MQTT EDGE WARN: Not connected, cannot publish external LED toggle request.")
                            
                            publish_to_thingsboard({"external_led_toggle_requested": True}, event_timestamp_utc=action_time_utc)
                    
                    elif "ir_action" in data:
                        action_time_utc = datetime.datetime.now(pytz.utc) 
                        ir_action_value = data["ir_action"]
                        ir_event_for_edge1 = None
                        event_type_str = "UNKNOWN_IR_ACTION"

                        if ir_action_value == "ALARM_OFF_LOCAL_BUZZER":
                            print("ARDUINO2 EVENT: IR signal received for ALARM OFF LOCAL BUZZER.")
                            event_type_str = "ALARM_OFF"
                            publish_to_thingsboard({"local_buzzer_ir_trigger": "OFF_REQUESTED"}, event_timestamp_utc=action_time_utc)
                            
                        elif ir_action_value == "FAN_ON":
                            print("ARDUINO2 EVENT: IR signal received for FAN ON.")
                            event_type_str = "FAN_ON"
                            publish_to_thingsboard({"fan_ir_trigger": "ON_REQUESTED"}, event_timestamp_utc=action_time_utc)
                        
                        elif ir_action_value == "FAN_OFF":
                            print("ARDUINO2 EVENT: IR signal received for FAN OFF.")
                            event_type_str = "FAN_OFF"
                            publish_to_thingsboard({"fan_ir_trigger": "OFF_REQUESTED"}, event_timestamp_utc=action_time_utc)

                        elif ir_action_value == "WINDOW_OPEN":
                            print("ARDUINO2 EVENT: IR signal received for WINDOW OPEN.")
                            event_type_str = "WINDOW_OPEN"
                            publish_to_thingsboard({"window_ir_trigger": "OPEN_REQUESTED"}, event_timestamp_utc=action_time_utc)

                        elif ir_action_value == "WINDOW_CLOSED":
                            print("ARDUINO2 EVENT: IR signal received for WINDOW CLOSED.")
                            event_type_str = "WINDOW_CLOSED"
                            publish_to_thingsboard({"window_ir_trigger": "CLOSED_REQUESTED"}, event_timestamp_utc=action_time_utc)
                        
                        if event_type_str != "UNKNOWN_IR_ACTION":
                            ir_event_for_edge1 = {"ir_event_type": event_type_str, "soureturn_codee_device": MQTT_CLIENT_ID_EDGE2, "timestamp_utc": action_time_utc.isoformat()}
                        
                        if ir_event_for_edge1 and mqtt_edge_client and mqtt_edge_client.is_connected():
                            try:
                                mqtt_edge_client.publish(IR_EVENT_PUB, json.dumps(ir_event_for_edge1), qos=1)
                                print(f"MQTT EDGE: Published IR event to {IR_EVENT_PUB}: {ir_event_for_edge1}")
                            except Exception as e_mqtt_pub_ir:
                                print(f"MQTT EDGE ERROR: Failed to publish IR event: {e_mqtt_pub_ir}")
                        elif ir_event_for_edge1:
                             print("MQTT EDGE WARN: Not connected, cannot publish IR event.")

                except json.JSONDecodeError:
                    print(f"ARDUINO2 SERIAL JSON DECODE ERROR: for line: {line}")
                except Exception as e_proc:
                    print(f"ARDUINO2 SERIAL ERROR: Processing line '{line}': {e_proc}")

            else: # No data in waiting
                time.sleep(0.03) 

        except serial.SerialException as e_ser_outer:
            print(f"ARDUINO2 SERIAL CRITICAL: Outer loop SerialException: {e_ser_outer}. Closing port.")
            if arduino2_serial_connection:
                try: arduino2_serial_connection.close()
                except: pass 
            arduino2_serial_connection = None 
            print("ARDUINO2 SERIAL: Will attempt to reconnect in the next loop iteration.")
            time.sleep(5) 
        except Exception as e_outer_loop:
            print(f"ARDUINO2 SERIAL CRITICAL: Unexpected outer loop error: {e_outer_loop}")
            time.sleep(5)
        
def calendar_event_check_loop():
    global processed_event_actions, is_initial_presence_published
    
    print("GCAL THREAD: Event check loop started.")
    print("GCAL THREAD: Initial 5s delay for MQTT client connections...")
    time.sleep(5)
    print("GCAL THREAD: Initial delay finished.")

    service = get_gcal_service()
    if not service: print("GCAL CRITICAL: GCal service failed to initialize for event loop.")

    loop_count = 0
    while True:
        loop_count += 1
        if not service:
            service = get_gcal_service()
            if not service:
                print(f"GCAL WARN [{loop_count}]: No GCal service. Retrying in {GCAL_CHECK_INTERVAL_SECONDS * 2}s.")
                time.sleep(GCAL_CHECK_INTERVAL_SECONDS * 2); continue
        
        now_local = datetime.datetime.now().astimezone()
        now_utc = now_local.astimezone(pytz.utc)
        time_min_utc_iso = now_utc.isoformat() 
        time_max_utc_iso = (now_utc + datetime.timedelta(minutes=GCAL_EVENT_API_LOOKAHEAD_MINUTES)).isoformat()
        
        try:
            events_result = service.events().list(
                calendarId=GCAL_CALENDAR_ID, timeMin=time_min_utc_iso, timeMax=time_max_utc_iso,
                maxResults=10, singleEvents=True, orderBy='startTime'
            ).execute()
            calendar_items = events_result.get('items', [])
            
            with presence_lock:
                if not is_initial_presence_published:
                    if away_timer_object is None:
                        update_calendar_presence_status(True, "Initial system state")
                    is_initial_presence_published = True

            active_away_event_processed_this_cycle = False

            for event_data in calendar_items:
                event_id = event_data['id']
                summary = event_data.get('summary', 'No Title')
                raw_start_str = event_data['start'].get('dateTime', event_data['start'].get('date'))
                event_start_utc = parse_gcal_event_time_to_utc(raw_start_str)
                
                if not event_start_utc: continue

                seconds_until_event_starts = (event_start_utc - now_utc).total_seconds()
                is_event_in_action_window = (
                    seconds_until_event_starts <= (GCAL_EVENT_TRIGGER_WINDOW_MINUTES * 60) and 
                    seconds_until_event_starts > (-1 * GCAL_PAST_EVENT_TOLERANCE_SECONDS)
                )
                
                away_match = re.seareturn_codeh(GCAL_AWAY_EVENT_PATTERN, summary, re.IGNORECASE)
                if away_match and not active_away_event_processed_this_cycle:
                    if is_event_in_action_window:
                        if processed_event_actions.get(event_id) != event_start_utc: # Check if this specific instance was processed
                            duration_str = away_match.group(1)
                            try:
                                duration_m = int(duration_str) if duration_str else GCAL_AWAY_DEFAULT_DURATION_MINUTES
                                if duration_m <= 0: print(f"GCAL WARN: Invalid AWAY duration for '{summary}'."); continue
                                print(f"GCAL ACTION: [AWAY] Event '{summary}' activating. Duration: {duration_m}m.")
                                start_away_mode(duration_m, event_data)
                                processed_event_actions[event_id] = event_start_utc # Mark this event instance (ID + start_time)
                                active_away_event_processed_this_cycle = True
                            except ValueError: print(f"GCAL ERROR: Parse AWAY duration for '{summary}'.")
                            except Exception as e: print(f"GCAL ERROR: Processing AWAY '{summary}': {e}")
                
                if GCAL_ALARM_EVENT_KEYWORD.lower() in summary.lower():
                    if is_event_in_action_window:
                        if processed_event_actions.get(event_id) != event_start_utc: # Check if this specific instance was processed
                            print(f"GCAL ACTION: [ALARM] Event '{summary}' activating.")
                            trigger_alarm_action(event_data)
                            processed_event_actions[event_id] = event_start_utc # Mark this event instance (ID + start_time)

            cutoff_time_for_cleanup = now_utc - datetime.timedelta(hours=6) 
            processed_event_actions = {
                ev_id: start_t for ev_id, start_t in processed_event_actions.items() 
                if start_t > cutoff_time_for_cleanup
            }

        except HttpError as error:
            print(f'GCAL ERROR Loop [{loop_count}]: API HttpError: {error}')
            if error.resp.status in [401, 403]: service = None
            if os.path.exists(GCAL_TOKEN_FILE) and error.resp.status in [401,403]:
                try: os.remove(GCAL_TOKEN_FILE)
                except OSError: 
                    pass
        except Exception as e:
            print(f"GCAL ERROR Loop [{loop_count}]: Unexpected: {e}"); import traceback; traceback.print_exc()
        
        time.sleep(GCAL_CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    print("Starting Edge Device 2 (Calendar: Simplified & Clearer TB)...")

    mqtt_edge_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID_EDGE2)
    mqtt_edge_client.on_connect = on_connect_mqtt_edge
    mqtt_edge_client.on_message = on_message_mqtt_edge
    try: mqtt_edge_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60); mqtt_edge_client.loop_start()
    except Exception as e: print(f"MQTT EDGE CRITICAL: Connect fail: {e}")

    mqtt_tb_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"tb_edge2_{MQTT_CLIENT_ID_EDGE2}")
    mqtt_tb_client.username_pw_set(THINGSBOARD_ACCESS_TOKEN_EDGE2)
    mqtt_tb_client.on_connect = on_connect_mqtt_tb
    try: mqtt_tb_client.connect(THINGSBOARD_HOST, THINGSBOARD_PORT, 60); mqtt_tb_client.loop_start()
    except Exception as e: print(f"MQTT TB CRITICAL: Connect fail: {e}")

    if SERIAL_PORT_ARDUINO2: 
        threading.Thread(target=read_from_arduino2, daemon=True, name="Arduino2Reader").start() 

    threading.Thread(target=calendar_event_check_loop, daemon=True, name="GCalEventCheckLoop").start()

    print(f"Edge Device 2 running. Keywords: ALARM='{GCAL_ALARM_EVENT_KEYWORD}', AWAY='{GCAL_AWAY_EVENT_PATTERN}'.")
    print(f"Config: Interval={GCAL_CHECK_INTERVAL_SECONDS}s, API Lookahead={GCAL_EVENT_API_LOOKAHEAD_MINUTES}m, Trigger Window={GCAL_EVENT_TRIGGER_WINDOW_MINUTES}m.")
    print("Ctrl+C to exit.")

    try:
        while True: time.sleep(10) 
    except KeyboardInterrupt: print("\nExiting...")
    finally:
        print("Cleaning up resoureturn_codees...")
        if away_timer_object: away_timer_object.cancel(); print("Active AWAY timer cancelled.")
        if arduino2_serial_connection and arduino2_serial_connection.is_open: arduino2_serial_connection.close()
        
        if mqtt_edge_client and mqtt_edge_client.is_connected(): 
            mqtt_edge_client.loop_stop(timeout=1.0); mqtt_edge_client.disconnect()
        if mqtt_tb_client and mqtt_tb_client.is_connected(): 
            mqtt_tb_client.loop_stop(timeout=1.0); mqtt_tb_client.disconnect()
        print("Edge Device 2 stopped.")