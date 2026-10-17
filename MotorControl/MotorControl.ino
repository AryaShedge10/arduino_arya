/*
  Jetson Nano -> USB Serial -> Arduino Uno
  Two SmartElex 15S motor drivers, differential (tank) drive.

  Right driver: PWM = D9,  DIR = D7
  Left driver:  PWM = D10, DIR = D8

  Protocol v2 (one line per command, newline terminated, 115200 baud)

    M<left>,<right>   signed PWM, -255..255 each (e.g. "M120,-80")
    S                 stop
    F B L R           legacy fixed-speed commands (LEGACY_SPEED)

  Safety
    - Command timeout: no valid command for CMD_TIMEOUT_MS -> motors ramp to 0.
    - Slew limit: the applied PWM never changes faster than SLEW_PER_TICK per
      LOOP_TICK_MS, whatever the Jetson asks for. Direction pins only change
      while the PWM is near zero (no blocking delay, no hard reversal).
    - Unknown / malformed input stops the motors.

  Replies
    READY2                         once at boot
    ACK2 <tL> <tR> <curL> <curR>   target + applied PWM, at most every
                                   ACK_PERIOD_MS and after each command
    ERR ...                        on a bad command
*/

#define PWM_HIGH_FREQ 0   // 1 = Timer1 PWM ~3.9 kHz on D9/D10 (less motor whine)

const int RIGHT_PWM = 9;
const int RIGHT_DIR = 7;
const int LEFT_PWM = 10;
const int LEFT_DIR = 8;

const int LEGACY_SPEED = 150;
const unsigned long CMD_TIMEOUT_MS = 1000;
const unsigned long LOOP_TICK_MS = 5;
const int SLEW_PER_TICK = 6;            // 6 PWM / 5 ms = 1200 PWM/s
const unsigned long ACK_PERIOD_MS = 200;

const int BUF_LEN = 24;
char buf[BUF_LEN];
int bufLen = 0;

int targetL = 0, targetR = 0;           // what the Jetson asked for
int curL = 0, curR = 0;                 // what is applied now

unsigned long lastCmdMs = 0;
unsigned long lastTickMs = 0;
unsigned long lastAckMs = 0;
bool timedOut = false;

int clampPwm(int v) {
  if (v > 255) return 255;
  if (v < -255) return -255;
  return v;
}

int slewToward(int cur, int target) {
  if (target > cur + SLEW_PER_TICK) return cur + SLEW_PER_TICK;
  if (target < cur - SLEW_PER_TICK) return cur - SLEW_PER_TICK;
  return target;
}

void applyMotor(int pwmPin, int dirPin, int value) {
  // The direction pin is only flipped when |value| is already tiny, because
  // slewToward() walks the value through zero.
  digitalWrite(dirPin, value >= 0 ? HIGH : LOW);
  analogWrite(pwmPin, value >= 0 ? value : -value);
}

void sendAck() {
  Serial.print("ACK2 ");
  Serial.print(targetL); Serial.print(' ');
  Serial.print(targetR); Serial.print(' ');
  Serial.print(curL);    Serial.print(' ');
  Serial.println(curR);
  lastAckMs = millis();
}

void setTargets(int l, int r) {
  targetL = clampPwm(l);
  targetR = clampPwm(r);
  lastCmdMs = millis();
  timedOut = false;
}

void handleLine(const char* line) {
  char c = line[0];
  int l, r;
  switch (c) {
    case 'M':
    case 'm':
      if (sscanf(line + 1, "%d,%d", &l, &r) == 2) {
        setTargets(l, r);
        sendAck();
      } else {
        setTargets(0, 0);
        Serial.println("ERR: bad M args -> stop");
      }
      break;
    case 'F': case 'f': setTargets( LEGACY_SPEED,  LEGACY_SPEED); sendAck(); break;
    case 'B': case 'b': setTargets(-LEGACY_SPEED, -LEGACY_SPEED); sendAck(); break;
    case 'L': case 'l': setTargets(-LEGACY_SPEED,  LEGACY_SPEED); sendAck(); break;
    case 'R': case 'r': setTargets( LEGACY_SPEED, -LEGACY_SPEED); sendAck(); break;
    case 'S': case 's': setTargets(0, 0); sendAck(); break;
    default:
      setTargets(0, 0);
      Serial.println("ERR: unknown cmd -> stop");
      break;
  }
}

void setup() {
  pinMode(RIGHT_PWM, OUTPUT);
  pinMode(RIGHT_DIR, OUTPUT);
  pinMode(LEFT_PWM, OUTPUT);
  pinMode(LEFT_DIR, OUTPUT);
  analogWrite(RIGHT_PWM, 0);
  analogWrite(LEFT_PWM, 0);
  digitalWrite(RIGHT_DIR, HIGH);
  digitalWrite(LEFT_DIR, HIGH);

#if PWM_HIGH_FREQ
  TCCR1B = (TCCR1B & 0xF8) | 0x02;     // Timer1 prescaler /8 -> ~3.9 kHz
#endif

  Serial.begin(115200);
  Serial.println("READY2");
  lastCmdMs = millis();
}

void loop() {
  // Read whatever is waiting; commands are newline-terminated.
  while (Serial.available() > 0) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (bufLen > 0) {
        buf[bufLen] = '\0';
        handleLine(buf);
        bufLen = 0;
      }
    } else if (bufLen < BUF_LEN - 1) {
      buf[bufLen++] = ch;
    } else {
      bufLen = 0;                       // overflow: drop the line
      setTargets(0, 0);
      Serial.println("ERR: line too long -> stop");
    }
  }

  unsigned long now = millis();

  // Watchdog: the Jetson re-sends at >= 10 Hz while it is healthy.
  if (!timedOut && (now - lastCmdMs) > CMD_TIMEOUT_MS) {
    targetL = 0;
    targetR = 0;
    timedOut = true;
    Serial.println("TIMEOUT -> stop");
  }

  // Fixed-rate slew, no blocking delays.
  if (now - lastTickMs >= LOOP_TICK_MS) {
    lastTickMs = now;
    curL = slewToward(curL, targetL);
    curR = slewToward(curR, targetR);
    applyMotor(LEFT_PWM, LEFT_DIR, curL);
    applyMotor(RIGHT_PWM, RIGHT_DIR, curR);
  }

  if (now - lastAckMs >= ACK_PERIOD_MS) {
    sendAck();
  }
}
