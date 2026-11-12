#!/usr/bin/env python3
"""Command-line control of an SRS PS370 high-voltage supply.

Examples (currents are in microamps, voltages in volts, negative for the PS370):
    ps370_cli.py idn
    ps370_cli.py status
    ps370_cli.py set-vlim -10000
    ps370_cli.py set-v -5000
    ps370_cli.py set-ilim 100
    ps370_cli.py on
    ps370_cli.py ramp -8000 --rate 200
    ps370_cli.py monitor --interval 1 --csv hv_log.csv
    ps370_cli.py off
    ps370_cli.py shell          # type raw commands: *IDN?, VSET -500, VOUT? ...

Port: --port, or the PS370_PORT environment variable, default /dev/ttyUSB0.
"""

from __future__ import annotations

import argparse
import os
import sys

from console import run_shell
from monitor import monitor
from ps370 import PS370, PS370Error


def cmd_status(psu: PS370, args) -> None:
    st = psu.status()
    print(f"Identity     : {psu.idn()}")
    print(f"High voltage : {'ON' if st['hvon'] else 'off'}")
    print(f"VSET         : {psu.voltage_setpoint():.1f} V")
    print(f"VLIM         : {psu.voltage_limit():.1f} V")
    print(f"ILIM         : {psu.current_limit() * 1e6:.1f} uA")
    print(f"ITRP         : {psu.current_trip() * 1e6:.1f} uA")
    print(f"VOUT         : {psu.read_voltage():.1f} V")
    print(f"IOUT         : {psu.read_current() * 1e6:.2f} uA")
    flags = [k for k in ("stable", "ilim", "vtrip", "itrip") if st[k]]
    print(f"Status flags : {', '.join(flags) or 'none'} (STB={st['raw']})")


def cmd_on(psu: PS370, args) -> None:
    vset = psu.voltage_setpoint()
    if not args.yes:
        ans = input(f"Turn ON high voltage with VSET = {vset:.0f} V? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    psu.hv_on()
    print(f"High voltage ON (VSET = {vset:.0f} V).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Control an SRS PS370 HV supply over RS-232.")
    p.add_argument("--port", default=os.environ.get("PS370_PORT", "/dev/ttyUSB0"),
                   help="serial device (default: $PS370_PORT or /dev/ttyUSB0)")
    p.add_argument("--timeout", type=float, default=1.0, help="reply timeout in seconds")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("idn", help="print *IDN? string")
    sub.add_parser("status", help="settings, readback and status flags")

    s = sub.add_parser("set-v", help="set output voltage VSET (V, negative)")
    s.add_argument("volts", type=float)
    s = sub.add_parser("set-vlim", help="set voltage limit VLIM (V, negative)")
    s.add_argument("volts", type=float)
    s = sub.add_parser("set-ilim", help="set current limit (uA)")
    s.add_argument("microamps", type=float)
    s = sub.add_parser("set-itrip", help="set current trip (uA)")
    s.add_argument("microamps", type=float)

    s = sub.add_parser("on", help="turn high voltage ON")
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("off", help="turn high voltage OFF")
    sub.add_parser("clear", help="clear voltage/current trips (TCLR)")

    s = sub.add_parser("ramp", help="step VSET to a target at a given rate")
    s.add_argument("volts", type=float)
    s.add_argument("--rate", type=float, default=500.0, help="V/s (default 500)")
    s.add_argument("--step", type=float, default=100.0, help="V per step (default 100)")

    sub.add_parser("shell", help="interactive prompt for typing raw instrument commands")

    s = sub.add_parser("monitor", help="log V, I and status until Ctrl-C")
    s.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    s.add_argument("--csv", help="append samples to this CSV file")
    s.add_argument("--samples", type=int, help="stop after N samples")
    s.add_argument("--off-on-exit", action="store_true", help="switch HV off when stopping")
    return p


def run(args, psu: PS370) -> None:
    c = args.cmd
    if c == "idn":
        print(psu.idn())
    elif c == "status":
        cmd_status(psu, args)
    elif c == "set-v":
        psu.set_voltage(args.volts)
        print(f"VSET = {psu.voltage_setpoint():.1f} V")
    elif c == "set-vlim":
        psu.set_voltage_limit(args.volts)
        print(f"VLIM = {psu.voltage_limit():.1f} V")
    elif c == "set-ilim":
        psu.set_current_limit(args.microamps * 1e-6)
        print(f"ILIM = {psu.current_limit() * 1e6:.1f} uA")
    elif c == "set-itrip":
        psu.set_current_trip(args.microamps * 1e-6)
        print(f"ITRP = {psu.current_trip() * 1e6:.1f} uA")
    elif c == "on":
        cmd_on(psu, args)
    elif c == "off":
        psu.hv_off()
        print("High voltage OFF.")
    elif c == "clear":
        psu.clear_trips()
        print("Trips cleared.")
    elif c == "ramp":
        psu.ramp_to(args.volts, rate=args.rate, step=args.step,
                    on_step=lambda v: print(f"  VSET -> {v:.0f} V"))
        print("Ramp complete.")
    elif c == "shell":
        run_shell(psu)
    elif c == "monitor":
        print("Monitoring (Ctrl-C to stop)...")
        monitor(psu, interval=args.interval, csv_path=args.csv,
                samples=args.samples, off_on_exit=args.off_on_exit)


def main(argv=None, psu_factory=None) -> int:
    args = build_parser().parse_args(argv)
    factory = psu_factory or (lambda: PS370(args.port, timeout=args.timeout))
    try:
        with factory() as psu:
            run(args, psu)
    except (PS370Error, ValueError, TimeoutError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Could not open {args.port}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
