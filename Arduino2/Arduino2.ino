#include <IRremote.hpp> 

#define IR_RECEIVE_PIN 8      
#define BUTTON_PIN 4          
#define MAIN_BUZZER_PIN 2     

const unsigned long IR_CODE_ALARM_OFF = 3125149440; // off btn 

const unsigned long IR_CODE_FAN_ON = 3877175040; // 2 btn
const unsigned long IR_CODE_FAN_OFF = 2707357440; // 3 btn

const unsigned long IR_CODE_WINDOW_ON = 4144561920; // 4 btn  
const unsigned long IR_CODE_WINDOW_OFF = 3810328320; // 5 btn

int lastSteadyButtonState = HIGH; 
int currentButtonReading = HIGH;
unsigned long lastButtonDebounceTime = 0;
unsigned long debounceDelay = 50; 

void setup() {
  Serial.begin(9600);
  while (!Serial && millis() < 2000); 

  pinMode(BUTTON_PIN, INPUT_PULLUP); 
  pinMode(MAIN_BUZZER_PIN, OUTPUT);

  IrReceiver.begin(IR_RECEIVE_PIN, ENABLE_LED_FEEDBACK); 
  Serial.println("{\"status\":\"Arduino 2 Simplified Ready\"}");
}

void loop() {
  int newButtonReading = digitalRead(BUTTON_PIN);

  if (newButtonReading != lastSteadyButtonState) 
  {
    lastButtonDebounceTime = millis(); 
  }

  if ((millis() - lastButtonDebounceTime) > debounceDelay) {
    if (newButtonReading != currentButtonReading) 
    {
      currentButtonReading = newButtonReading;
      if (currentButtonReading == LOW) 
      {
        Serial.println("{\"button_action\":\"TOGGLE_EXTERNAL_LED\"}");
      }
    }
  }
  lastSteadyButtonState = newButtonReading;

  if (IrReceiver.decode()) {
    unsigned long receivedIRCode = IrReceiver.decodedIRData.decodedRawData;
    if (receivedIRCode != 0 && !(IrReceiver.decodedIRData.flags & IRDATA_FLAGS_IS_REPEAT)) 
    {
      if (receivedIRCode == IR_CODE_ALARM_OFF) {
        Serial.println("{\"ir_action\":\"ALARM_OFF_LOCAL_BUZZER\"}");
      }
      if (receivedIRCode == IR_CODE_FAN_ON) 
      {
        Serial.println("{\"ir_action\":\"FAN_ON\"}");
      }
      if (receivedIRCode == IR_CODE_FAN_OFF)
      {
        Serial.println("{\"ir_action\":\"FAN_OFF\"}");
      }
       if (receivedIRCode == IR_CODE_WINDOW_ON) 
      {
        Serial.println("{\"ir_action\":\"WINDOW_OPEN\"}");
      }
       if (receivedIRCode == IR_CODE_WINDOW_OFF) 
      {
        Serial.println("{\"ir_action\":\"WINDOW_CLOSED\"}");
      }
    }
    IrReceiver.resume(); 
  }

  if (Serial.available() > 0) 
  {
    String command = Serial.readStringUntil('\n');
    command.trim(); 
    
    if (command.startsWith("BUZZER:")) 
    {
      String state = command.substring(7); // length of passed in buzzer
      if (state == "ON") 
      {
        digitalWrite(MAIN_BUZZER_PIN, HIGH);
      } 
      else if (state == "OFF") 
      {
        digitalWrite(MAIN_BUZZER_PIN, LOW);
      }
      else if (state == "BEEP2000") 
      {
        digitalWrite(MAIN_BUZZER_PIN, HIGH);
        delay(10000);
        digitalWrite(MAIN_BUZZER_PIN, LOW);
      }
    }
  delay(100);
  }
}