void setup() {
  Serial.begin(9600);
}

void loop() {
  int lightValue = analogRead(lightSensorPin);

  Serial.println("Light Value:");
  Serial.println(lightValue);

  if (Serial.avaliable())
  {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command == '')
    {
      digitalWrite(pinxx, HIGH)
    }
  }
  delay(300);  
}