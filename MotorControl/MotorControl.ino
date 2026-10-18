/*
  Jetson Nano -> USB Serial -> Arduino Uno
  Two SmartElex 15S motor drivers, differential (tank) drive, one encoder
  per side (the two motors of a side share one driver and one encoder).

  Right driver: PWM = D9,  DIR = D7        Right encoder: A = D3 (INT1), B = A0
  Left driver:  PWM = D10, DIR = D8        Left  encoder: A = D2 (INT0), B = D4

  Protocol v3 (one line per command, newline terminated, 115200 baud)

    M<left>,<right>   raw signed PWM, -255..255 each          (open loop)
    V<left>,<right>   wheel speed targets in encoder ticks/s   (closed loop)
    G<kp>,<ki>,<kd>,<kff>,<minpwm>
                      PID gains x1000 (PWM per tick/s, per tick/s*s, ...);
                      minpwm = PWM at which a stopped wheel starts to turn
    S                 stop
    F B L R           legacy fixed-speed commands (LEGACY_SPEED)

  One "tick" = one edge of channel A (2 ticks per encoder pulse). Python
  converts ticks <-> m/s, the firmware never needs the wheel size.

  Safety (unchanged from v2)
    - Command timeout: no valid command for CMD_TIMEOUT_MS -> motors ramp to 0.
    - Slew limit: the applied PWM never changes faster than SLEW_PER_TICK per
      LOOP_TICK_MS, whatever the PID or the Jetson asks for. The direction pin
      only flips while the PWM is near zero.
    - Unknown / malformed input stops the motors.
  Added for the PID
    - The PID correction is clamped to +-CORR_MAX around the feed-forward, so
      a dead encoder can never run a wheel away.
    - A wheel that is commanded but shows no motion for FAULT_MS is flagged
      (bit in the ACK) and falls back to feed-forward only (open loop).

  Replies
    READY2 v3                      once at boot
    ACK2 tL tR cL cR vL vR eL eR m sL sR f
         t = PWM target (PID output in V mode), c = PWM applied,
         v = measured ticks/s, e = tick counters, m = P or V,
         s = ticks/s target, f = fault bits (1 = left, 2 = right encoder)
         Fields 1-4 are identical to v2, so old parsers still work.
    GAINS ok                       after a valid G command
    ERR ...                        on a bad command
*/

#define PWM_HIGH_FREQ 0   // 1 = Timer1 PWM ~3.9 kHz on D9/D10 (less motor whine)

// Encoder wiring. Quadrature (A+B) gives direction; with a single channel
// the direction is taken from the sign of the applied PWM instead.
#define ENC_QUADRATURE 1
#define ENC_INVERT_L 0    // set to 1 if forward counts go negative
#define ENC_INVERT_R 0

const int RIGHT_PWM = 9;
const int RIGHT_DIR = 7;
const int LEFT_PWM = 10;
const int LEFT_DIR = 8;

const int ENC_L_A = 2;
const int ENC_L_B = 4;
const int ENC_R_A = 3;
const int ENC_R_B = A0;

const int LEGACY_SPEED = 150;
const unsigned long CMD_TIMEOUT_MS = 1000;
const unsigned long LOOP_TICK_MS = 5;
const int SLEW_PER_TICK = 6;            // 6 PWM / 5 ms = 1200 PWM/s
const unsigned long ACK_PERIOD_MS = 100;
const unsigned long ACK_MIN_GAP_MS = 40;   // replies to commands are throttled

// PID
const unsigned long PID_PERIOD_MS = 20;    // 50 Hz speed measurement + PID
const float SPEED_FILTER = 0.5f;           // measured-speed low-pass (per PID tick)
const int SPEED_WINDOW = 5;                // speed = ticks over the last 5 PID periods (100 ms)
const float CORR_MAX = 100.0f;             // max PID correction, PWM
const float I_MAX = 80.0f;                 // integrator clamp, PWM
const unsigned long FAULT_MS = 1200;       // commanded but not moving for this long
const float FAULT_MIN_TARGET = 30.0f;      // ticks/s: below this we do not judge
const float FAULT_MOVING = 12.0f;          // ticks/s that count as "moving"

const int BUF_LEN = 48;
char buf[BUF_LEN];
int bufLen = 0;

int targetL = 0, targetR = 0;           // PWM the slew limiter walks toward
int curL = 0, curR = 0;                 // PWM applied now

// Encoder counters (written by interrupts)
volatile long encL = 0, encR = 0;

// Closed loop state
bool speedMode = false;
float spdTgtL = 0, spdTgtR = 0;         // ticks/s targets
float spdMeasL = 0, spdMeasR = 0;       // filtered ticks/s
long histL[SPEED_WINDOW], histR[SPEED_WINDOW];   // tick counters, oldest first at histIdx
unsigned long histT[SPEED_WINDOW];
int histIdx = 0;
float intL = 0, intR = 0;
float prevErrL = 0, prevErrR = 0;
unsigned long noMoveL = 0, noMoveR = 0; // ms commanded without motion
byte faultBits = 0;

