// Arduino1.ino

#include <DHT.h>
#include <Servo.h>
#include <Stepper.h>

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

// Stepper Motor (Fan - e.g., 28BYJ-48 with ULN2003 driver)
const int stepsPerRevolution = 2048; // Change for your motor
// Initialize the stepper library on pins 5, 6, 7, 8:
Stepper fanStepper(stepsPerRevolution, 5, 6, 7, 8); // IN1, IN3, IN2, IN4 for ULN2003

unsigned long lastSensorReadTime = 0;
const long sensorReadInterval = 2000; // Read sensors every 2 seconds

void setup() {
  Serial.begin(9600);

  dht.begin();
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW); // LED off initially

  windowServo.attach(SERVO_PIN);
  windowServo.write(0); // Window closed initially (0 degrees)

  fanStepper.setSpeed(10); // Default fan speed

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
    // --- Stepper (Fan) Control ---
    else if (command.startsWith("FAN_SPEED:")) { // e.g., FAN_SPEED:15 (RPM)
      int speed = command.substring(10).toInt();
      fanStepper.setSpeed(max(1, speed)); // Speed must be > 0
    } else if (command.startsWith("FAN_STEPS:")) { // e.g., FAN_STEPS:100 (positive for one way, negative for other)
      int steps = command.substring(10).toInt();
      if (steps != 0) { // Only step if steps is non-zero
          fanStepper.step(steps);
      }
    } else if (command == "FAN_ON") { // Simplified ON command - needs a speed to be set prior
        // This requires edge to remember last speed or send speed command first
        // For simplicity, let's assume it just means run at current set speed indefinitely
        // To make it run, you'd need a loop here or continuous small steps.
        // A better "FAN_ON" would be handled by the edge sending continuous "FAN_STEPS"
        // Or Arduino having a state machine. For "no logic", we'll just make it step a bit.
        // fanStepper.step(stepsPerRevolution / 4); // Example: run for 1/4 revolution
        Serial.println("{\"info\":\"FAN_ON received, set speed and steps separately\"}");
    } else if (command == "FAN_OFF") {
        // Stepper motors hold position when not stepping if powered.
        // To truly turn "off" (no power to coils), you'd need to control driver enable pin
        // or set speed to 0 and send no step commands.
        fanStepper.setSpeed(1); // Set to a very low speed (effectively stops it unless steps are sent)
        Serial.println("{\"info\":\"FAN_OFF received, speed set low\"}");
    }
  }
}