// Arduino2.ino

#include <IRremote.hpp> // Or <IRremote.h> depending on your exact library version/setup.
                        // For the global IrReceiver instance, .hpp is often correct for newer versions.

// --- Configuration for Edge Device 2 Communication & Local Actions ---

// IR Receiver (Pin from your example, used for both direct action and sending to Edge2)
#define IR_RECEIVE_PIN 6      // Pin for the IR receiver, AS IN YOUR EXAMPLE.
                              // Ensure this pin does not conflict with others.

// Button (Sends state to Edge2)
#define BUTTON_PIN 2          // Digital pin for the button
int lastButtonStateSent = HIGH; 
int currentButtonReading = HIGH;
int lastSteadyButtonState = HIGH;
unsigned long lastButtonDebounceTime = 0;
unsigned long debounceDelay = 50;

// Main Buzzer (Controlled by Edge2 commands AND your direct IR example logic)
#define MAIN_BUZZER_PIN 3     // Digital pin for the Buzzer. Pin 2 was Button, Pin 6 is now IR.
                              // CHOOSE AN UNUSED DIGITAL PIN!

unsigned long lastIRTimeSentToEdge2 = 0; // Debounce for sending IR codes to Edge2
const unsigned long IR_DEBOUNCE_SERIAL_SEND = 300; // ms

// --- Additional Pins from your IR example for direct LED control ---
// Ensure these pins do not conflict with IR_RECEIVE_PIN, BUTTON_PIN, or MAIN_BUZZER_PIN.
int const yellowLedPin = 4; // Pin for yellow LED
int const redLedPin = 5;    // Pin for red LED (Pin 3 was previously MAIN_BUZZER_PIN, ensure no conflict)
                            // If MAIN_BUZZER_PIN is 3, redLedPin must be different. Let's assume 5 is free.

// --- IR Codes from your example ---
const unsigned long IR_CODE_ALL_OFF = 4278238976UL;
const unsigned long IR_CODE_RED_LED_ON = 4010852096UL;
const unsigned long IR_CODE_BUZZER_ON_IR = 3994140416UL; // Will turn on MAIN_BUZZER_PIN
const unsigned long IR_CODE_YELLOW_LED_ON = 3977428736UL;


void setup() {
  Serial.begin(9600);
  while (!Serial && millis() < 2000); // Wait for Serial, but not forever

  // Initialize pins for functionalities
  pinMode(MAIN_BUZZER_PIN, OUTPUT);
  digitalWrite(MAIN_BUZZER_PIN, LOW); // Buzzer off initially

  pinMode(BUTTON_PIN, INPUT_PULLUP); // Use internal pull-up resistor
  
  // Initialize the global IR receiver, AS PER YOUR EXAMPLE
  // ENABLE_LED_FEEDBACK is optional, it blinks the built-in LED (usually Pin 13) on IR signal.
  // If it causes issues or you don't have an LED on Pin 13, use: IrReceiver.begin(IR_RECEIVE_PIN);
  IrReceiver.begin(IR_RECEIVE_PIN, ENABLE_LED_FEEDBACK); 
  
  Serial.print("IR Receiver setup started on pin "); // Debug
  Serial.println(IR_RECEIVE_PIN);                   // Debug

  // Initialize pins for your direct IR example logic
  pinMode(yellowLedPin, OUTPUT);
  pinMode(redLedPin, OUTPUT);

  digitalWrite(yellowLedPin, LOW); // LEDs off initially
  digitalWrite(redLedPin, LOW);

  Serial.println("{\"status\":\"Arduino 2 Hybrid Ready (Global IrReceiver)\"}");
}

