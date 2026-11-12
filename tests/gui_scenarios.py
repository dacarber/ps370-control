"""GUI test scenarios. Each runs in its OWN process (one Tk window per process),
launched by the pytest wrappers in test_console_gui.py / test_hardware.py:

    python -m tests.gui_scenarios <scenario> [--port SIM|/dev/ttyUSB0] [--csv path] [--log-dir d]

Exit codes: 0 pass, 1 fail (message on stderr), 77 no display available.
"""

from __future__ import annotations

import argparse
import csv
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
    check(open(args.csv).readline().startswith("timestamp,unix_time,vout_V"), "CSV header")


def read_logs(app):
    """(commands rows, readings rows) from the GUI's data logs, all days."""
    import glob
    import os
    d = app.logger.log_dir
    rows = {}
    for kind in ("commands", "readings"):
        rows[kind] = []
        for f in sorted(glob.glob(os.path.join(d, f"{kind}_*.csv"))):
            rows[kind] += list(csv.DictReader(open(f)))
    return rows["commands"], rows["readings"]


def find(rows, **want):
    for i, r in enumerate(rows):
        if all(r[k] == v for k, v in want.items()):
            return i, r
    raise AssertionError(f"no command-log row matching {want}; rows: "
                         + "; ".join(f"{r['source']}|{r['command']}|{r['status']}" for r in rows))


def sim_logs(app, args):
    """Commands and readings land in the two CSVs, in order, on one clock."""
    wait_connected(app, ",SIM")
    set_via_gui(app, "vset", "-2000")
    check(pump(app, lambda: app.set_cur["vset"].get() == "-2000.0"), "VSET not applied")
    send(app, "*IDN?")
    check(pump(app, lambda: "SIM" in text(app).split("> *IDN?")[-1]), "no *IDN? reply")
    send(app, "VSET 500")                     # raw: instrument rejects
    check(pump(app, lambda: "ERROR 10" in text(app)), "VSET 500 not rejected")
    set_via_gui(app, "vset", "1000")          # driver refuses
    check(pump(app, lambda: "negative-only" in text(app)), "VSET 1000 not refused")
    app.hv_on()
    check(pump(app, lambda: app.vout_var.get().replace(",", "").startswith(("-2000", "-1999"))),
          "VOUT did not reach -2000")
    n = len(app.v_hist)
    check(pump(app, lambda: len(app.v_hist) >= n + 3), "readings stopped")
    set_via_gui(app, "itrp", "10")            # 20 uA load > 10 uA -> current trip
    check(pump(app, lambda: "CURRENT TRIP" in text(app), timeout=5), "no trip")
    pump(app, lambda: False, timeout=0.6)     # let a couple more readings arrive

    cmds, reads = read_logs(app)
    check(len(reads) >= 5, f"only {len(reads)} readings logged")
    check(reads[0].keys() >= {"timestamp", "unix_time", "vout_V", "iout_A"}, "readings columns")
    order = [
        find(cmds, source="gui", command="connected", status="EVENT"),
        find(cmds, source="gui", command="VSET -2000", status="OK"),
        find(cmds, source="console", command="*IDN?", status="REPLY"),
        find(cmds, source="console", command="VSET 500", status="ERROR"),
        find(cmds, source="gui", command="VSET 1000", status="REFUSED"),
        find(cmds, source="gui", command="HVON", status="OK"),
        find(cmds, source="gui", command="ITRP 1e-05", status="OK"),
        find(cmds, source="gui", command="CURRENT TRIP", status="EVENT"),
    ]
    idx = [i for i, _ in order]
    check(idx == sorted(idx), f"command log out of order: {idx}")
    check(order[2][1]["response"].endswith(",SIM"), "IDN reply not logged")
    check(order[3][1]["response"].startswith("10 "), "error code not logged")
    times = [float(r["unix_time"]) for r in cmds]
    check(times == sorted(times), "command times not increasing")
    # line up: readings before HVON are off, readings after it are on
    t_on = float(order[5][1]["unix_time"])
    before = [r for r in reads if float(r["unix_time"]) < t_on]
    after = [r for r in reads if float(r["unix_time"]) > t_on + 0.5]
    check(before and all(r["hv_on"] == "0" for r in before), "readings before HVON show HV on")
    check(after and any(r["hv_on"] == "1" for r in after), "no HV-on readings after HVON")
    check(any(r["itrip"] == "1" for r in reads), "trip bit not in readings log")
    # pausing stops both files
    app.log_var.set(False)
    app._toggle_logging()
    nc, nr = len(read_logs(app)[0]), len(read_logs(app)[1])
    send(app, "VSET?")
    pump(app, lambda: False, timeout=0.8)
    c2, r2 = read_logs(app)
    check(len(c2) == nc and len(r2) == nr, "logging did not pause")
    check(c2[-1]["command"] == "logging paused", "pause event missing")


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
    cmds, reads = read_logs(app)
    find(cmds, source="console", command="*IDN?", status="REPLY")
    find(cmds, source="console", command="VSET 500", status="ERROR")
    find(cmds, source="gui", command="VSET 100", status="REFUSED")
    find(cmds, source="gui", command="HVOF", status="OK")
    check(len(reads) >= 1, "no readings logged")


SCENARIOS = {f.__name__: f for f in
             (sim_end_to_end, sim_demo, any_csv, sim_logs, hw_connect, hw_setting_and_console)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=SCENARIOS)
    p.add_argument("--port", default="SIM")
    p.add_argument("--csv")
    p.add_argument("--log-dir", help="data log folder (default: $PS370_LOG_DIR / logs/)")
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

    import datalog
    import ps370_gui
    mb = ps370_gui.messagebox
    dialogs = []
    mb.askyesno = lambda *a, **k: True
    mb.askyesnocancel = lambda *a, **k: True  # on close: switch HV off, then quit
    for name in ("showerror", "showwarning", "showinfo"):
        setattr(mb, name, lambda *a, _n=name, **k: dialogs.append((_n, a)))

    app = ps370_gui.App(root, port=args.port, interval=0.25,
                        logger=datalog.DataLogger(args.log_dir, source="gui"))
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
