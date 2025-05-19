#include <DHT.h>

#define DHTPIN 2
#define DHTTYPE DHT11

DHT dht(DHTPIN, DHTTYPE);

const int buzzerPin = 6;

void setup() {
  Serial.begin(9600);
  dht.begin();
  delay(2000);

  pinMode(buzzerPin, OUTPUT);
  pinMode(ledPin, OUTPUT);

  windowServo.attach(servoPin);
  windowServo.write(0); // Servo closed position
}

void loop() {
  float temp = dht.readTemperature();
  float hum = dht.readHumidity();

  if (isnan(temp) || isnan(hum)) {
    Serial.println("ERROR");
    return;
  }

  // Send data over Serial
  Serial.print("TEMP:");
  Serial.print(temp);
  Serial.print(" HUM:");
  Serial.print(hum);

  // Check for incoming commands
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command == "BUZZER_ON") {
      digitalWrite(buzzerPin, HIGH);
    }
    else if (command == "BUZZER_OFF") {
      digitalWrite(buzzerPin, LOW);
    }
  }

  delay(3000);
}