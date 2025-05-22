#include <IRremote.h>

/*Send serial data based on IR sensor data and control state of motor based off serial data.*/

const int RECIEVER_PIN = 7;
const int MOTOR_PIN = 8;

void setup()
{
  Serial.begin(9600);
  pinMode(MOTOR_PIN, OUTPUT);
  IrReceiver.begin(RECIEVER_PIN, ENABLE_LED_FEEDBACK); //ENABLE_LED_FEEDBACK causes the light on the IR sensor to flash when data is recieved.
}

void loop()
{
  //Send serial string if valid data is recieved from IR sensor:
  if (IrReceiver.decode()) //If there is IR data available
  {
    unsigned long code = IrReceiver.decodedIRData.decodedRawData;
    switch(code)
    {
      case 4077715200: //1 on remote
        Serial.println("1");
        break;
      //More cases can be added here
      default:
        //If code does not match
        break;
    }
    IrReceiver.resume(); //Allow next IR frame to be recieved
  }

  //Turn on motor if "On" is recieved, turn off if "Off" is recieved:
  String data = Serial.readString();
  if (data == "On")
  {
    digitalWrite(MOTOR_PIN, HIGH);
  }
  else if (data == "Off")
  {
    digitalWrite(MOTOR_PIN, LOW);
  }
}