// Gains (defaults are conservative; Python overwrites them with G on connect)
float kp = 0.8f, ki = 1.5f, kd = 0.0f, kff = 1.0f, minPwm = 40.0f;

unsigned long lastCmdMs = 0;
unsigned long lastTickMs = 0;
unsigned long lastAckMs = 0;
unsigned long lastPidMs = 0;
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

// ---- encoders -----------------------------------------------------------
void isrL() {
#if ENC_QUADRATURE
  if (digitalRead(ENC_L_A) == digitalRead(ENC_L_B)) encL--; else encL++;
#else
  encL++;
#endif
}

void isrR() {
#if ENC_QUADRATURE
  if (digitalRead(ENC_R_A) == digitalRead(ENC_R_B)) encR--; else encR++;
#else
  encR++;
#endif
}

long readEnc(volatile long* p) {
  noInterrupts();
  long v = *p;
  interrupts();
  return v;
}

// Signed tick counter as seen by the controller ("forward" = positive).
long sideTicks(bool left) {
  long raw = readEnc(left ? &encL : &encR);
#if ENC_QUADRATURE
  if (left) return ENC_INVERT_L ? -raw : raw;
  return ENC_INVERT_R ? -raw : raw;
#else
  // single channel: counts only grow, direction comes from the applied PWM
  return raw;
#endif
}

// ---- reporting ----------------------------------------------------------
void sendAck() {
  Serial.print(F("ACK2 "));
  Serial.print(targetL); Serial.print(' ');
  Serial.print(targetR); Serial.print(' ');
  Serial.print(curL);    Serial.print(' ');
  Serial.print(curR);    Serial.print(' ');
  Serial.print((int)spdMeasL); Serial.print(' ');
  Serial.print((int)spdMeasR); Serial.print(' ');
  Serial.print(sideTicks(true));  Serial.print(' ');
  Serial.print(sideTicks(false)); Serial.print(' ');
  Serial.print(speedMode ? 'V' : 'P'); Serial.print(' ');
  Serial.print((int)spdTgtL); Serial.print(' ');
  Serial.print((int)spdTgtR); Serial.print(' ');
  Serial.println(faultBits);
  lastAckMs = millis();
}

void sendAckThrottled() {
  if (millis() - lastAckMs >= ACK_MIN_GAP_MS) sendAck();
}

// ---- commands -----------------------------------------------------------
void resetPid() {
  intL = intR = 0;
  prevErrL = prevErrR = 0;
  noMoveL = noMoveR = 0;
  faultBits = 0;
}

void setTargets(int l, int r) {          // raw PWM (open loop)
  if (speedMode) resetPid();
  speedMode = false;
  spdTgtL = spdTgtR = 0;
  targetL = clampPwm(l);
  targetR = clampPwm(r);
  lastCmdMs = millis();
  timedOut = false;
}

void setSpeedTargets(long l, long r) {   // ticks/s (closed loop)
  if (!speedMode) resetPid();
  speedMode = true;
  spdTgtL = (float)l;
  spdTgtR = (float)r;
  lastCmdMs = millis();
  timedOut = false;
}

void handleLine(const char* line) {
  char c = line[0];
  int l, r;
  long sl, sr;
  int gp, gi, gd, gf, gm;
  switch (c) {
    case 'M':
    case 'm':
      if (sscanf(line + 1, "%d,%d", &l, &r) == 2) {
        setTargets(l, r);
        sendAckThrottled();
      } else {
        setTargets(0, 0);
        Serial.println(F("ERR: bad M args -> stop"));
      }
      break;
    case 'V':
    case 'v':
      if (sscanf(line + 1, "%ld,%ld", &sl, &sr) == 2) {
        setSpeedTargets(sl, sr);
        sendAckThrottled();
      } else {
        setTargets(0, 0);
        Serial.println(F("ERR: bad V args -> stop"));
      }
      break;
    case 'G':
    case 'g':
      if (sscanf(line + 1, "%d,%d,%d,%d,%d", &gp, &gi, &gd, &gf, &gm) == 5) {
        kp = gp / 1000.0f;
        ki = gi / 1000.0f;
        kd = gd / 1000.0f;
        kff = gf / 1000.0f;
        minPwm = (float)gm;
        resetPid();
        Serial.println(F("GAINS ok"));
      } else {
        Serial.println(F("ERR: bad G args"));
      }
      break;
    case 'F': case 'f': setTargets( LEGACY_SPEED,  LEGACY_SPEED); sendAckThrottled(); break;
    case 'B': case 'b': setTargets(-LEGACY_SPEED, -LEGACY_SPEED); sendAckThrottled(); break;
    case 'L': case 'l': setTargets(-LEGACY_SPEED,  LEGACY_SPEED); sendAckThrottled(); break;
    case 'R': case 'r': setTargets( LEGACY_SPEED, -LEGACY_SPEED); sendAckThrottled(); break;
    case 'S': case 's': setTargets(0, 0); sendAck(); break;
    default:
      setTargets(0, 0);
      Serial.println(F("ERR: unknown cmd -> stop"));
      break;
  }
}

