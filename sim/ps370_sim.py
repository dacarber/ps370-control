#!/usr/bin/env python3
"""Software stand-in for an SRS PS370 so you can test without the hardware.

Run it as a virtual serial port (macOS / Linux):

    python3 sim/ps370_sim.py --link ./sim_port
    python3 ps370_cli.py --port ./sim_port status

Or use FakeSerial in-process (what the pytest suite does).

It follows the manual's rules closely enough to catch real mistakes:
positive voltages, VSET beyond VLIM, unknown commands, HVON with the
front-panel switch off, latched trip bits that clear on *STB? read, etc.
Behaviour that the manual leaves unclear is a best guess; see README.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import re
import select
import signal
import sys
import time
import tty

FULL_SCALE_V = 20000.0          # magnitude; the PS370 output is negative
FULL_SCALE_I = 500e-6
MAX_I = 1.05 * FULL_SCALE_I
SLEW_V_PER_S = 14000.0          # spec: 14 kV/s typical for PS370
INPUT_BUFFER = 128

# ESR bits
ESR_OPC, ESR_QYE, ESR_RCL, ESR_EXE, ESR_CME, ESR_URQ, ESR_PON = 0, 2, 3, 4, 5, 6, 7

CMD_RE = re.compile(r"^(\*?[A-Z]{3,4})(\?)?\s*(.*)$")


def sci(x: float, digits: int = 4) -> str:
    """Format like the manual's examples: -1.8998E4, 4.78E-4."""
    if x == 0:
        return "0"
    exp = int(math.floor(math.log10(abs(x))))
    mant = x / 10 ** exp
    if abs(round(mant, digits)) >= 10:  # rounding pushed it to 10.000
        mant /= 10
        exp += 1
    return f"{mant:.{digits}f}E{exp}"


