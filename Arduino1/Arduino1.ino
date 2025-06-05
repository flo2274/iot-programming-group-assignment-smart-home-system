#include <DHT.h>
#include <Servo.h>
#include <Stepper.h>

#define DHTPIN 2
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);

#define LIGHT_SENSOR_PIN A0
#define LED_PIN 7

// Servo Motor (Window)
#define SERVO_PIN 5
Servo windowServo;

// Stepper Motor (Fan) - 28BYJ-48 (ULN2003)
const int stepsPerRevolution = 2048;
Stepper fanStepper(stepsPerRevolution, 8, 10, 9, 11); // IN1, IN3, IN2, IN4 for ULN2003

bool fanOn = false; // flag for fan state

unsigned long lastSensorReadTime = 0;
const long sensorReadInterval = 2000; // 2s

void setup() {
  Serial.begin(9600);

  dht.begin();
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  windowServo.attach(SERVO_PIN);
  windowServo.write(0);

  fanStepper.setSpeed(10);  // testing 

  Serial.println("Arduino 1 Ready");
}

void loop() {
  if (millis() - lastSensorReadTime >= sensorReadInterval) {
    lastSensorReadTime = millis();

    float temperature = dht.readTemperature();
    float humidity = dht.readHumidity();
    int lightLevel = analogRead(LIGHT_SENSOR_PIN);

    if (isnan(temperature) || isnan(humidity)) {
      Serial.println("{\"error\":\"DHT read failed\"}");
    } else {
      String jsonData = "{";
      jsonData += "\"temperature\":" + String(temperature, 1) + ",";
      jsonData += "\"humidity\":" + String(humidity, 1) + ",";
      jsonData += "\"light\":" + String(lightLevel);
      jsonData += "}";
      Serial.println(jsonData);
    }
  }

  if (fanOn) {
    fanStepper.step(1);
  }

  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    // LED
    if (command.startsWith("LED:")) {
      String state = command.substring(4);
      digitalWrite(LED_PIN, (state == "ON") ? HIGH : LOW);
    } 
    else if (command.startsWith("LED_BRIGHTNESS:")) {
      int brightness = command.substring(15).toInt();
      analogWrite(LED_PIN, constrain(brightness, 0, 255));
    }
    // Window
    else if (command.startsWith("WINDOW:")) {
      int angle = command.substring(7).toInt();
      windowServo.write(constrain(angle, 0, 180));
    }
    // Fan
    else if (command == "FAN_ON") {
      fanOn = true;
      Serial.println("{\"info\":\"Fan turned ON\"}");
    } 
    else if (command == "FAN_OFF") {
      fanOn = false;
      Serial.println("{\"info\":\"Fan turned OFF\"}");
    }
  }
}