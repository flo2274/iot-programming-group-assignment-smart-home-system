const int greenLedPin = 3;  //pwm
const int redLedPin = 7;    
const int motionPin = 8;    
const int lightSensorPin = A5; 

const int LOW_LIGHT = 55;     
const int HIGH_LIGHT = 155;   

volatile bool motionDetected = false;
unsigned long lastMotionTime = 0;
const unsigned long motionTimeout = 30000;  // 30 secs

void setup() {
  Serial.begin(9600);
  pinMode(greenLedPin, OUTPUT);
  pinMode(redLedPin, OUTPUT);
  pinMode(motionPin, INPUT);

}

void loop() {
  int lightValue = analogRead(lightSensorPin);
  int motionValue = digitalRead(motionPin);
  
  if (motionValue == HIGH) {  
    motionDetected = true;
    lastMotionTime = millis();
    Serial.println("Motion Detected");
  }
  
  if (motionDetected && (millis() - lastMotionTime > motionTimeout)) {
    motionDetected = false;
    Serial.println("Motion timeout");
  }
  
  if (motionDetected) {
    if (lightValue < LOW_LIGHT) {
      analogWrite(greenLedPin, 255);
      digitalWrite(redLedPin, LOW);
      Serial.print("Dark environment, LED at full brightness. Light value: ");
      Serial.println(lightValue);
    } 
    else if (lightValue < HIGH_LIGHT) {
      // Moderate light
      int brightness = map(lightValue, LOW_LIGHT, HIGH_LIGHT, 255, 0);
      analogWrite(greenLedPin, brightness);
      digitalWrite(redLedPin, LOW);
      Serial.print("LED at variable brightness: ");
      Serial.print(brightness);
      Serial.print(", Light value: ");
      Serial.println(lightValue);
    } 
    else {
      // Bright light
      analogWrite(greenLedPin, 0);
      digitalWrite(redLedPin, HIGH);
      Serial.print("Bright environment, LED off. Light value: ");
      Serial.println(lightValue);
    }
  } 
  else {
    // No motion detected
    analogWrite(greenLedPin, 0);
    digitalWrite(redLedPin, HIGH);
    Serial.println("No motion detected, LED off");
  }

  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command == "xxxxx") {
      digitalWrite(, HIGH);
    }
    else if (command == "xxxxxx") {
      digitalWrite(], LOW);
    }
  }
  
  delay(300);  
}