class SimPS370:
    def __init__(self, load_ohms: float = 100e6, hv_switch: bool = True,
                 clock=time.monotonic, noise: bool = True, seed: int | None = None,
                 serial_number: str = "000000"):
        self.load_ohms = load_ohms
        self.hv_switch = hv_switch
        self.clock = clock
        self.noise = noise
        self.rng = random.Random(seed)
        self.serial_number = serial_number
        self.saved: dict[int, dict] = {}
        self._defaults()
        self.esr = 1 << ESR_PON
        self.ese = 0
        self.sre = 0
        self.psc = 1
        self.lerr = 0
        self._t = self.clock()

    # ---- state -----------------------------------------------------------------
    def _defaults(self):
        self.vset = 0.0
        self.vlim = -FULL_SCALE_V
        self.ilim = MAX_I
        self.itrp = MAX_I
        self.tmod = 0        # 0 = manual reset, 1 = auto (manual's text is ambiguous)
        self.smod = 0
        self.hv_on = False
        self.vout = 0.0
        self.iout = 0.0
        self.latched = 0     # STB bits 1..3
        self.stable = False

    def _settings(self):
        return {k: getattr(self, k) for k in ("vset", "vlim", "ilim", "itrp", "tmod", "smod")}

    # ---- physics ---------------------------------------------------------------
    def update(self):
        now = self.clock()
        dt = max(0.0, now - self._t)
        self._t = now
        target = self.vset if self.hv_on else 0.0
        step = SLEW_V_PER_S * dt
        if abs(target - self.vout) <= step:
            self.vout = target
        else:
            self.vout += math.copysign(step, target - self.vout)

        i_load = abs(self.vout) / self.load_ohms if self.load_ohms > 0 else float("inf")
        if self.hv_on and i_load >= self.itrp > 0:
            self.latched |= 1 << 2          # itrip
            self.vout, i_load = 0.0, 0.0
            if self.tmod == 0:              # manual reset: stays off
                self.hv_on = False
        elif self.hv_on and i_load > self.ilim:
            self.latched |= 1 << 3          # ilim: constant-current mode
            i_load = self.ilim
            self.vout = -self.ilim * self.load_ohms
        self.iout = i_load
        self.stable = self.hv_on and abs(self.vout - self.vset) < 2.0 or (
            self.hv_on and bool(self.latched & 8) and i_load == self.ilim)

    def measured_v(self) -> float:
        v = self.vout + (self.rng.gauss(0, 0.3) if self.noise and self.hv_on else 0.0)
        return round(v, 1)

    def measured_i(self) -> float:
        i = self.iout + (self.rng.gauss(0, 0.1e-6) if self.noise and self.hv_on else 0.0)
        return max(0.0, round(i, 8))

    def stb(self) -> int:
        b = self.latched
        if self.stable:
            b |= 1
        if self.esr & self.ese:
            b |= 1 << 5
        if b & self.sre & ~(1 << 6):
            b |= 1 << 6
        if self.hv_on:
            b |= 1 << 7
        return b

    # ---- errors ----------------------------------------------------------------
    def _cmd_err(self, code):
        self.lerr = code
        self.esr |= 1 << ESR_CME

    def _exe_err(self, code=10):
        self.lerr = code
        self.esr |= 1 << ESR_EXE

    # ---- parser ----------------------------------------------------------------
    def handle_line(self, line: str) -> list[str]:
        """Execute one terminated line; return reply strings (one per query)."""
        self.update()
        replies: list[str] = []
        for part in line.split(";"):
            part = part.strip()
            if not part:
                continue  # null command
            m = CMD_RE.match(part.upper())
            if not m:
                self._cmd_err(126)
                continue
            name, q, arg = m.group(1), bool(m.group(2)), m.group(3).strip()
            r = self._dispatch(name, q, arg)
            if r is not None:
                replies.append(r)
        return replies

    def _float(self, arg):
        if not arg:
            self._cmd_err(116)
            return None
        if "," in arg:
            self._cmd_err(115)
            return None
        try:
            return float(arg)
        except ValueError:
            self._cmd_err(118)
            return None

    def _int(self, arg):
        v = self._float(arg)
        if v is None:
            return None
        if v != int(v):
            self._cmd_err(120)
            return None
        return int(v)

    def _dispatch(self, name, q, arg):  # noqa: C901 - flat command table
        # ---- query-only
        query_only = {
            "*IDN": lambda: f"StanfordResearchSystems,PS370,{self.serial_number},SIM",
            "VOUT": lambda: sci(self.measured_v()),
            "IOUT": lambda: sci(self.measured_i(), 2),
            "LERR": self._read_lerr,
        }
        if name in query_only:
            if not q:
                self._cmd_err(113)
                return None
            return query_only[name]()

        # ---- set-only
        if name in ("HVON", "HVOF", "TCLR", "*RST", "*CLS", "*SAV", "*RCL"):
            if q:
                self._cmd_err(112)
                return None
            return self._set_only(name, arg)

        # ---- set or query
        if name in ("VSET", "VLIM", "ILIM", "ITRP"):
            if q:
                return {
                    "VSET": lambda: f"{self.vset:.0f}",
                    "VLIM": lambda: sci(self.vlim),
                    "ILIM": lambda: sci(self.ilim, 2),
                    "ITRP": lambda: sci(self.itrp, 2),
                }[name]()
            x = self._float(arg)
            if x is not None:
                self._set_value(name, x)
            return None
        if name in ("SMOD", "TMOD", "*PSC"):
            attr = {"SMOD": "smod", "TMOD": "tmod", "*PSC": "psc"}[name]
            if q:
                return str(getattr(self, attr))
            i = self._int(arg)
            if i is None:
                return None
            if i not in (0, 1):
                return self._exe_err()
            if name == "SMOD" and i != self.smod and self.hv_on:
                self.hv_on = False   # manual: changing SMOD with HV on turns it off
            setattr(self, attr, i)
            return None
        if name in ("*ESE", "*SRE"):
            attr = name[1:].lower()
            if q:
                return str(getattr(self, attr))
            i = self._int(arg)
            if i is not None:
                if 0 <= i <= 255:
                    setattr(self, attr, i)
                else:
                    self._exe_err()
            return None
        if name == "*ESR":
            if not q:
                return self._cmd_err(113)
            if arg:
                bit = self._int(arg)
                if bit is None:
                    return None
                val = self.esr >> bit & 1
                self.esr &= ~(1 << bit)
                return str(val)
            val, self.esr = self.esr, 0
            return str(val)
        if name == "*STB":
            if not q:
                return self._cmd_err(113)
            val = self.stb()
            self.latched = 0  # bits 1, 2, 3 clear on read
            if arg:
                bit = self._int(arg)
                return None if bit is None else str(val >> bit & 1)
            return str(val)
        if name == "*OPC":
            if q:
                return "1"
            self.esr |= 1 << ESR_OPC
            return None

        self._cmd_err(111)  # undefined command
        return None

    def _read_lerr(self):
        code, self.lerr = self.lerr, 0
        return str(code)

    def _set_value(self, name, x):
        if name == "VSET":
            if self.smod == 1:
                return self._exe_err()
            if x > 0 or abs(x) > FULL_SCALE_V or abs(x) > abs(self.vlim):
                return self._exe_err()
            self.vset = round(x)                 # 1 V resolution
        elif name == "VLIM":
            if x > 0 or abs(x) > FULL_SCALE_V or abs(x) < abs(self.vset):
                return self._exe_err()
            self.vlim = round(x)
        else:  # ILIM / ITRP
            if not 0 <= x <= MAX_I + 1e-12:
                return self._exe_err()
            setattr(self, name.lower(), round(x, 6))  # 1 uA resolution
        return None

    def _set_only(self, name, arg):
        if name == "HVON":
            if not self.hv_switch:
                return self._exe_err()
            self.latched &= ~0b110
            self.hv_on = True
        elif name == "HVOF":
            self.hv_on = False
        elif name == "TCLR":
            self.latched &= ~0b110
        elif name == "*RST":
            self._defaults()
        elif name == "*CLS":
            self.esr = 0
            self.latched = 0
            self.lerr = 0
        elif name in ("*SAV", "*RCL"):
            i = self._int(arg)
            if i is None:
                return None
            if name == "*SAV":
                if not 1 <= i <= 9:
                    return self._exe_err()
                self.saved[i] = self._settings()
            else:
                if i == 0:
                    self._defaults()
                elif i in self.saved:
                    hv = self.hv_on
                    self.__dict__.update(self.saved[i])
                    self.hv_on = hv
                else:
                    self.esr |= 1 << ESR_RCL
                    self.lerr = 154
        return None


