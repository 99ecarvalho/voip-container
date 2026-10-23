#!/usr/bin/env python3
"""
tg_callcenter.py — Asterisk AGI: Telegärtner call-center simulator.

Called from extensions.conf as:  AGI(tg_callcenter.py,<call_type>)
  call_type: 12=real (default), 13=technician, 14=routine, 22=routine

Protocol flow (call-center perspective):
  1. Answer; wait 1.5 s for RTP to stabilise
  2. Send DTMF 'C' (trigger) up to 5 times, 2 s apart
  3. Collect 2-digit call-type echo from device (12 / 13 / 14 / 22)
  4. Send 'A' (acknowledge)
  5. Collect device number: DTMF digits until 'C', then 1 checksum digit
  6. Validate checksum (TG char→value mapping, sum mod 16)
  7. Send 'A' if OK, '#' if bad
  8. Collect device type (same format)
  9. Parse firmware from last 4 digits of device-type string (MMNN → vMM.NN)
 10. Send '2','9' (D29) if firmware > 06.02, else '3','0' (D30)
 11. Voice phase: forward 'A'/'6'/'9' events; send '0' on '6' (clear)

Checksum algorithm:
  - DTMF '0'→10, '1'→1, …, '9'→9, 'A'→11, 'B'→12, 'C'→13, 'D'→14, '#'→15, '*'→15
  - sum = Σ values; checksum_digit = tg_hex(sum & 0xF)
"""

import sys
import time

# --- TG DTMF ↔ numeric value ------------------------------------------------

TG_VALUE = {
    '0': 10, '1': 1, '2': 2, '3': 3, '4': 4,
    '5': 5,  '6': 6, '7': 7, '8': 8, '9': 9,
    'A': 11, 'B': 12, 'C': 13, 'D': 14,
    '*': 15, '#': 15,
}

def _tg_hex(v):
    """Map 4-bit int → TG checksum DTMF char."""
    v &= 0xF
    if v == 0:  return '0'   # corner case: 0 mapped back to '0'
    if 1 <= v <= 9: return str(v)
    if v == 10: return '0'
    return chr(ord('A') + v - 11)  # 11→A, 12→B, 13→C, 14→D, 15→* (never sent)

def _tg_checksum(digits):
    """Compute TG checksum of a string of DTMF digits."""
    return sum(TG_VALUE.get(c.upper(), 0) for c in digits) & 0xF

def _tg_valid(digits, checksum_char):
    """Return True if checksum_char matches the TG checksum of digits."""
    expected = _tg_checksum(digits)
    received = TG_VALUE.get(checksum_char.upper(), -1)
    # Normalise: value 10 → index 10; but expected is already 0-15
    # getHex maps 10→'0', so TG_VALUE['0']=10 means checksum_char='0' has value 10
    # We need: expected == received (using TG_VALUE for received)
    return expected == received

# --- AGI helper ---------------------------------------------------------------

class AGI:
    def __init__(self):
        self.env = {}
        self.hung_up = False
        while True:
            line = sys.stdin.readline()
            if not line or not line.strip():
                break
            if ':' in line:
                k, v = line.split(':', 1)
                self.env[k.strip()] = v.strip()

    def _rpc(self, cmd):
        sys.stdout.write(cmd + '\n')
        sys.stdout.flush()
        return sys.stdin.readline().strip()

    def answer(self):
        self._rpc('ANSWER')

    def hangup(self):
        self._rpc('HANGUP')

    def wait_for_digit(self, timeout_ms=3000):
        resp = self._rpc(f'WAIT FOR DIGIT {timeout_ms}')
        # result=0 → timeout, result=-1 (or no reply) → channel gone.
        try:
            code = int(resp.split('result=')[1].split()[0])
        except (IndexError, ValueError):
            code = -1
        if code < 0:
            self.hung_up = True
            return None
        return None if code == 0 else chr(code).upper()

    def play_dtmf(self, digit, gap_ms=150):
        self._rpc(f'EXEC SendDTMF {digit}')
        time.sleep(gap_ms / 1000.0)

    def log(self, msg):
        self._rpc(f'VERBOSE "TG-CC: {msg}" 2')

    def collect_until(self, terminator, item_timeout_ms=4000, max_digits=20):
        """Collect digits until terminator; returns (digits_before, found_terminator)."""
        buf = []
        for _ in range(max_digits):
            d = self.wait_for_digit(item_timeout_ms)
            if d is None:
                return ''.join(buf), False
            if d == terminator:
                return ''.join(buf), True
            buf.append(d)
        return ''.join(buf), False


