"""GUI test scenarios. Each runs in its OWN process (one Tk window per process),
launched by the pytest wrappers in test_console_gui.py / test_hardware.py:

    python -m tests.gui_scenarios <scenario> [--port SIM|/dev/ttyUSB0] [--csv path]

Exit codes: 0 pass, 1 fail (message on stderr), 77 no display available.
"""

from __future__ import annotations

import argparse
import faulthandler
import sys
import time
import traceback

NO_DISPLAY = 77


def pump(app, cond, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def text(app) -> str:
    return app.console.get("1.0", "end")


def check(ok, msg):
    if not ok:
        raise AssertionError(msg)


def wait_connected(app, model_suffix=None):
    check(pump(app, lambda: app.idn_var.get().startswith("StanfordResearchSystems,PS370")),
          f"no identity in GUI: {app.idn_var.get()!r}; dialogs: {app.dialogs}")
    if model_suffix:
        check(app.idn_var.get().endswith(model_suffix), app.idn_var.get())
    check(pump(app, lambda: app.set_cur["vlim"].get() not in ("—", "")), "settings not read")


def set_via_gui(app, key, value):
    app.set_entry[key].delete(0, "end")
    app.set_entry[key].insert(0, value)
    app.apply_setting(key)


def send(app, cmd):
    app.cmd_entry.delete(0, "end")
    app.cmd_entry.insert(0, cmd)
    app.send_console()


# ---- simulator scenarios ---------------------------------------------------------------
def sim_end_to_end(app, args):
    wait_connected(app, ",SIM")
    check(app.set_cur["vlim"].get() == "-20000.0", "default VLIM")
    set_via_gui(app, "vset", "-3000")
    check(pump(app, lambda: app.set_cur["vset"].get() == "-3000.0"), "VSET not applied")
    set_via_gui(app, "ilim", "100")
    check(pump(app, lambda: app.set_cur["ilim"].get() == "100.0"), "ILIM not applied")
    set_via_gui(app, "vset", "500")
    check(pump(app, lambda: "negative-only" in text(app)), "positive VSET not refused")
    check(app.set_cur["vset"].get() == "-3000.0", "VSET changed by refused value")
    app.hv_on()
    check(pump(app, lambda: app.vout_var.get().replace(",", "").startswith(("-3000", "-2999"))),
          f"VOUT did not reach -3000: {app.vout_var.get()}")
    check(app.lights["hvon"].cget("bg") != app.OFF_BG, "HV light off")
    check(len(app.v_hist) > 1, "chart history empty")
    send(app, "*IDN?")
    check(pump(app, lambda: "StanfordResearchSystems" in text(app)), "console *IDN? failed")
    send(app, "VSET -1500")
    check(pump(app, lambda: app.set_cur["vset"].get() == "-1500.0"), "console VSET failed")
    app.hv_off()
    check(pump(app, lambda: app.lights["hvon"].cget("bg") == app.OFF_BG), "HV OFF failed")


def sim_demo(app, args):
    wait_connected(app, ",SIM")
    app.start_demo(speed=0.1)
    check(pump(app, lambda: "[demo] finished" in text(app), timeout=15), "demo did not finish")
    t = text(app)
    check("negative-only" in t and "exceeds the voltage limit" in t, "refusals missing")
    check("CURRENT TRIP" in t, "trip not shown")
    check(app.set_cur["vset"].get() == "-5000.0", app.set_cur["vset"].get())


def any_csv(app, args):
    wait_connected(app)
    app._open_csv(args.csv)
    check(pump(app, lambda: open(args.csv).read().count("\n") >= 3, timeout=10),
          "CSV rows not written")
    check(open(args.csv).readline().startswith("timestamp,vout_V"), "CSV header")


# ---- hardware scenarios (HV stays OFF) --------------------------------------------------
def hw_connect(app, args):
    wait_connected(app)
    check(pump(app, lambda: len(app.v_hist) >= 3, timeout=10), "no readings arrived")
    check(app.poll_failures == 0, f"{app.poll_failures} failed readings")
    check(app.lights["hvon"].cget("bg") == app.OFF_BG, "HV light is ON")


def hw_setting_and_console(app, args):
    wait_connected(app)
    check(app.lights["hvon"].cget("bg") == app.OFF_BG or not app.v_hist, "HV is ON")
    if abs(float(app.set_cur["vlim"].get())) >= 100:
        set_via_gui(app, "vset", "-100")
        check(pump(app, lambda: app.set_cur["vset"].get() == "-100.0"), "VSET -100 not applied")
    set_via_gui(app, "vset", "100")
    check(pump(app, lambda: "negative-only" in text(app)), "positive VSET not refused")
    send(app, "*IDN?")
    check(pump(app, lambda: text(app).count("StanfordResearchSystems,PS370") >= 1),
          "console *IDN? failed")
    send(app, "VSET 500")  # raw: the instrument itself must reject it
    check(pump(app, lambda: "ERROR 10" in text(app)), "instrument did not reject VSET 500")
    send(app, "VOUT?; IOUT?")
    before = text(app)
    check(pump(app, lambda: text(app) != before), "no reply to VOUT?; IOUT?")
    app.hv_off()
    check(pump(app, lambda: text(app).rstrip().endswith("OK")), "HV OFF not acknowledged")


SCENARIOS = {f.__name__: f for f in
             (sim_end_to_end, sim_demo, any_csv, hw_connect, hw_setting_and_console)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=SCENARIOS)
    p.add_argument("--port", default="SIM")
    p.add_argument("--csv")
    p.add_argument("--timeout", type=float, default=60)
    args = p.parse_args(argv)
    faulthandler.dump_traceback_later(args.timeout, exit=True)  # never hang silently

    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"no display: {e}", file=sys.stderr)
        return NO_DISPLAY
    root.withdraw()

    import ps370_gui
    mb = ps370_gui.messagebox
    dialogs = []
    mb.askyesno = lambda *a, **k: True
    mb.askyesnocancel = lambda *a, **k: True  # on close: switch HV off, then quit
    for name in ("showerror", "showwarning", "showinfo"):
        setattr(mb, name, lambda *a, _n=name, **k: dialogs.append((_n, a)))

    app = ps370_gui.App(root, port=args.port, interval=0.25)
    app.dialogs = dialogs
    rc = 0
    try:
        SCENARIOS[args.scenario](app, args)
        print(f"PASS {args.scenario}")
    except Exception:
        traceback.print_exc()
        print("---- GUI console ----\n" + text(app), file=sys.stderr)
        rc = 1
    finally:
        app.on_close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
