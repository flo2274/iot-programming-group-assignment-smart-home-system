#include <IRremote.h>

const int RECIEVER_PIN = 7;
const int buttonPin = 4;

bool buttonState = false;
bool lastButtonReading = false;
unsigned long lastDebounceTime = 0;
const unsigned long debounceDelay = 50;

void setup() {
  pinMode(buttonPin, INPUT_PULLUP); 
  IrReceiver.begin(RECIEVER_PIN, ENABLE_LED_FEEDBACK);
  Serial.begin(9600);
}

void loop() {
  bool currentReading = !digitalRead(buttonPin); 

  if (currentReading != lastButtonReading) {
    lastDebounceTime = millis();
  }

  if ((millis() - lastDebounceTime) > debounceDelay) {
    if (currentReading && !buttonState) {
      buttonState = true;
      Serial.println("Button toggled");
    }
  }

  if (!currentReading) {
    buttonState = false;
  }

  lastButtonReading = currentReading;

  if (IrReceiver.decode()) {
    unsigned long code = IrReceiver.decodedIRData.decodedRawData;
    switch(code) {
      case 4077715200: // 1 on remote
        Serial.println("1");
        break;
      default:
        break;
    }
    IrReceiver.resume();
  }
}
