#include <Wire.h>
#include <BH1750.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include "Adafruit_VL53L0X.h"
#include <time.h>
// ==========================================
// NETWORK CONFIGURATION
// ==========================================
const char* ssid = "name_____";
const char* password = "erikahahaha";
const char* mqtt_server = "10.230.237.26";
WiFiClient espClient;
PubSubClient client(espClient);
// ==========================================
// PIN DEFINITIONS
// ==========================================
// --- Tank Monitoring Pins ---
const int SENSOR_PIN = 23;          // XKC-Y25-V Fluid Level Sensor
const int PUMP_PIN = 26;            // Peristaltic Pump Relay (High-Z fix)
const int turbidityPin = 35;        // Turbidity Sensor (Analog Input)
const int phPin = 34;               // pH Sensor Analog Input
const int relayTurbidityPin = 33;   // Relay Channel for Filter
const int relayLightPin = 25;       // Relay Channel for Lamp
// --- GSM Module (Hardware Serial2 on ESP32)
const int GSM_RX_PIN = 16;          // GSM RX (to TX of GSM)
const int GSM_TX_PIN = 17;          // GSM TX (to RX of GSM)
// --- Stepper Motor Pins (28BYJ-48 via ULN2003) ---
const int STEPPER_IN1 = 13;
const int STEPPER_IN2 = 12;
const int STEPPER_IN3 = 14;
const int STEPPER_IN4 = 27;
// ==========================================
// THRESHOLDS & CONSTANTS
// ==========================================
const int thresholdADC = 1600;       // Turbidity: below = cloudy
const float lightThreshold = 2.0;    // BH1750: below = dark (lux)
const boolean isActiveLowRelay = true;
const int TOF_THRESHOLD_MM = 50;     // VL53L0X: distance threshold (less than 50mm)
const int TOF_ZERO_THRESHOLD_MM = 0; // VL53L0X: distance threshold (0mm)
const int STEP_DELAY_US = 900;       // Stepper speed (lower = faster)
const float phCalibrationValue = 23.34;
// GSM / alerting
const char* ALERT_PHONE_NUMBER = "+639122196781"; // <- CHANGE to destination number
bool lowPhAlertSent = false;
bool highPhAlertSent = false;

// Forward declarations
void sendGSMSMS(const String &text);
void sendLowPhAlert(float phValue);
void sendHighPhAlert(float phValue);
void publishIfChanged(const char* topic, const String &value, String &lastState);
// Scheduling / time-based stepper run
const long TZ_OFFSET_SEC = 8 * 3600; // local timezone offset in seconds (adjust if needed)
const int SCHEDULE_WINDOW_MIN = 5;   // +/- minutes allowance
const int SCHEDULE_HOURS_COUNT = 4;
const int SCHEDULE_HOURS[SCHEDULE_HOURS_COUNT] = {8, 13, 18, 22};
const int TOF_SCHEDULE_MM = 100;     // distance threshold for scheduled action
bool scheduledTriggered[SCHEDULE_HOURS_COUNT] = {false, false, false, false};
bool scheduledStepperActive = false;
unsigned long scheduledStepperEndMillis = 0;
bool prevStepperAuto = true;

bool isWithinScheduleWindow(int targetHour) {
  time_t now;
  time(&now);
  // apply timezone offset
  now += TZ_OFFSET_SEC;
  struct tm *tm_info = localtime(&now);
  int currentMinutes = tm_info->tm_hour * 60 + tm_info->tm_min;
  int targetMinutes = targetHour * 60;
  int diff = abs(currentMinutes - targetMinutes);
  return (diff <= SCHEDULE_WINDOW_MIN);
}
bool waitForGSMResponse(const String &target, unsigned long timeoutMs);
// ==========================================
// STATE VARIABLES (All default to AUTO)
// ==========================================
bool pumpAuto = true;
bool filterAuto = true;
bool lampAuto = true;
bool stepperAuto = true;
// Non-blocking stepper state
int stepperDirection = 0;   // -1 = CCW, 0 = STOP, 1 = CW
int currentStepIndex = 0;

