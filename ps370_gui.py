#!/usr/bin/env python3
"""Tkinter GUI for the SRS PS370 high-voltage supply.

    python3 ps370_gui.py                     # pick the port in the window
    python3 ps370_gui.py --port /dev/ttyUSB0
    python3 ps370_gui.py --port SIM          # built-in simulator, no hardware

Panels: connection, live readout + status lights, settings, HV on/off,
strip chart, CSV logging, and a console for typing raw instrument commands.
All serial I/O runs in one background thread so the window never freezes.
Esc switches the high voltage OFF from anywhere in the window.
"""

from __future__ import annotations

import argparse
import csv
import os
import queue
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from console import execute_line
from monitor import CSV_FIELDS, take_sample
from ps370 import PS370

SIM_PORT = "SIM (built-in simulator)"
HISTORY = 600  # samples kept on the chart


def list_ports() -> list[str]:
    ports = []
    try:
        from serial.tools import list_ports as lp
        ports = sorted(p.device for p in lp.comports())
    except Exception:
        pass
    return ports + [SIM_PORT]


def open_psu(port: str, timeout: float = 1.0) -> PS370:
    if port.strip().upper().startswith("SIM"):
        from sim.ps370_sim import FakeSerial, SimPS370
        return PS370(ser=FakeSerial(SimPS370(noise=True, load_ohms=100e6)))
    return PS370(port, timeout=timeout)


class Worker(threading.Thread):
    """Owns the PS370. The UI submits jobs; results and samples come back on `events`."""

    def __init__(self, psu: PS370, events: queue.Queue, interval: float):
        super().__init__(daemon=True)
        self.psu = psu
        self.events = events
        self.interval = interval
        self.jobs: queue.Queue = queue.Queue()

    def submit(self, fn, callback=None) -> None:
        """Run fn(psu) on the worker; callback(result, error) later runs on the UI thread."""
        self.jobs.put((fn, callback))

    def stop(self) -> None:
        self.jobs.put((None, None))  # sentinel: finish queued jobs (e.g. HV off) first

    def run(self) -> None:
        next_poll = time.monotonic()
        while True:
            try:
                fn, cb = self.jobs.get(timeout=max(0.0, next_poll - time.monotonic()))
            except queue.Empty:
                pass
            else:
                if fn is None:
                    break
                try:
                    res, err = fn(self.psu), None
                except Exception as e:  # reported in the UI, never kills the thread
                    res, err = None, e
                if cb:
                    self.events.put(("result", cb, res, err))
                elif err:
                    self.events.put(("error", err))
                continue
            try:
                self.events.put(("sample", take_sample(self.psu)))
            except Exception as e:
                self.events.put(("poll_error", e))
            next_poll = time.monotonic() + self.interval
        try:
            self.psu.close()
        except Exception:
            pass


def read_settings(psu: PS370) -> dict:
    return {
        "vset": psu.voltage_setpoint(),
        "vlim": psu.voltage_limit(),
        "ilim": psu.current_limit(),
        "itrp": psu.current_trip(),
    }