// ---- speed measurement + PID --------------------------------------------
// One side: returns the PWM target for the slew limiter.
int pidSide(float dtS, float raw, float* meas,
            float tgt, float* integ, float* prevErr, unsigned long* noMove,
            byte faultBit, int appliedPwm) {
#if !ENC_QUADRATURE
  // Single channel: the sign comes from the way the wheel is being driven.
  if (appliedPwm < 0) raw = -raw;
#endif
  *meas += SPEED_FILTER * (raw - *meas);

  if (tgt == 0.0f) {                      // stop: no PID, no integrator
    *integ = 0; *prevErr = 0; *noMove = 0;
    faultBits &= ~faultBit;
    return 0;
  }

  float sgn = tgt > 0 ? 1.0f : -1.0f;
  float ff = sgn * minPwm + kff * tgt;
  float err = tgt - *meas;

  // Encoder fault: commanded for a while but the wheel does not move.
  if (fabs(tgt) >= FAULT_MIN_TARGET && fabs(*meas) < FAULT_MOVING) {
    if (*noMove < 60000UL) *noMove += PID_PERIOD_MS;
    if (*noMove >= FAULT_MS) faultBits |= faultBit;
  } else {
    *noMove = 0;
    if (fabs(*meas) > 2 * FAULT_MOVING) faultBits &= ~faultBit;
  }
  if (faultBits & faultBit) {             // trust the feed-forward only
    *integ = 0;
    return constrain((int)ff, -255, 255);
  }

  float deriv = (err - *prevErr) / dtS;
  *prevErr = err;
  float corr = kp * err + *integ + kd * deriv;
  float out = ff + constrain(corr, -CORR_MAX, CORR_MAX);
  bool saturated = (out > 255.0f && err > 0) || (out < -255.0f && err < 0);
  if (!saturated) {
    *integ += ki * err * dtS;
    *integ = constrain(*integ, -I_MAX, I_MAX);
  }
  // never let the PID push a wheel against the commanded direction
  if (sgn > 0 && out < 0) out = 0;
  if (sgn < 0 && out > 0) out = 0;
  return constrain((int)out, -255, 255);
}

void pidUpdate(unsigned long now) {
  float dtS = (now - lastPidMs) / 1000.0f;
  lastPidMs = now;
  if (dtS <= 0) return;
  long tl = sideTicks(true), tr = sideTicks(false);
  // A 100 ms window instead of one 20 ms period: a slow wheel only makes a
  // few ticks per period, which would make the speed jump between 0 and 2x.
  float winS = (now - histT[histIdx]) / 1000.0f;
  float rawL = winS > 0.02f ? (tl - histL[histIdx]) / winS : 0;
  float rawR = winS > 0.02f ? (tr - histR[histIdx]) / winS : 0;
  histL[histIdx] = tl; histR[histIdx] = tr; histT[histIdx] = now;
  histIdx = (histIdx + 1) % SPEED_WINDOW;
  int ol = pidSide(dtS, rawL, &spdMeasL, speedMode ? spdTgtL : 0,
                   &intL, &prevErrL, &noMoveL, 1, curL);
  int orr = pidSide(dtS, rawR, &spdMeasR, speedMode ? spdTgtR : 0,
                    &intR, &prevErrR, &noMoveR, 2, curR);
  if (speedMode) {
    targetL = ol;
    targetR = orr;
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

  pinMode(ENC_L_A, INPUT_PULLUP);
  pinMode(ENC_L_B, INPUT_PULLUP);
  pinMode(ENC_R_A, INPUT_PULLUP);
  pinMode(ENC_R_B, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(ENC_L_A), isrL, CHANGE);
  attachInterrupt(digitalPinToInterrupt(ENC_R_A), isrR, CHANGE);

#if PWM_HIGH_FREQ
  TCCR1B = (TCCR1B & 0xF8) | 0x02;     // Timer1 prescaler /8 -> ~3.9 kHz
#endif

  Serial.begin(115200);
  Serial.println(F("READY2 v3"));
  lastCmdMs = millis();
  lastPidMs = millis();
  for (int i = 0; i < SPEED_WINDOW; i++) {
    histL[i] = histR[i] = 0;
    histT[i] = millis();
  }
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
      Serial.println(F("ERR: line too long -> stop"));
    }
  }

  unsigned long now = millis();

  // Watchdog: the Jetson re-sends at >= 10 Hz while it is healthy.
  if (!timedOut && (now - lastCmdMs) > CMD_TIMEOUT_MS) {
    speedMode = false;
    spdTgtL = spdTgtR = 0;
    targetL = 0;
    targetR = 0;
    timedOut = true;
    Serial.println(F("TIMEOUT -> stop"));
  }

  if (now - lastPidMs >= PID_PERIOD_MS) {
    pidUpdate(now);
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