// Stepper Cycle State Machine (Non-blocking Step-Pause-Step)
int cycleState = 0;             // 0 = Idle, 1 = First Half, 2 = Paused, 3 = Second Half
unsigned long pauseStartMillis = 0;
const unsigned long PAUSE_DURATION_MS = 2000;
int cycleStepsCount = 0;
const int HALF_ROTATION_STEPS = 2048; // 2048 half-steps for 180 degrees
bool tofTriggered = false;      // To prevent multiple triggers when distance stays < 50mm

// ==========================================
// PREVIOUS-STATE TRACKING (Publish on change only)
// Prevents MQTT message flooding while keeping
// real-time responsiveness on actual state changes.
// ==========================================
String lastWaterLevelState = "";
String lastPumpState = "";
String lastFilterState = "";
String lastLampState = "";
String lastStepperState = "";

void startStepperCycle() {
  cycleState = 1;
  cycleStepsCount = 0;
  stepperDirection = 0; // Stop continuous run if any
  publishIfChanged("tank/stepper", "CW (Cycle)", lastStepperState);
  Serial.println("[CYCLE] Starting stepper cycle...");
}
// ==========================================
// TIMING (All non-blocking via millis/micros)
// ==========================================
unsigned long prevPumpMillis = 0;
const long PUMP_INTERVAL = 200;       // Water level check rate
unsigned long prevEnvMillis = 0;
const long ENV_INTERVAL = 1000;       // Turbidity + Light check rate
unsigned long prevTofMillis = 0;
const long TOF_INTERVAL = 500;        // ToF distance check rate
unsigned long prevStepMicros = 0;     // Stepper per-step timing
unsigned long lastReconnectAttempt = 0;
// ==========================================
// SENSOR OBJECTS
// - BH1750 on default I2C bus (SDA=21, SCL=22)
// - VL53L0X on secondary I2C bus (SDA=18, SCL=19)
// ==========================================
BH1750 lightMeter;
// Note: Wire1 is pre-defined globally by the ESP32 Wire library
Adafruit_VL53L0X lox = Adafruit_VL53L0X();
// Half-step lookup table for 28BYJ-48 stepper
const bool stepLookup[8][4] = {
  {true,  false, false, false},
  {true,  true,  false, false},
  {false, true,  false, false},
  {false, true,  true,  false},
  {false, false, true,  false},
  {false, false, true,  true },
  {false, false, false, true },
  {true,  false, false, true }
};

