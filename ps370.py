"""Driver for the Stanford Research Systems PS370 high-voltage supply over RS-232.

PS370: -100 V to -20 kV, 500 uA full scale (negative polarity only).
Serial settings are fixed by the instrument: 9600 baud, 8N1, no handshaking.
The rear DB-9 is wired as DCE, so a normal USB-RS232 adapter + straight-through
cable is all you need.

Reference: SRS PS300-series manual, chapter 3 (Remote Operation).
"""

from __future__ import annotations

import threading
import time

try:
    import serial  # pyserial
except ImportError:  # allows the tests to run with the fake port only
    serial = None

# ---- Instrument constants (PS370) -------------------------------------------
FULL_SCALE_V = -20000.0          # volts (negative-only supply)
MIN_V_MAGNITUDE = 100.0          # spec'd lower end of the output range
FULL_SCALE_I = 500e-6            # amps
MAX_I_SETTING = 1.05 * FULL_SCALE_I  # ILIM / ITRP may be set to 105 % of FS

# *STB? (serial poll status byte), manual section 3.6.1
STB_BITS = {
    0: "stable",   # VSET or ILIM value is stable
    1: "vtrip",    # latched: a voltage trip occurred
    2: "itrip",    # latched: a current trip occurred
    3: "ilim",     # latched: current limit was hit
    4: "mav",
    5: "esb",
    6: "rqs",
    7: "hvon",     # high voltage is on
}

# *ESR? (standard event status), manual section 3.6.2
ESR_ERROR_MASK = (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)  # query/recall/exec/command

# LERR? codes, manual section 3.4.6
LERR_CODES = {
    0: "No error",
    10: "Illegal value (execution)",
    100: "Lost data (query)",
    101: "No data (query)",
    102: "No listener (query)",
    103: "Overflow (query)",
    110: "Illegal command (parser)",
    111: "Undefined command (parser)",
    112: "Illegal query (parser)",
    113: "Illegal set (parser)",
    114: "Null parameter (parser)",
    115: "Extra parameter (parser)",
    116: "Missing parameter (parser)",
    117: "Overflow (parser)",
    118: "Bad float (parser)",
    119: "Float overflow (parser)",
    120: "Bad integer (parser)",
    121: "Integer overflow (parser)",
    122: "Bad hex value (parser)",
    123: "Hex overflow (parser)",
    124: "Bad token integer (parser)",
    125: "Unknown token (parser)",
    126: "Syntax error (parser)",
    151: "CMF reset (device dependent)",
    152: "COP reset (device dependent)",
    153: "Illegal reset (device dependent)",
    154: "Recall error (device dependent)",
    155: "Watchdog (device dependent)",
}


class PS370Error(RuntimeError):
    """The instrument reported an error after a command."""

    def __init__(self, code: int, command: str, esr: int):
        self.code = code
        self.command = command
        self.esr = esr
        meaning = LERR_CODES.get(code, "Unknown error")
        super().__init__(f"PS370 rejected '{command}': error {code} ({meaning}), ESR={esr}")


def decode_stb(stb: int) -> dict:
    flags = {name: bool(stb >> bit & 1) for bit, name in STB_BITS.items()}
    flags["raw"] = stb
    return flags