class App:
    # name, label, unit, scale (display units per instrument unit), driver setter
    SETTINGS = [
        ("vset", "Voltage set (VSET)", "V", 1.0, "set_voltage"),
        ("vlim", "Voltage limit (VLIM)", "V", 1.0, "set_voltage_limit"),
        ("ilim", "Current limit (ILIM)", "µA", 1e6, "set_current_limit"),
        ("itrp", "Current trip (ITRP)", "µA", 1e6, "set_current_trip"),
    ]
    LIGHTS = [("hvon", "HV ON", "#e03131"), ("stable", "STABLE", "#2f9e44"),
              ("ilim", "I-LIMIT", "#f08c00"), ("vtrip", "V-TRIP", "#e03131"),
              ("itrip", "I-TRIP", "#e03131")]
    OFF_BG = "#d9d9d9"

    def __init__(self, root: tk.Tk, port: str | None = None, interval: float = 1.0,
                 csv_path: str | None = None):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.v_hist: deque = deque(maxlen=HISTORY)
        self.i_hist: deque = deque(maxlen=HISTORY)
        self.trips = {"vtrip": False, "itrip": False}  # sticky until "Clear trips"
        self.hv_is_on = False
        self.settings: dict = {}
        self.poll_failures = 0
        self.csv_fh = None
        self.csv_writer = None
        self.history: list[str] = []
        self.hist_idx = 0

        root.title("PS370 High Voltage Control")
        root.minsize(820, 640)
        self._build()
        self._set_connected(False)
        root.bind_all("<Escape>", lambda e: self.hv_off())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._after_ids: list[str] = []
        self._drain_id = root.after(50, self._drain)

        if csv_path:
            self._open_csv(csv_path)
        if port:
            self.port_var.set(SIM_PORT if port.upper().startswith("SIM") else port)
            self.connect()
        self.interval_var.set(f"{interval:g}")

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        r = self.root
        r.columnconfigure(0, weight=1)
        r.rowconfigure(2, weight=1)
        r.rowconfigure(3, weight=1)
        big = ("Helvetica", 28, "bold")

        # connection bar
        bar = ttk.Frame(r, padding=(8, 8, 8, 0))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Port").pack(side="left")
        self.port_var = tk.StringVar(value=list_ports()[0])
        self.port_box = ttk.Combobox(bar, textvariable=self.port_var, width=28,
                                     values=list_ports(), postcommand=self._refresh_ports)
        self.port_box.pack(side="left", padx=4)
        self.connect_btn = ttk.Button(bar, text="Connect", command=self.toggle_connect)
        self.connect_btn.pack(side="left")
        self.idn_var = tk.StringVar(value="Not connected")
        ttk.Label(bar, textvariable=self.idn_var, foreground="#555").pack(side="left", padx=10)

        # middle: readout (left) + settings (right)
        mid = ttk.Frame(r, padding=8)
        mid.grid(row=1, column=0, sticky="ew")
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        read = ttk.LabelFrame(mid, text="Output", padding=8)
        read.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.vout_var = tk.StringVar(value="---- V")
        self.iout_var = tk.StringVar(value="---- µA")
        ttk.Label(read, textvariable=self.vout_var, font=big, foreground="#c92a2a").grid(
            row=0, column=0, columnspan=5, sticky="w")
        ttk.Label(read, textvariable=self.iout_var, font=big, foreground="#1864ab").grid(
            row=1, column=0, columnspan=5, sticky="w")
        self.lights = {}
        for k, (key, text, _) in enumerate(self.LIGHTS):
            lab = tk.Label(read, text=text, width=8, bg=self.OFF_BG, fg="black",
                           relief="groove", padx=2, pady=3)
            lab.grid(row=2, column=k, padx=2, pady=(8, 4))
            self.lights[key] = lab
        btns = ttk.Frame(read)
        btns.grid(row=3, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.on_btn = ttk.Button(btns, text="HV ON…", command=self.hv_on)
        self.off_btn = ttk.Button(btns, text="HV OFF  (Esc)", command=self.hv_off)
        self.clr_btn = ttk.Button(btns, text="Clear trips", command=self.clear_trips)
        for b in (self.on_btn, self.off_btn, self.clr_btn):
            b.pack(side="left", padx=(0, 6))

        sett = ttk.LabelFrame(mid, text="Settings", padding=8)
        sett.grid(row=0, column=1, sticky="nsew")
        self.set_cur = {}
        self.set_entry = {}
        self.set_btns = []
        for row, (key, label, unit, _, _) in enumerate(self.SETTINGS):
            ttk.Label(sett, text=label).grid(row=row, column=0, sticky="w")
            cur = tk.StringVar(value="—")
            ttk.Label(sett, textvariable=cur, width=10, anchor="e").grid(row=row, column=1)
            ttk.Label(sett, text=unit, width=3).grid(row=row, column=2, sticky="w")
            e = ttk.Entry(sett, width=10)
            e.grid(row=row, column=3, padx=4, pady=2)
            e.bind("<Return>", lambda ev, k=key: self.apply_setting(k))
            b = ttk.Button(sett, text="Set", width=5, command=lambda k=key: self.apply_setting(k))
            b.grid(row=row, column=4)
            self.set_cur[key], self.set_entry[key] = cur, e
            self.set_btns.append(b)
        opts = ttk.Frame(sett)
        opts.grid(row=len(self.SETTINGS), column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(opts, text="Poll every").pack(side="left")
        self.interval_var = tk.StringVar(value="1")
        sp = ttk.Spinbox(opts, from_=0.2, to=60, increment=0.5, width=5,
                         textvariable=self.interval_var)
        sp.pack(side="left", padx=3)
        ttk.Label(opts, text="s").pack(side="left")
        self.interval_var.trace_add("write", lambda *a: self._interval_changed())
        self.log_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Log to CSV", variable=self.log_var,
                        command=self._toggle_csv).pack(side="left", padx=(14, 3))
        self.csv_label = tk.StringVar(value="")
        ttk.Label(opts, textvariable=self.csv_label, foreground="#555").pack(side="left")

        # chart
        chart_frame = ttk.LabelFrame(r, text=f"History (last {HISTORY} samples)", padding=4)
        chart_frame.grid(row=2, column=0, sticky="nsew", padx=8)
        self.chart = tk.Canvas(chart_frame, bg="white", height=200, highlightthickness=0)
        self.chart.pack(fill="both", expand=True)
        self.chart.bind("<Configure>", lambda e: self._draw_chart())

        # console
        con = ttk.LabelFrame(r, text="Command console  (type 'help'; ↑/↓ for history)", padding=4)
        con.grid(row=3, column=0, sticky="nsew", padx=8, pady=8)
        con.columnconfigure(0, weight=1)
        con.rowconfigure(0, weight=1)
        self.console = ScrolledText(con, height=8, font="TkFixedFont", state="disabled",
                                    wrap="word", bg="white", fg="black")
        self.console.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.console.tag_configure("cmd", foreground="#1864ab")
        self.console.tag_configure("err", foreground="#c92a2a")
        self.console.tag_configure("info", foreground="#666")
        self.cmd_entry = ttk.Entry(con, font="TkFixedFont")
        self.cmd_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.cmd_entry.bind("<Return>", lambda e: self.send_console())
        self.cmd_entry.bind("<Up>", lambda e: self._history(-1))
        self.cmd_entry.bind("<Down>", lambda e: self._history(+1))
        self.send_btn = ttk.Button(con, text="Send", command=self.send_console)
        self.send_btn.grid(row=1, column=1, padx=(4, 0), pady=(4, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(r, textvariable=self.status_var, anchor="w", padding=(8, 0, 8, 4)).grid(
            row=4, column=0, sticky="ew")

    # --------------------------------------------------------------- helpers
    def log(self, text: str, tag: str | None = None) -> None:
        self.console.configure(state="normal")
        self.console.insert("end", text + "\n", tag)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _refresh_ports(self) -> None:
        self.port_box["values"] = list_ports()

    def _set_connected(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        for w in [self.on_btn, self.off_btn, self.clr_btn, self.send_btn, self.cmd_entry,
                  *self.set_btns, *self.set_entry.values()]:
            w.configure(state=state)
        self.port_box.configure(state="disabled" if on else "normal")
        self.connect_btn.configure(text="Disconnect" if on else "Connect")

    def _submit(self, fn, callback=None) -> None:
        if self.worker:
            self.worker.submit(fn, callback)

    def _interval_changed(self) -> None:
        try:
            v = max(0.2, float(self.interval_var.get()))
        except ValueError:
            return
        if self.worker:
            self.worker.interval = v

    # ------------------------------------------------------------ connection
    def toggle_connect(self) -> None:
        self.disconnect() if self.worker else self.connect()

    def connect(self) -> None:
        port = self.port_var.get().strip()
        try:
            psu = open_psu(port)
        except Exception as e:
            messagebox.showerror("Connection failed", f"Could not open {port}:\n{e}")
            return
        self._interval_changed()
        try:
            interval = max(0.2, float(self.interval_var.get()))
        except ValueError:
            interval = 1.0
        self.worker = Worker(psu, self.events, interval)
        self.worker.start()
        self._set_connected(True)
        self.status_var.set(f"Connected to {port}")
        self.log(f"--- connected to {port}", "info")

        def done(idn, err):
            if err:
                self.idn_var.set("No reply!")
                self.log(f"ERROR: {err}", "err")
                messagebox.showwarning(
                    "No reply", f"{port} opened, but the PS370 did not answer *IDN?.\n\n{err}")
            else:
                self.idn_var.set(idn)
        self._submit(lambda p: p.idn(), done)
        self.refresh_settings()

    def disconnect(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
            self.worker = None
        self._set_connected(False)
        self.idn_var.set("Not connected")
        self.status_var.set("Disconnected")
        self.log("--- disconnected", "info")

    # --------------------------------------------------------------- actions
    def refresh_settings(self) -> None:
        def done(s, err):
            if err:
                self.log(f"ERROR reading settings: {err}", "err")
                return
            self.settings = s
            for key, _, _, scale, _ in self.SETTINGS:
                self.set_cur[key].set(f"{s[key] * scale:.1f}")
        self._submit(read_settings, done)

    def apply_setting(self, key: str) -> None:
        _, label, unit, scale, setter = next(s for s in self.SETTINGS if s[0] == key)
        text = self.set_entry[key].get().strip()
        try:
            value = float(text)
        except ValueError:
            self.log(f"ERROR: '{text}' is not a number for {label}", "err")
            return
        self.log(f"> {label} = {value:g} {unit}", "cmd")

        def done(_, err):
            if err:
                self.log(f"ERROR: {err}", "err")
            else:
                self.log("OK")
                self.set_entry[key].delete(0, "end")
            self.refresh_settings()
        self._submit(lambda p: getattr(p, setter)(value / scale), done)

    def hv_on(self, confirm: bool = True) -> None:
        vset = self.settings.get("vset")
        msg = (f"Turn ON high voltage?\n\nVSET = {vset:.0f} V" if vset is not None
               else "Turn ON high voltage?")
        if confirm and not messagebox.askyesno("Confirm HV ON", msg, icon="warning"):
            return
        self.log("> HV ON", "cmd")
        self._submit(lambda p: p.hv_on(), self._ok_or_err)

    def hv_off(self) -> None:
        if not self.worker:
            return
        self.log("> HV OFF", "cmd")
        self._submit(lambda p: p.hv_off(), self._ok_or_err)

    def clear_trips(self) -> None:
        self.trips = {"vtrip": False, "itrip": False}
        self.log("> Clear trips", "cmd")
        self._submit(lambda p: p.clear_trips(), self._ok_or_err)

    def _ok_or_err(self, _, err) -> None:
        self.log(f"ERROR: {err}" if err else "OK", "err" if err else None)

    # --------------------------------------------------------------- console
    def send_console(self) -> None:
        line = self.cmd_entry.get().strip()
        self.cmd_entry.delete(0, "end")
        if not line:
            return
        self.history.append(line)
        self.hist_idx = len(self.history)
        if line.lower() == "clear":
            self.console.configure(state="normal")
            self.console.delete("1.0", "end")
            self.console.configure(state="disabled")
            return
        self.log(f"> {line}", "cmd")

        def done(lines, err):
            if err:
                self.log(f"ERROR: {err}", "err")
                return
            for ln in lines:
                self.log(ln, "err" if ln.startswith("ERROR") else None)
            self.refresh_settings()  # the command may have changed a setting
        self._submit(lambda p: execute_line(p, line), done)

    def _history(self, step: int) -> str:
        if not self.history:
            return "break"
        self.hist_idx = max(0, min(len(self.history), self.hist_idx + step))
        self.cmd_entry.delete(0, "end")
        if self.hist_idx < len(self.history):
            self.cmd_entry.insert(0, self.history[self.hist_idx])
        return "break"

    # --------------------------------------------------------------- logging
    def _toggle_csv(self) -> None:
        if self.log_var.get():
            name = datetime.now().strftime("ps370_log_%Y%m%d_%H%M%S.csv")
            path = filedialog.asksaveasfilename(
                title="Log samples to…", initialfile=name, defaultextension=".csv",
                filetypes=[("CSV", "*.csv")], confirmoverwrite=False)
            if not path:
                self.log_var.set(False)
                return
            self._open_csv(path)
        else:
            self._close_csv()

    def _open_csv(self, path: str) -> None:
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.csv_fh = open(path, "a", newline="")
        self.csv_writer = csv.DictWriter(self.csv_fh, fieldnames=CSV_FIELDS)
        if new:
            self.csv_writer.writeheader()
        self.log_var.set(True)
        self.csv_label.set(os.path.basename(path))
        self.log(f"--- logging to {path}", "info")

    def _close_csv(self) -> None:
        if self.csv_fh:
            self.csv_fh.close()
            self.log("--- logging stopped", "info")
        self.csv_fh = self.csv_writer = None
        self.csv_label.set("")

    # ------------------------------------------------------------ event loop
    def _drain(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "sample":
                    self._on_sample(ev[1])
                elif kind == "result":
                    ev[1](ev[2], ev[3])
                elif kind == "error":
                    self.log(f"ERROR: {ev[1]}", "err")
                elif kind == "poll_error":
                    self.poll_failures += 1
                    self.status_var.set(f"Read failed ({self.poll_failures}x): {ev[1]}")
                    if self.poll_failures == 3:
                        self.log(f"ERROR: readings failing: {ev[1]}", "err")
        except queue.Empty:
            pass
        self._drain_id = self.root.after(50, self._drain)

    def _on_sample(self, s: dict) -> None:
        self.poll_failures = 0
        self.hv_is_on = bool(s["hv_on"])
        self.vout_var.set(f"{s['vout_V']:,.1f} V")
        self.iout_var.set(f"{s['iout_A'] * 1e6:.2f} µA")
        for key in ("vtrip", "itrip"):
            if s[key] and not self.trips[key]:
                self.trips[key] = True
                self.log(f"*** {'VOLTAGE' if key == 'vtrip' else 'CURRENT'} TRIP at "
                         f"{s['timestamp']} ***", "err")
        states = {"hvon": s["hv_on"], "stable": s["stable"], "ilim": s["ilim"],
                  "vtrip": self.trips["vtrip"], "itrip": self.trips["itrip"]}
        for key, _, color in self.LIGHTS:
            self.lights[key].configure(bg=color if states[key] else self.OFF_BG,
                                       fg="white" if states[key] else "black")
        self.v_hist.append(s["vout_V"])
        self.i_hist.append(s["iout_A"] * 1e6)
        self._draw_chart()
        if self.csv_writer:
            self.csv_writer.writerow(s)
            self.csv_fh.flush()
        self.status_var.set(f"Last reading {s['timestamp'][11:19]}")

    # ----------------------------------------------------------------- chart
    def _draw_chart(self) -> None:
        c = self.chart
        c.delete("all")
        h = c.winfo_height()
        if c.winfo_width() < 100 or h < 60:
            return
        self._draw_series(list(self.v_hist), 0, h / 2, "#c92a2a", "Vout (V)")
        self._draw_series(list(self.i_hist), h / 2, h, "#1864ab", "Iout (µA)")

    def _draw_series(self, data, y0, y1, color, title) -> None:
        c = self.chart
        left, right = 70, c.winfo_width() - 10
        top, bot = y0 + 18, y1 - 6
        c.create_text(left, y0 + 3, anchor="nw", text=title, fill=color,
                      font=("Helvetica", 11, "bold"))
        c.create_rectangle(left, top, right, bot, outline="#bbb")
        if len(data) < 2:
            return
        lo, hi = min(data), max(data)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        for val in (lo, hi, (lo + hi) / 2):
            y = bot - (val - lo) / (hi - lo) * (bot - top)
            c.create_line(left, y, right, y, fill="#eee")
            c.create_text(left - 4, y, anchor="e", text=f"{val:.4g}", fill="#555",
                          font=("Helvetica", 9))
        start = HISTORY - len(data)
        pts = []
        for k, val in enumerate(data):
            pts.append(left + (start + k) / (HISTORY - 1) * (right - left))
            pts.append(bot - (val - lo) / (hi - lo) * (bot - top))
        c.create_line(*pts, fill=color, width=2)

    # ------------------------------------------------------------------ demo
    def start_demo(self, speed: float = 1.0) -> None:
        """Play a scripted session through the normal GUI controls (simulator only!)."""
        def setting(key, text):
            def act():
                self.set_entry[key].delete(0, "end")
                self.set_entry[key].insert(0, text)
                self.apply_setting(key)
            return act

        def console(cmd):
            def act():
                self.cmd_entry.delete(0, "end")
                self.cmd_entry.insert(0, cmd)
                self.send_console()
            return act

        def note(text):
            return lambda: self.log(f"[demo] {text}", "info")

        steps = [
            (1.0, note("Configure limits: VLIM -10 kV, ILIM 200 uA, ITRP 300 uA")),
            (1.0, setting("vlim", "-10000")),
            (0.8, setting("ilim", "200")),
            (0.8, setting("itrp", "300")),
            (0.8, setting("vset", "-2000")),
            (1.5, note("A positive voltage is refused (the PS370 is negative-only)")),
            (0.8, setting("vset", "1000")),
            (1.5, note("VSET beyond VLIM is refused too")),
            (0.8, setting("vset", "-15000")),
            (1.5, note("HV ON (normally asks for confirmation)")),
            (0.8, lambda: self.hv_on(confirm=False)),
            (3.0, note("Typed console commands")),
            (0.8, console("*IDN?")),
            (1.0, console("VOUT?; IOUT?")),
            (1.0, console("*STB?")),
            (1.5, note("Ramp to -5 kV in 500 V steps")),
            *[(0.6, setting("vset", str(v))) for v in range(-2500, -5001, -500)],
            (3.0, note("Lower the current trip to 40 uA: the 50 uA load current trips it")),
            (0.8, setting("itrp", "40")),
            (3.0, note("Trip light stays on until 'Clear trips'; restore ITRP and turn HV back on")),
            (1.0, setting("itrp", "300")),
            (0.8, self.clear_trips),
            (0.8, lambda: self.hv_on(confirm=False)),
            (3.0, note("HV OFF (same as pressing Esc)")),
            (0.8, self.hv_off),
            (2.0, note("finished. Try the controls and console yourself.")),
        ]
        t = 0.0
        for delay, act in steps:
            t += delay * speed
            self._after_ids.append(self.root.after(int(t * 1000), act))

    # ----------------------------------------------------------------- close
    def on_close(self) -> None:
        if self.worker and self.hv_is_on:
            ans = messagebox.askyesnocancel(
                "High voltage is ON", "High voltage is still ON.\n\n"
                "Yes: switch HV OFF, then quit\nNo: quit and leave HV ON\nCancel: stay")
            if ans is None:
                return
            if ans:
                self.worker.submit(lambda p: p.hv_off())
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=3)
            self.worker = None
        self._close_csv()
        # Cancel pending callbacks, otherwise Tk reports them as errors after destroy().
        for aid in [self._drain_id, *self._after_ids]:
            try:
                self.root.after_cancel(aid)
            except tk.TclError:
                pass
        self.root.destroy()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="PS370 high-voltage supply GUI")
    p.add_argument("--port", default=os.environ.get("PS370_PORT"),
                   help="serial port to connect at start-up, or SIM for the simulator")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between readings")
    p.add_argument("--csv", help="start logging to this CSV file")
    p.add_argument("--demo", action="store_true",
                   help="play a scripted demo (only allowed with the simulator)")
    a = p.parse_args(argv)
    root = tk.Tk()
    app = App(root, port=a.port, interval=a.interval, csv_path=a.csv)
    if a.demo:
        # The demo turns HV on, so refuse to run it against real hardware.
        def check_and_start(tries=0):
            idn = app.idn_var.get()
            if idn.endswith(",SIM"):
                app.start_demo()
            elif tries < 50 and app.worker:
                root.after(100, check_and_start, tries + 1)
            else:
                app.log(f"[demo] refused: not connected to the simulator ({idn})", "err")
        check_and_start()
    root.mainloop()


if __name__ == "__main__":
    main()