float readPH() {
  int buffer_arr[10];
  for (int i = 0; i < 10; i++) {
    buffer_arr[i] = analogRead(phPin);
    delay(10);
  }

  for (int i = 0; i < 9; i++) {
    for (int j = i + 1; j < 10; j++) {
      if (buffer_arr[i] > buffer_arr[j]) {
        int temp = buffer_arr[i];
        buffer_arr[i] = buffer_arr[j];
        buffer_arr[j] = temp;
      }
    }
  }

  unsigned long avgValue = 0;
  for (int i = 2; i < 8; i++) {
    avgValue += buffer_arr[i];
  }

  float avgVoltage = (avgValue / 6.0) * (3.3 / 4095.0);
  return (-5.70 * avgVoltage) + phCalibrationValue;
}
// ==========================================
// MQTT CALLBACK (Manual Override Commands)
// ==========================================
void callback(char* topic, byte* payload, unsigned int length) {
  String message;
  for (unsigned int i = 0; i < length; i++) {
    message += (char)payload[i];
  }
  String incomingTopic = String(topic);
  Serial.println("[MQTT CMD] " + incomingTopic + " -> " + message);
  // --- Pump Commands ---
  if (incomingTopic == "tank/pump/cmd") {
    if (message == "ON") {
      pumpAuto = false;
      triggerRelay(PUMP_PIN, true);
      publishIfChanged("tank/pump", "ON (Manual)", lastPumpState);
    } else if (message == "OFF") {
      pumpAuto = false;
      triggerRelay(PUMP_PIN, false);
      publishIfChanged("tank/pump", "OFF (Manual)", lastPumpState);
    } else if (message == "AUTO") {
      pumpAuto = true;
      Serial.println("[MODE] Pump -> AUTO");
    }
  }
  // --- Filter Commands ---
  if (incomingTopic == "tank/filter/cmd") {
    if (message == "ON") {
      filterAuto = false;
      triggerRelay(relayTurbidityPin, true);
      publishIfChanged("tank/filter", "ON (Manual)", lastFilterState);
    } else if (message == "OFF") {
      filterAuto = false;
      triggerRelay(relayTurbidityPin, false);
      publishIfChanged("tank/filter", "OFF (Manual)", lastFilterState);
    } else if (message == "AUTO") {
      filterAuto = true;
      Serial.println("[MODE] Filter -> AUTO");
    }
  }
  // --- Lamp Commands ---
  if (incomingTopic == "tank/lamp/cmd") {
    if (message == "ON") {
      lampAuto = false;
      triggerRelay(relayLightPin, true);
      publishIfChanged("tank/lamp", "ON (Manual)", lastLampState);
    } else if (message == "OFF") {
      lampAuto = false;
      triggerRelay(relayLightPin, false);
      publishIfChanged("tank/lamp", "OFF (Manual)", lastLampState);
    } else if (message == "AUTO") {
      lampAuto = true;
      Serial.println("[MODE] Lamp -> AUTO");
    }
  }
  // --- Stepper Commands ---
  if (incomingTopic == "tank/stepper/cmd") {
    if (message == "CW") {
      stepperAuto = false;
      cycleState = 0; // Cancel cycle if any
      stepperDirection = 1;
      publishIfChanged("tank/stepper", "CW (Manual)", lastStepperState);
    } else if (message == "CCW") {
      stepperAuto = false;
      cycleState = 0; // Cancel cycle if any
      stepperDirection = -1;
      publishIfChanged("tank/stepper", "CCW (Manual)", lastStepperState);
    } else if (message == "STOP") {
      stepperAuto = false;
      cycleState = 0; // Cancel cycle if any
      stepperDirection = 0;
      stopStepper();
      publishIfChanged("tank/stepper", "STOPPED (Manual)", lastStepperState);
    } else if (message == "ON") {
      stepperAuto = false;
      startStepperCycle();
      publishIfChanged("tank/stepper", "ON (Manual)", lastStepperState);
    } else if (message == "AUTO") {
      stepperAuto = true;
      cycleState = 0; // Cancel cycle if any
      Serial.println("[MODE] Stepper -> AUTO");
    }
  }
}
// ==========================================
// WiFi Connection (Blocking on boot only)
// ==========================================
void setup_wifi() {
  delay(10);
  Serial.println("\n=============================================");
  Serial.print("[WIFI] Connecting to: ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);
  int counter = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    counter++;
    if (counter > 30) {
      Serial.println("\n[WARNING] WiFi taking too long. Check SSID/Password!");
      counter = 0;
    }
  }
  Serial.println("\n[WIFI] Connected!");
  Serial.print("[WIFI] IP: ");
  Serial.println(WiFi.localIP());
  Serial.println("=============================================\n");
}
// ==========================================
// Non-blocking MQTT Reconnect
// ==========================================
void maintainMQTTConnection() {
  if (!client.connected()) {
    unsigned long now = millis();
    if (now - lastReconnectAttempt > 5000) {
      lastReconnectAttempt = now;
      Serial.print("[MQTT] Connecting to ");
      Serial.print(mqtt_server);
      Serial.println("...");
      if (client.connect("ESP32_TankMonitor")) {
        Serial.println("[MQTT] Connected! Subscribing to command topics.");
        client.subscribe("tank/pump/cmd");
        client.subscribe("tank/filter/cmd");
        client.subscribe("tank/lamp/cmd");
        client.subscribe("tank/stepper/cmd");
      } else {
        Serial.print("[MQTT] Failed, rc=");
        Serial.print(client.state());
        Serial.println(". Retrying in 5s...");
      }
    }
  } else {
    client.loop();
  }
}
// ==========================================
// SETUP
// ==========================================
void setup() {
  Serial.begin(115200);
  delay(1500);  // Safe boot delay (avoids while(!Serial) lockup)
  Serial.println("\n=============================================");
  Serial.println("  COMBINED TANK MONITOR + ToF STEPPER BOOT  ");
  Serial.println("=============================================");
  // Initialize shared I2C bus (ESP32 default: SDA=21, SCL=22)
  Wire.begin(21, 22);
  // Initialize secondary I2C bus for VL53L0X (SDA=18, SCL=19)
  Wire1.begin(18, 19);
  // --- BH1750 Light Sensor (I2C addr 0x23) ---
  if (lightMeter.begin(BH1750::CONTINUOUS_HIGH_RES_MODE)) {
    Serial.println("[OK] BH1750 Light Sensor ready on I2C.");
  } else {
    Serial.println("[ERR] BH1750 failed! Check I2C SDA/SCL wiring.");
  }
  // --- VL53L0X ToF Sensor (I2C addr 0x29) ---
  if (lox.begin(0x29, false, &Wire1)) {
    Serial.println("[OK] VL53L0X ToF Sensor ready on I2C.");
  } else {
    Serial.println("[ERR] VL53L0X failed! Check I2C SDA/SCL wiring.");
  }
  // --- Tank monitoring GPIO ---
  pinMode(SENSOR_PIN, INPUT);
  pinMode(relayTurbidityPin, OUTPUT);
  pinMode(relayLightPin, OUTPUT);
  pinMode(phPin, INPUT);
  analogSetPinAttenuation(phPin, ADC_11db);
  // --- GSM serial for SIM/GSM module (HardwareSerial2)
  Serial2.begin(9600, SERIAL_8N1, GSM_RX_PIN, GSM_TX_PIN);
  Serial.println(String("[GSM] Serial2 started on RX=") + String(GSM_RX_PIN) + String(" TX=") + String(GSM_TX_PIN));
  // --- Stepper motor GPIO ---
  pinMode(STEPPER_IN1, OUTPUT);
  pinMode(STEPPER_IN2, OUTPUT);
  pinMode(STEPPER_IN3, OUTPUT);
  pinMode(STEPPER_IN4, OUTPUT);
  // --- Safe boot state: all actuators OFF ---
  triggerRelay(relayTurbidityPin, false);
  triggerRelay(relayLightPin, false);
  triggerRelay(PUMP_PIN, false);
  stopStepper();
  // --- Network ---
  setup_wifi();
  // Initialize NTP time (used for scheduled stepper actions)
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.println("[TIME] NTP client started (applying TZ offset in checks)");
  client.setServer(mqtt_server, 1883);
  client.setCallback(callback);
  Serial.println("[SYSTEM] Boot complete. Entering main loop.\n");
}
// ==========================================
// MAIN LOOP (Entirely non-blocking)
// ==========================================
void loop() {
  // Keep MQTT alive in the background
  maintainMQTTConnection();
  unsigned long currentMillis = millis();
  unsigned long currentMicros = micros();
  // ------------------------------------------
  // 1. WATER LEVEL + PUMP (every 200ms)
  // ------------------------------------------
  if (currentMillis - prevPumpMillis >= PUMP_INTERVAL) {
    prevPumpMillis = currentMillis;
    int rawSignal = digitalRead(SENSOR_PIN);
    String waterState = (rawSignal == HIGH) ? "FULL" : "LOW";
    // Publish water level sensor reading (change-only)
    publishIfChanged("tank/waterlevel", waterState, lastWaterLevelState);
    // Auto pump control based on fluid level
    if (pumpAuto) {
      if (rawSignal == HIGH) {
        triggerRelay(PUMP_PIN, false);
        publishIfChanged("tank/pump", "OFF (Auto)", lastPumpState);
      } else {
        triggerRelay(PUMP_PIN, true);
        publishIfChanged("tank/pump", "ON (Auto)", lastPumpState);
      }
    }
  }
  // ------------------------------------------
  // 2. TURBIDITY + LIGHT (every 1000ms)
  // ------------------------------------------
  if (currentMillis - prevEnvMillis >= ENV_INTERVAL) {
    prevEnvMillis = currentMillis;
    // --- Turbidity Sensor ---
    int rawTurbidity = analogRead(turbidityPin);
    float turbidityVoltage = rawTurbidity * (3.3 / 4095.0);
    Serial.println("\n--- [SENSOR READINGS] ---");
    Serial.print("Turbidity -> ADC: "); Serial.print(rawTurbidity);
    Serial.print(" | V: "); Serial.print(turbidityVoltage, 2); Serial.println("V");
    // Always publish sensor value (continuous data, not state)
    if (client.connected()) {
      client.publish("tank/turbidity", String(rawTurbidity).c_str());
    }
    // --- pH Sensor ---
    float phValue = readPH();
    int rawPh = analogRead(phPin);
    float phVoltage = rawPh * (3.3 / 4095.0);
    Serial.print("pH -> ADC: "); Serial.print(rawPh);
    Serial.print(" | V: "); Serial.print(phVoltage, 2);
    Serial.print("V | pH: "); Serial.println(phValue, 2);
    if (client.connected()) {
      client.publish("tank/ph", String(phValue, 2).c_str());
    }
    // --- pH alerting: distinct low/high alerts with one-shot per overlap
    if (phValue < 6.5) {
      if (!lowPhAlertSent) {
        Serial.println("[ALERT] pH LOW -> sending GSM recommendation");
        sendLowPhAlert(phValue);
        lowPhAlertSent = true;
      }
    } else if (phValue > 8.0) {
      if (!highPhAlertSent) {
        Serial.println("[ALERT] pH HIGH -> sending GSM recommendation");
        sendHighPhAlert(phValue);
        highPhAlertSent = true;
      }
    } else {
      // reset alert latches when pH returns to the normal window
      if (lowPhAlertSent || highPhAlertSent) {
        Serial.println("[ALERT] pH back to normal range -> latches reset");
      }
      lowPhAlertSent = false;
      highPhAlertSent = false;
    }
    // Auto filter control based on turbidity
    if (filterAuto) {
      if (rawTurbidity < thresholdADC) {
        Serial.println("  [AUTO] Water CLOUDY -> Filter ON");
        triggerRelay(relayTurbidityPin, true);
        publishIfChanged("tank/filter", "ON (Auto)", lastFilterState);
      } else {
        Serial.println("  [AUTO] Water CLEAR -> Filter OFF");
        triggerRelay(relayTurbidityPin, false);
        publishIfChanged("tank/filter", "OFF (Auto)", lastFilterState);
      }
    }
    // --- BH1750 Light Sensor ---
    float lux = lightMeter.readLightLevel();
    Serial.print("Light -> "); Serial.print(lux, 1); Serial.println(" lx");
    // Always publish sensor value (continuous data)
    if (client.connected()) {
      client.publish("tank/light", String(lux, 1).c_str());
    }
    // Auto lamp control based on light level
    if (lampAuto) {
      if (lux < lightThreshold) {
        Serial.println("  [AUTO] DARK -> Lamp ON");
        triggerRelay(relayLightPin, true);
        publishIfChanged("tank/lamp", "ON (Auto)", lastLampState);
      } else {
        Serial.println("  [AUTO] BRIGHT -> Lamp OFF");
        triggerRelay(relayLightPin, false);
        publishIfChanged("tank/lamp", "OFF (Auto)", lastLampState);
      }
    }
    Serial.println("-------------------------\n");
  }
  // ------------------------------------------
  // 3. ToF DISTANCE SENSOR (every 500ms)
  // ------------------------------------------
  if (currentMillis - prevTofMillis >= TOF_INTERVAL) {
    prevTofMillis = currentMillis;
    VL53L0X_RangingMeasurementData_t measure;
    lox.rangingTest(&measure, false);
    if (measure.RangeStatus != 4 && measure.RangeMilliMeter != 8191) {
      int distance_mm = measure.RangeMilliMeter;
      Serial.print("ToF -> "); Serial.print(distance_mm); Serial.println(" mm");
      // Always publish distance (continuous data)
      if (client.connected()) {
        client.publish("tank/tof", String(distance_mm).c_str());
      }
      // Scheduled stepper trigger: when object is very close and time matches schedule
      if (distance_mm <= TOF_SCHEDULE_MM) {
        for (int i = 0; i < SCHEDULE_HOURS_COUNT; i++) {
          if (isWithinScheduleWindow(SCHEDULE_HOURS[i])) {
            if (!scheduledTriggered[i]) {
              Serial.println("[SCHEDULE] TOF close and time matched -> starting scheduled stepper cycle");
              startStepperCycle();
              scheduledTriggered[i] = true;
            }
          } else {
            // Reset trigger for that schedule when outside window so it can fire next time
            scheduledTriggered[i] = false;
          }
        }
      }
      // Auto stepper control based on distance (< 50mm or 0mm triggers cycle, else idle)
      if (stepperAuto) {
        if (distance_mm <= TOF_THRESHOLD_MM || distance_mm == TOF_ZERO_THRESHOLD_MM) {
          if (!tofTriggered) {
            tofTriggered = true;
            startStepperCycle();
            Serial.println("  [AUTO] <= 50mm or 0mm -> Triggered Stepper Cycle");
          }
        } else {
          tofTriggered = false; // Reset trigger when out of range
          if (cycleState == 0) {
            stepperDirection = 0;
            stopStepper();
            publishIfChanged("tank/stepper", "STOPPED (Auto)", lastStepperState);
          }
        }
      }
    } else {
      // Nothing in range — stop stepper if auto
      if (client.connected()) {
        client.publish("tank/tof", "-1");
      }
      if (stepperAuto) {
        tofTriggered = false;
        if (cycleState == 0) {
          stepperDirection = 0;
          stopStepper();
          publishIfChanged("tank/stepper", "STOPPED (Auto)", lastStepperState);
        }
      }
    }
  }
  // ------------------------------------------
  // 4. STEPPER MOTOR (non-blocking, per-step)
  //    Handles continuous running and the step-pause-step cycle
  // ------------------------------------------
  if (cycleState > 0) {
    // Running step-pause-step cycle
    if (cycleState == 1) {
      // First half of rotation (180 degrees)
      if (currentMicros - prevStepMicros >= (unsigned long)STEP_DELAY_US) {
        prevStepMicros = currentMicros;
        singleStep(true); // Rotate CW
        cycleStepsCount++;
        if (cycleStepsCount >= HALF_ROTATION_STEPS) {
          stopStepper(); // De-energize motor while paused
          cycleState = 2; // Pause state
          pauseStartMillis = millis();
          publishIfChanged("tank/stepper", "PAUSED (Cycle)", lastStepperState);
          Serial.println("[CYCLE] First half complete. Pausing for 2s...");
        }
      }
    } else if (cycleState == 2) {
      // Pause for 2 seconds
      if (millis() - pauseStartMillis >= PAUSE_DURATION_MS) {
        cycleState = 3; // Move to second half
        cycleStepsCount = 0;
        publishIfChanged("tank/stepper", "CW (Cycle)", lastStepperState);
        Serial.println("[CYCLE] Pause complete. Starting second half...");
      }
    } else if (cycleState == 3) {
      // Second half of rotation (180 degrees)
      if (currentMicros - prevStepMicros >= (unsigned long)STEP_DELAY_US) {
        prevStepMicros = currentMicros;
        singleStep(true); // Rotate CW
        cycleStepsCount++;
        if (cycleStepsCount >= HALF_ROTATION_STEPS) {
          stopStepper(); // Done
          cycleState = 0; // Back to Idle
          publishIfChanged("tank/stepper", "STOPPED (Cycle)", lastStepperState);
          Serial.println("[CYCLE] Cycle complete. Stepper stopped.");
        }
      }
    }
  } else if (stepperDirection != 0) {
    // Regular continuous manual override (CW or CCW)
    if (currentMicros - prevStepMicros >= (unsigned long)STEP_DELAY_US) {
      prevStepMicros = currentMicros;
      singleStep(stepperDirection > 0);
    }
  }
}
// ==========================================
// RELAY HELPER (Preserves your pump high-Z fix)
// ==========================================
void triggerRelay(int pin, boolean turnOn) {
  // Pump uses the high-impedance INPUT trick for reliable shutoff
  if (pin == PUMP_PIN) {
    if (turnOn) {
      if (isActiveLowRelay) {
        digitalWrite(pin, LOW);
        pinMode(pin, OUTPUT);
      } else {
        digitalWrite(pin, HIGH);
        pinMode(pin, OUTPUT);
      }
    } else {
      pinMode(pin, INPUT);  // High-impedance shutoff
    }
    return;
  }
  // Standard relay channels
  if (isActiveLowRelay) {
    digitalWrite(pin, turnOn ? LOW : HIGH);
  } else {
    digitalWrite(pin, turnOn ? HIGH : LOW);
  }
}
// ==========================================
// STEPPER HELPERS (Non-blocking single-step)
// ==========================================
void singleStep(bool clockwise) {
  if (clockwise) {
    currentStepIndex++;
    if (currentStepIndex > 7) currentStepIndex = 0;
  } else {
    currentStepIndex--;
    if (currentStepIndex < 0) currentStepIndex = 7;
  }
  digitalWrite(STEPPER_IN1, stepLookup[currentStepIndex][0]);
  digitalWrite(STEPPER_IN2, stepLookup[currentStepIndex][1]);
  digitalWrite(STEPPER_IN3, stepLookup[currentStepIndex][2]);
  digitalWrite(STEPPER_IN4, stepLookup[currentStepIndex][3]);
}
void stopStepper() {
  digitalWrite(STEPPER_IN1, LOW);
  digitalWrite(STEPPER_IN2, LOW);
  digitalWrite(STEPPER_IN3, LOW);
  digitalWrite(STEPPER_IN4, LOW);
}