class LineBuffer:
    """Accumulates bytes; yields complete lines on CR or LF (128-char limit)."""

    def __init__(self, sim: SimPS370):
        self.sim = sim
        self.buf = bytearray()

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            if b in (0x0A, 0x0D):
                line = self.buf.decode("ascii", errors="replace")
                self.buf.clear()
                for r in self.sim.handle_line(line):
                    out += (r + "\r\n").encode("ascii")
            else:
                self.buf.append(b)
                if len(self.buf) > INPUT_BUFFER:   # overflow: discard everything
                    self.buf.clear()
                    self.sim.esr |= 1 << ESR_QYE
                    self.sim.lerr = 117
        return bytes(out)


class FakeSerial:
    """Minimal pyserial look-alike backed by SimPS370 (for tests)."""

    def __init__(self, sim: SimPS370 | None = None):
        self.sim = sim or SimPS370(noise=False)
        self.lines = LineBuffer(self.sim)
        self._out = bytearray()
        self.is_open = True
        self.sent: list[str] = []   # every command line written, for assertions

    def write(self, data: bytes) -> int:
        self.sent.extend(s for s in data.decode().splitlines() if s)
        self._out += self.lines.feed(data)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        chunk = bytes(self._out[:n])
        del self._out[:n]
        return chunk

    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        self.is_open = False


# ---- virtual serial port -------------------------------------------------------
def serve_pty(sim: SimPS370, link: str | None, verbose: bool) -> None:
    master, slave = os.openpty()
    tty.setraw(slave)              # no echo / line editing on the device side
    name = os.ttyname(slave)
    if link:
        if os.path.islink(link):
            os.unlink(link)
        os.symlink(name, link)
    print(f"PS370 simulator listening on {name}" + (f" (link: {link})" if link else ""),
          flush=True)
    print(f"  load = {sim.load_ohms / 1e6:g} MOhm, front-panel HV switch "
          f"{'ON' if sim.hv_switch else 'OFF'}. Ctrl-C to stop.", flush=True)
    lines = LineBuffer(sim)
    # `kill` sends SIGTERM; turn it into KeyboardInterrupt so the link is removed.
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while True:
            r, _, _ = select.select([master], [], [], 0.5)
            if not r:
                continue
            try:
                data = os.read(master, 1024)
            except OSError:
                time.sleep(0.05)
                continue
            if verbose:
                print(f"<- {data!r}", flush=True)
            reply = lines.feed(data)
            if reply:
                if verbose:
                    print(f"-> {reply!r}", flush=True)
                os.write(master, reply)
    except KeyboardInterrupt:
        pass
    finally:
        if link and os.path.islink(link):
            os.unlink(link)
        os.close(master)
        os.close(slave)


def main(argv=None):
    p = argparse.ArgumentParser(description="Simulated SRS PS370 on a virtual serial port")
    p.add_argument("--link", help="create a symlink to the pty at this path (e.g. ./sim_port)")
    p.add_argument("--load-mohm", type=float, default=100.0,
                   help="resistive load in megaohms (default 100 -> 50 uA at -5 kV)")
    p.add_argument("--hv-switch", choices=["on", "off"], default="on",
                   help="front-panel HV enable switch position")
    p.add_argument("--no-noise", action="store_true", help="exact readings")
    p.add_argument("-v", "--verbose", action="store_true", help="print raw traffic")
    a = p.parse_args(argv)
    sim = SimPS370(load_ohms=a.load_mohm * 1e6, hv_switch=a.hv_switch == "on",
                   noise=not a.no_noise)
    serve_pty(sim, a.link, a.verbose)


if __name__ == "__main__":
    sys.exit(main())