class PS370:
    """Thin, thread-safe wrapper around the PS370 command set.

    Pass ``ser=`` to use any object with pyserial's ``write`` / ``read`` /
    ``reset_input_buffer`` / ``close`` methods (the tests use a simulator).
    """

    def __init__(self, port: str = "/dev/ttyUSB0", timeout: float = 1.0, ser=None):
        if ser is None:
            if serial is None:
                raise ImportError("pyserial is required: pip install pyserial")
            ser = serial.Serial(
                port,
                baudrate=9600,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        self.ser = ser
        self._lock = threading.RLock()

    # ---- context manager -----------------------------------------------------
    def close(self) -> None:
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- low level I/O -------------------------------------------------------
    def write(self, cmd: str) -> None:
        """Send one command line (no error check)."""
        with self._lock:
            self.ser.write((cmd + "\n").encode("ascii"))

    def _readline(self) -> str:
        # The manual allows CR or LF; read until either, skipping blank leftovers
        # (e.g. the LF of a previous CR-LF pair).
        buf = bytearray()
        while True:
            b = self.ser.read(1)
            if not b:
                break  # serial timeout
            if b in (b"\r", b"\n"):
                if buf:
                    break
                continue
            buf += b
        return buf.decode("ascii", errors="replace").strip()

    def query(self, cmd: str) -> str:
        """Send a query and return the reply line."""
        with self._lock:
            self.ser.reset_input_buffer()
            self.write(cmd)
            reply = self._readline()
        if not reply:
            raise TimeoutError(
                f"No reply to '{cmd}'. Check cable, port name, and that the supply is on."
            )
        return reply

    def query_float(self, cmd: str) -> float:
        return float(self.query(cmd))

    def query_int(self, cmd: str) -> int:
        return int(float(self.query(cmd)))

    def check_error(self, command: str = "") -> None:
        """Raise PS370Error if the last command(s) set an error bit."""
        with self._lock:
            esr = self.query_int("*ESR?")  # reading clears it
            if esr & ESR_ERROR_MASK:
                code = self.query_int("LERR?")
                raise PS370Error(code, command, esr)

    def command(self, cmd: str) -> None:
        """Send a set command, then confirm the instrument accepted it."""
        with self._lock:
            self.write(cmd)
            self.check_error(cmd)

    # ---- identification / housekeeping ---------------------------------------
    def idn(self) -> str:
        return self.query("*IDN?")

    def clear_status(self) -> None:
        self.write("*CLS")

    def reset(self) -> None:
        """Factory defaults (HV off, VSET 0, VLIM -20 kV, ILIM/ITRP 525 uA)."""
        self.command("*RST")

    # ---- safety validation ---------------------------------------------------
    def _validate_voltage(self, volts: float, limit: float | None) -> float:
        """Return the value to send for a VSET/VLIM request, or raise ValueError.

        volts : requested voltage (from the user / script)
        limit : present VLIM when validating a VSET, or None when validating VLIM
        """
        # Policy: reject rather than "fix" bad input, so typos are never silently
        # turned into a high voltage. 0 V is allowed (it is the default setpoint).
        volts = float(volts)
        if volts != volts:  # NaN
            raise ValueError("voltage must be a number")
        if volts > 0:
            raise ValueError(
                f"PS370 is negative-only; got {volts:g} V (did you mean {-volts:g}?)")
        if abs(volts) > abs(FULL_SCALE_V):
            raise ValueError(f"{volts:g} V is beyond the {FULL_SCALE_V:g} V full scale")
        if limit is not None and abs(volts) > abs(limit):
            raise ValueError(
                f"{volts:g} V exceeds the voltage limit VLIM = {limit:g} V; raise VLIM first")
        return volts

    @staticmethod
    def _validate_current(amps: float, what: str) -> float:
        if not 0 <= amps <= MAX_I_SETTING:
            raise ValueError(f"{what} must be 0..{MAX_I_SETTING * 1e6:.0f} uA, got {amps * 1e6:g} uA")
        return float(amps)

    # ---- settings ------------------------------------------------------------
    def set_voltage(self, volts: float) -> None:
        v = self._validate_voltage(volts, self.voltage_limit())
        self.command(f"VSET {v:.6g}")

    def voltage_setpoint(self) -> float:
        return self.query_float("VSET?")

    def set_voltage_limit(self, volts: float) -> None:
        v = self._validate_voltage(volts, None)
        self.command(f"VLIM {v:.6g}")

    def voltage_limit(self) -> float:
        return self.query_float("VLIM?")

    def set_current_limit(self, amps: float) -> None:
        self.command(f"ILIM {self._validate_current(amps, 'ILIM'):.6g}")

    def current_limit(self) -> float:
        return self.query_float("ILIM?")

    def set_current_trip(self, amps: float) -> None:
        self.command(f"ITRP {self._validate_current(amps, 'ITRP'):.6g}")

    def current_trip(self) -> float:
        return self.query_float("ITRP?")

    def set_mode(self) -> int:
        """0 = voltage from front panel / remote, 1 = rear-panel analog SET input."""
        return self.query_int("SMOD?")

    def set_set_mode(self, mode: int) -> None:
        self.command(f"SMOD {int(mode)}")

    # ---- output control --------------------------------------------------------
    def hv_on(self) -> None:
        try:
            self.command("HVON")
        except PS370Error as e:
            if e.code == 10:
                raise PS370Error(e.code, "HVON (is the front-panel HV switch ON?)", e.esr) from None
            raise

    def hv_off(self) -> None:
        self.command("HVOF")

    def clear_trips(self) -> None:
        self.command("TCLR")

    # ---- measurements ----------------------------------------------------------
    def read_voltage(self) -> float:
        """Measured output voltage in volts (signed, so negative for the PS370)."""
        return self.query_float("VOUT?")

    def read_current(self) -> float:
        """Measured output current in amps (always unsigned)."""
        return self.query_float("IOUT?")

    def status(self) -> dict:
        """Decoded *STB?. NOTE: reading clears the latched vtrip/itrip/ilim bits."""
        return decode_stb(self.query_int("*STB?"))

    # ---- helpers ---------------------------------------------------------------
    def ramp_to(self, target: float, rate: float = 500.0, step: float = 100.0,
                sleep=time.sleep, on_step=None) -> None:
        """Step VSET from the present setpoint to ``target`` at about ``rate`` V/s.

        The supply itself slews at ~14 kV/s, so this is for gentler ramps
        (e.g. detectors). ``on_step(v)`` is called after each step.
        """
        if rate <= 0 or step <= 0:
            raise ValueError("rate and step must be positive")
        self._validate_voltage(target, self.voltage_limit())  # fail before moving
        v = self.voltage_setpoint()
        dt = step / rate
        while abs(target - v) > 1e-9:
            delta = max(-step, min(step, target - v))
            v += delta
            self.set_voltage(v)
            if on_step:
                on_step(v)
            if abs(target - v) > 1e-9:
                sleep(dt)
