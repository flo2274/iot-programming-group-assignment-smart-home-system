// Arduino1.ino

#include <DHT.h>
#include <Servo.h>

// --- Configuration ---
// DHT11 Sensor
#define DHTPIN 2        // Digital pin connected to the DHT sensor
#define DHTTYPE DHT11   // DHT 11
DHT dht(DHTPIN, DHTTYPE);

// Analog Ambient Light Sensor
#define LIGHT_SENSOR_PIN A0 // Analog pin for LDR/light sensor

// LED
#define LED_PIN 7           // Digital PWM pin for LED

// Servo Motor (Window)
#define SERVO_PIN 5
Servo windowServo;

// DC Motor (Fan) - simple on/off pin
#define FAN_PIN 8

unsigned long lastSensorReadTime = 0;
const long sensorReadInterval = 2000; // Read sensors every 2 seconds

void setup() {
  Serial.begin(9600);

  dht.begin();
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW); // LED off initially

  windowServo.attach(SERVO_PIN);
  windowServo.write(0); // Window closed initially (0 degrees)

  pinMode(FAN_PIN, OUTPUT);
  digitalWrite(FAN_PIN, LOW); // Fan off initially

  Serial.println("Arduino 1 Ready");
}

void loop() {
  // Periodically read sensors and send data
  if (millis() - lastSensorReadTime >= sensorReadInterval) {
    lastSensorReadTime = millis();

    float temperature = dht.readTemperature();
    float humidity = dht.readHumidity();
    int lightLevel = analogRead(LIGHT_SENSOR_PIN);

    if (isnan(temperature) || isnan(humidity)) {
      Serial.println("{\"error\":\"DHT read failed\"}");
    } else {
      String jsonData = "{";
      jsonData += "\"temperature\":" + String(temperature, 1) + ","; // 1 decimal place
      jsonData += "\"humidity\":" + String(humidity, 1) + ",";    // 1 decimal place
      jsonData += "\"light\":" + String(lightLevel);
      jsonData += "}";
      Serial.println(jsonData);
    }
  }

  // Check for incoming commands from Serial (from Edge Device 1)
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim(); // Remove newline characters

    // --- LED Control ---
    if (command.startsWith("LED:")) {
      String state = command.substring(4); // Get state after "LED:"
      if (state == "ON") {
        digitalWrite(LED_PIN, HIGH);
      } else if (state == "OFF") {
        digitalWrite(LED_PIN, LOW);
      }
    } else if (command.startsWith("LED_BRIGHTNESS:")) {
      int brightness = command.substring(15).toInt();
      analogWrite(LED_PIN, constrain(brightness, 0, 255));
    }
    // --- Servo (Window) Control ---
    else if (command.startsWith("WINDOW:")) { // e.g., WINDOW:90 (degrees)
      int angle = command.substring(7).toInt();
      windowServo.write(constrain(angle, 0, 180)); // Servo typically 0-180 degrees
    }
    // --- Fan (DC Motor) Control ---
    else if (command == "FAN_ON") {
      digitalWrite(FAN_PIN, HIGH); // Turn fan on
    } else if (command == "FAN_OFF") {
      digitalWrite(FAN_PIN, LOW);  // Turn fan off
    }
  }
}