# --- Main protocol ------------------------------------------------------------

def main():
    agi = AGI()
    call_type_response = agi.env.get('agi_arg_1', '12')   # what CC sends to device
    agi.answer()
    time.sleep(1.5)

    agi.log(f'start, responding with call-type={call_type_response}')

    # --- Phase 1: trigger exchange ---
    received_call_type = ''
    for attempt in range(5):
        agi.log(f'sending trigger C (attempt {attempt + 1})')
        agi.play_dtmf('C')
        d1 = agi.wait_for_digit(2000)
        if d1 is None:
            if agi.hung_up:
                return
            continue
        d2 = agi.wait_for_digit(1500)
        if d2 is None:
            received_call_type = d1
        else:
            received_call_type = d1 + d2
        break

    if len(received_call_type) < 2:
        agi.log('no call-type response received — hanging up')
        agi.hangup()
        return

    agi.log(f'device call-type: {received_call_type}')

    # CC sends its own 2-digit call-type (the one configured for this extension)
    for ch in call_type_response:
        agi.play_dtmf(ch)
    time.sleep(0.2)

    # --- Phase 2: acknowledge ---
    agi.play_dtmf('A')
    time.sleep(0.3)

    # --- Phase 3: device number ---
    agi.log('collecting device number')
    digits, found = agi.collect_until('C', item_timeout_ms=5000, max_digits=16)
    if not found:
        agi.log(f'device number timeout, got={digits!r} — hanging up')
        agi.hangup()
        return

    cs = agi.wait_for_digit(3000)
    agi.log(f'device number={digits!r}  checksum={cs!r}')

    if cs and _tg_valid(digits, cs):
        agi.log('device number OK')
        agi.play_dtmf('A')
    else:
        agi.log('device number CHECKSUM FAIL')
        agi.play_dtmf('#')
    time.sleep(0.3)

    # --- Phase 4: device type ---
    agi.log('collecting device type')
    digits, found = agi.collect_until('C', item_timeout_ms=5000, max_digits=20)
    device_type = digits
    cs = agi.wait_for_digit(3000)
    agi.log(f'device type={device_type!r}  checksum={cs!r}')

    if cs and _tg_valid(device_type, cs):
        agi.log('device type OK')
    else:
        agi.log('device type CHECKSUM FAIL (continuing anyway)')
    time.sleep(0.3)

    # --- Phase 5: firmware version → voice-start signal ---
    fw_maj, fw_min = 6, 2   # default when unparseable: sends D30
    if len(device_type) >= 4:
        try:
            fw_maj = int(device_type[-4:-2])
            fw_min = int(device_type[-2:])
        except ValueError:
            pass

    agi.log(f'firmware v{fw_maj:02d}.{fw_min:02d}')
    if fw_maj * 100 + fw_min > 602:
        agi.log('sending D29')
        agi.play_dtmf('2')
        agi.play_dtmf('9')
    else:
        agi.log('sending D30')
        agi.play_dtmf('3')
        agi.play_dtmf('0')

    # --- Phase 6: voice phase ---
    agi.log('voice phase')
    while True:
        d = agi.wait_for_digit(60000)
        if d is None:
            if agi.hung_up:
                break
            continue             # no DTMF for 60 s — stay on the line
        agi.log(f'voice DTMF: {d}')
        if d == '6':
            agi.play_dtmf('0')   # clear acknowledgement
        elif d == '9':
            break                # device requesting hangup

    agi.log('done')


if __name__ == '__main__':
    main()