// ==========================================
// GSM ALERT: send SMS via Serial2 (GPIO16/17)
// ==========================================
void sendGSMSMS(const String &text) {
  Serial.println("[GSM] Sending SMS (robust): " + text);
  // Basic handshake
  Serial2.println("AT");
  if (!waitForGSMResponse("OK", 1000)) {
    Serial.println("[GSM] No response to AT");
  }
  Serial2.println("AT+CMGF=1"); // Text mode
  if (!waitForGSMResponse("OK", 1000)) {
    Serial.println("[GSM] AT+CMGF failed or timed out");
  }
  // Split long messages into safe-sized parts to avoid module limits
  const int PART_LEN = 120; // conservative part length
  int totalLen = text.length();
  for (int offset = 0; offset < totalLen; offset += PART_LEN) {
    int end = offset + PART_LEN;
    if (end > totalLen) end = totalLen;
    String part = text.substring(offset, end);
    // Request CMGS, then wait for '>' prompt
    Serial2.print("AT+CMGS=\""); Serial2.print(ALERT_PHONE_NUMBER); Serial2.println("\"");
    if (!waitForGSMResponse(">", 2000)) {
      Serial.println("[GSM] No '>' prompt for CMGS — aborting part");
      return;
    }
    // Send the text part and terminate with Ctrl+Z
    Serial2.print(part);
    Serial2.write(26);
    // Wait for +CMGS confirmation
    if (!waitForGSMResponse("+CMGS", 5000)) {
      Serial.println("[GSM] No +CMGS confirmation — message may have failed");
    } else {
      Serial.println("[GSM] Part sent successfully");
    }
    delay(500); // small spacing between parts
  }
  Serial.println("[GSM] SMS send routine complete.");
}