void loop() {
  // --- IR Reception (Using the global IrReceiver instance) ---
  if (IrReceiver.decode()) { // Checks if new data from the global IrReceiver has been decoded
    unsigned long receivedIRCode = IrReceiver.decodedIRData.decodedRawData; // Get raw data as in your example

    // 1. Direct Arduino Actions based on specific IR codes (from your example)
    //    Only act on a new, valid code (not a repeat or zero)
    if (receivedIRCode != 0 && !(IrReceiver.decodedIRData.flags & IRDATA_FLAGS_IS_REPEAT)) {
      // Serial.print("Direct IR Action Check - Code: "); Serial.println(receivedIRCode); // Debug

      if (receivedIRCode == IR_CODE_ALL_OFF) { 
        digitalWrite(redLedPin, LOW);
        digitalWrite(yellowLedPin, LOW);
        digitalWrite(MAIN_BUZZER_PIN, LOW); // Turn off the main buzzer too
        Serial.println("{\"ir_action\":\"LOCAL_ALL_OFF\"}"); // Send confirmation
      } else if (receivedIRCode == IR_CODE_RED_LED_ON) { 
        digitalWrite(redLedPin, HIGH);
        Serial.println("{\"ir_action\":\"LOCAL_RED_LED_ON\"}");
      } else if (receivedIRCode == IR_CODE_BUZZER_ON_IR) { 
        digitalWrite(MAIN_BUZZER_PIN, HIGH); // Use the main buzzer pin
        Serial.println("{\"ir_action\":\"LOCAL_BUZZER_ON\"}");
      } else if (receivedIRCode == IR_CODE_YELLOW_LED_ON) { 
        digitalWrite(yellowLedPin, HIGH);
        Serial.println("{\"ir_action\":\"LOCAL_YELLOW_LED_ON\"}");
      }
    }

    // 2. Send the (filtered) IR code to Edge Device 2 via Serial
    //    Ensure it's not a repeat code and is a valid signal.
    if (!(IrReceiver.decodedIRData.flags & IRDATA_FLAGS_IS_REPEAT) && receivedIRCode != 0) {
      if (millis() - lastIRTimeSentToEdge2 > IR_DEBOUNCE_SERIAL_SEND) {
        String irHexCode = String(receivedIRCode, HEX);
        irHexCode.toUpperCase(); // Consistent casing for Python
        String irDataToEdge2 = "{\"ir_code\":\"0x" + irHexCode + "\"}";
        Serial.println(irDataToEdge2); // Send to Edge2
        lastIRTimeSentToEdge2 = millis();
      }
    }
    IrReceiver.resume(); // Essential: prepare for the next IR code
  }


  // --- Read Button and send to Edge Device 2 (with debounce) ---
  int newButtonReading = digitalRead(BUTTON_PIN);
  if (newButtonReading != lastSteadyButtonState) {
    lastButtonDebounceTime = millis();
  }
  if ((millis() - lastButtonDebounceTime) > debounceDelay) {
    if (newButtonReading != currentButtonReading) { 
      currentButtonReading = newButtonReading;
      if (currentButtonReading != lastButtonStateSent) { 
        // Send 1 for pressed (LOW due to INPUT_PULLUP), 0 for unpressed (HIGH)
        String buttonData = "{\"button_state\":" + String(currentButtonReading == LOW ? 1 : 0) + "}";
        Serial.println(buttonData);
        lastButtonStateSent = currentButtonReading; 
      }
    }
  }
  lastSteadyButtonState = newButtonReading;


  // --- Receive and process commands from Edge Device 2 via Serial ---
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();
    // Serial.print("Arduino 2 RX Command: "); Serial.println(command); // Debug echo

    if (command.startsWith("BUZZER:")) {
      String state = command.substring(7); // Length of "BUZZER:"
      if (state == "ON") {
        digitalWrite(MAIN_BUZZER_PIN, HIGH);
      } else if (state == "OFF") {
        digitalWrite(MAIN_BUZZER_PIN, LOW);
      }
    } else if (command.startsWith("BUZZER_BEEP:")) { // e.g., BUZZER_BEEP:500
        int duration = command.substring(12).toInt(); // Length of "BUZZER_BEEP:"
        if (duration > 0 && duration < 5000) { // Sanity check duration
            digitalWrite(MAIN_BUZZER_PIN, HIGH);
            delay(duration); // Blocking, but acceptable for short beeps
            digitalWrite(MAIN_BUZZER_PIN, LOW);
        }
    }
  }
  // Optional small delay if loop runs too fast, though event-driven parts should be fine.
  // delay(10); 
}