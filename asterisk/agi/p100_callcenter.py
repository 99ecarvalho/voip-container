#!/usr/bin/env python3
"""
p100_callcenter.py — Asterisk AGI: P100/EOX call-center simulator.

Called from extensions.conf as:  AGI(p100_callcenter.py,<mode>)
  mode: real (default) or routine

In P100 mode the device side handles the P100 frame exchange over the ANALOG
line (FXS ↔ 2N phone) BEFORE making the VoIP call, so when this call centre
answers the VoIP leg is already established.

Two sub-protocols depending on mode:

  real:
    - Answer and stay on line.
    - Send 'A' after 1 s to signal "call acknowledged".
    - Voice phase: log DTMF, send '0' on '6' (clear).

  routine (EOX handshake):
    - Answer and wait for a single digit N (0-9) from device.
    - Send back N+1 (mod 10).
    - Wait for N+2 (mod 10) from device to confirm.
    - Log success/failure and hang up.
"""

import sys
import time

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

    def wait_for_digit(self, timeout_ms=5000):
        resp = self._rpc(f'WAIT FOR DIGIT {timeout_ms}')
        # result=0 → timeout, result=-1 (or no reply) → channel gone.
        try:
            code = int(resp.split('result=')[1].split()[0])
        except (IndexError, ValueError):
            code = -1
        if code < 0:
            self.hung_up = True
            return None
        return None if code == 0 else chr(code)

    def play_dtmf(self, digit):
        self._rpc(f'EXEC SendDTMF {digit}')
        time.sleep(0.15)

    def log(self, msg):
        self._rpc(f'VERBOSE "P100-CC: {msg}" 2')


def main():
    agi = AGI()
    mode = agi.env.get('agi_arg_1', 'real').lower()
    agi.answer()
    time.sleep(1.0)

    agi.log(f'start, mode={mode}')

    if mode == 'routine':
        # EOX routine-test handshake: device sends N → CC sends N+1 → device sends N+2
        agi.log('waiting for handshake digit N')
        d = agi.wait_for_digit(10000)
        if d is None or not d.isdigit():
            agi.log(f'no handshake digit received (got {d!r}) — fail')
            agi.hangup()
            return

        n = int(d)
        n1 = (n + 1) % 10
        n2 = (n + 2) % 10
        agi.log(f'received N={n}, sending N+1={n1}')
        agi.play_dtmf(str(n1))

        d2 = agi.wait_for_digit(5000)
        if d2 is not None and d2.isdigit() and int(d2) == n2:
            agi.log(f'handshake SUCCESS: N+2={n2} confirmed')
        else:
            agi.log(f'handshake FAIL: expected N+2={n2}, got {d2!r}')

        agi.hangup()
        return

    # --- real / technician call ---
    time.sleep(0.5)
    agi.play_dtmf('A')   # "call acknowledged"
    agi.log('voice phase — waiting for hangup or clear')

    while True:
        d = agi.wait_for_digit(60000)
        if d is None:
            if agi.hung_up:
                break
            continue             # no DTMF for 60 s — stay on the line
        agi.log(f'voice DTMF: {d!r}')
        if d == '6':
            # Device signals clear → call centre acknowledges with '0'
            agi.play_dtmf('0')
        elif d == '9':
            break

    agi.log('done')


if __name__ == '__main__':
    main()