// Wait for a substring response from Serial2 with timeout (returns true if found)
bool waitForGSMResponse(const String &target, unsigned long timeoutMs) {
  unsigned long start = millis();
  String buf = "";
  while (millis() - start < timeoutMs) {
    while (Serial2.available()) {
      char c = (char)Serial2.read();
      buf += c;
    }
    if (buf.indexOf(target) >= 0) {
      Serial.print("[GSM RX] "); Serial.println(buf);
      return true;
    }
    delay(10);
  }
  Serial.print("[GSM RX TIMEOUT] last buffer: "); Serial.println(buf);
  return false;
}

void publishIfChanged(const char* topic, const String &value, String &lastState) {
  if (value != lastState) {
    if (client.connected()) {
      client.publish(topic, value.c_str());
      Serial.print("[MQTT SEND] ");
      Serial.print(topic);
      Serial.print(" → ");
      Serial.println(value);
    } else {
      Serial.print("[MQTT] Not connected, skipping publish: ");
      Serial.print(topic);
      Serial.print(" → ");
      Serial.println(value);
    }
    lastState = value;
  }
}

void sendLowPhAlert(float phValue) {
  String msg = "ALERT: pH LOW (" + String(phValue, 2) + "). Recommendation: ";
  msg += "Perform a 20-30% partial water change using dechlorinated water; \n";
  msg += "test and raise KH/alkalinity if low, and consider a commercial pH buffer per instructions. \n";
  msg += "Reduce feeding temporarily, increase gentle aeration, and re-test pH in a few hours. \n";
  msg += "If levels remain low or livestock show stress, seek expert advice before large chemical adjustments.";
  sendGSMSMS(msg);
}

void sendHighPhAlert(float phValue) {
  String msg = "ALERT: pH HIGH (" + String(phValue, 2) + "). Recommendation: ";
  msg += "Perform gradual 10-20% water changes to slowly lower pH; \n";
  msg += "check for high KH/alkalinity and substrate or source water causes. \n";
  msg += "If needed, use a commercial pH-lowering product carefully and follow directions; \n";
  msg += "avoid rapid changes that stress fish, monitor for 24-48 hours, and consult experts if unsure.";
  sendGSMSMS(msg);
}
