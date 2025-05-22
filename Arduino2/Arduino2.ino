// Arduino2.ino

#include <IRremote.h> // For v3.x. If using v4.x+, include IRRemote.hpp and use IRrecv irReceiver(pin);

// --- Configuration ---
// IR Receiver
#define IR_RECEIVE_PIN 3
// For IRremote v3.x:
IRrecv irrecv(IR_RECEIVE_PIN);
decode_results results;
// For IRremote v4.x+
// IRrecv irReceiver(IR_RECEIVE_PIN);
// decode_results results; // results object is usually part of irReceiver in v4

// Button
#define BUTTON_PIN 2      // Digital pin for button
int lastButtonState = HIGH; // Assuming INPUT_PULLUP, so HIGH is unpressed
unsigned long lastButtonDebounceTime = 0;
unsigned long debounceDelay = 50;

// Buzzer
#define BUZZER_PIN 6      // Digital pin for Buzzer

unsigned long lastIRTime = 0; // To avoid flooding with repeat codes

void setup() {
  Serial.begin(9600);

  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW); // Buzzer off initially

  pinMode(BUTTON_PIN, INPUT_PULLUP); // Use internal pull-up resistor

  // For IRremote v3.x:
  irrecv.enableIRIn();
  // For IRremote v4.x+:
  // irReceiver.enableIRIn(); // or irReceiver.begin();

  Serial.println("Arduino 2 Ready");
}

void loop() {
  // --- Read IR Sensor ---
  // For IRremote v3.x:
  if (irrecv.decode(&results)) {
    if (results.decode_type != UNKNOWN && results.value != 0xFFFFFFFF && results.value != 0x0 ) { // Filter out repeats or invalid
        if (millis() - lastIRTime > 300) { // Send IR code if it's been a bit since the last one
            String irData = "{\"ir_code\":\"0x" + String(results.value, HEX) + "\"}";
            Serial.println(irData);
            lastIRTime = millis();
        }
    }
    irrecv.resume(); // Receive the next value
  }
  // For IRremote v4.x+:
  // if (irReceiver.decode()) {
  //   if (irReceiver.decodedIRData.decodedRawData != 0 && irReceiver.decodedIRData.protocol != UNKNOWN) { // Check for valid data
  //     if (millis() - lastIRTime > 300) {
  //       String irData = "{\"ir_code\":\"0x" + String(irReceiver.decodedIRData.decodedRawData, HEX) + "\"}"; // May need to use .command or specific protocol data
  //       Serial.println(irData);
  //       lastIRTime = millis();
  //     }
  //   }
  //   irReceiver.resume();
  // }


  // --- Read Button (with basic debounce) ---
  int reading = digitalRead(BUTTON_PIN);
  if (reading != lastButtonState) {
    lastButtonDebounceTime = millis(); // Reset the debounce timer
  }

  if ((millis() - lastButtonDebounceTime) > debounceDelay) {
    // If the button state has been stable for longer than the debounce delay
    if (reading != lastButtonState) { // Check if it's actually different from last sent state
      lastButtonState = reading; // Update the last known state
      String buttonData = "{\"button_state\":" + String(lastButtonState == LOW ? 1 : 0) + "}"; // 1 for pressed (LOW), 0 for unpressed (HIGH)
      Serial.println(buttonData);
    }
  }
  // Update lastButtonState for the next comparison in the debounce logic
  // This line should be here if you want to continuously check for changes,
  // but it was part of the if condition above.
  // If you only want to send on change after debounce, the above structure is okay.
  // For continuous check, this would be `lastButtonState = reading;` outside the debounce if.
  // Let's stick to sending only on confirmed change after debounce.


  // --- Check for incoming commands from Serial (from Edge Device 2) ---
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command.startsWith("BUZZER:")) {
      String state = command.substring(7);
      if (state == "ON") {
        digitalWrite(BUZZER_PIN, HIGH);
      } else if (state == "OFF") {
        digitalWrite(BUZZER_PIN, LOW);
      }
    } else if (command.startsWith("BUZZER_BEEP:")) { // e.g., BUZZER_BEEP:500 (milliseconds)
        int duration = command.substring(12).toInt();
        if (duration > 0) {
            digitalWrite(BUZZER_PIN, HIGH);
            delay(duration);
            digitalWrite(BUZZER_PIN, LOW);
        }
    }
  }
}