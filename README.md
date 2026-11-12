# PS370 RS-232 control from a Raspberry Pi

Python driver, CLI and CSV monitor for the SRS PS370 (−100 V … −20 kV, 500 µA, negative only),
plus a simulator so you can test everything on your laptop first.

| File | What it is |
|---|---|
| `ps370.py` | Driver class `PS370` (settings, HV on/off, readback, status, error checking, ramp) |
| `ps370_gui.py` | **GUI**: live readout, status lights, settings, HV on/off, chart, CSV logging, command console |
| `ps370_cli.py` | Command-line tool (incl. `shell`, an interactive command prompt) |
| `console.py` | Raw-command execution shared by the GUI console and `shell` |
| `monitor.py` | Polling loop → console + CSV |
| `sim/ps370_sim.py` | Simulated PS370 (virtual serial port or in-process `FakeSerial`) |
| `tests/` | pytest suites: driver/CLI, console + GUI (simulator), and `test_hardware.py` (real PS370) |
| `run_pi_tests.sh` | One-command test runner for the Pi (simulator, rehearsal, real hardware) |
| `run_local_demo.sh` | Simulator + scripted CLI session, one command |
| `run_gui_demo.sh` | GUI on a simulated serial port playing a scripted session |

## 1. Test on your own computer first (macOS/Linux)

```bash
cd ps370
python3 -m venv .venv && source .venv/bin/activate
pip install pyserial pytest
./run_pi_tests.sh --rehearse   # all tests incl. GUI + hardware tests against the simulator
./run_local_demo.sh            # full CLI session over a virtual serial port
./run_gui_demo.sh              # watch the GUI play a scripted session
```

GUI with the built-in simulator (no hardware, no virtual port needed):

```bash
python3 ps370_gui.py --port SIM
```

Interactive play: in one terminal `python3 sim/ps370_sim.py --link ./sim_port -v`
(`-v` shows the raw bytes), in another `python3 ps370_cli.py --port ./sim_port status`.
Simulator options: `--hv-switch off` (HVON should fail), `--load-mohm 10` (makes trips and
current limit easy to hit), `--no-noise`.

## 2. Hardware

* PS370 rear port: **female DB-9, DCE, 9600 baud, 8N1**, no handshaking (fixed, not configurable).
* Use a USB-to-RS232 adapter with a **male DB-9** (FTDI or Prolific chipsets work out of the box on
  Raspberry Pi OS). Plug it directly into the supply, or use a **straight-through** extension cable.
  You don't need a null modem: the supply is DCE and the adapter is DTE.
* If you get no replies at all, the cable may be a null-modem type by mistake (TX/RX swapped).
  Check it, or try a null-modem adapter as a test.

## 3. Raspberry Pi setup

```bash
sudo apt update && sudo apt install -y python3-serial python3-pytest python3-tk xvfb
sudo usermod -aG dialout $USER     # serial port permission; log out and back in
```

Copy the `ps370/` folder over (e.g. `scp -r ps370 pi@<pi-host>:~/`), then:

```bash
ls /dev/ttyUSB*          # adapter should appear as /dev/ttyUSB0
dmesg | tail             # shows which driver claimed it
cd ~/ps370 && pytest -q  # same tests pass on the Pi
```

Raw sanity check before using the scripts (type `*IDN?` then Enter, Ctrl-] to quit):

```bash
python3 -m serial.tools.miniterm /dev/ttyUSB0 9600 --eol LF
```

### Optional: stable device name
`/dev/ttyUSB0` can change if you add other USB-serial devices. Get the adapter IDs with
`udevadm info -a -n /dev/ttyUSB0 | grep -E 'idVendor|idProduct|serial' | head -3`, then create
`/etc/udev/rules.d/99-ps370.rules`:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTRS{serial}=="YOURSERIAL", SYMLINK+="ps370", GROUP="dialout", MODE="0660"
```

`sudo udevadm control --reload && sudo udevadm trigger`, then use `--port /dev/ps370`
(or `export PS370_PORT=/dev/ps370`).

## 4. Testing on the Pi

Run these in order. Each step builds confidence before the next one touches the real supply.

```bash
./run_pi_tests.sh                    # 1. code works on the Pi (simulator; includes the GUI)
./run_pi_tests.sh --rehearse         # 2. full hardware suite against the simulator on a virtual port
./run_pi_tests.sh /dev/ttyUSB0       # 3. real PS370, HV stays OFF (asks before starting)
./run_pi_tests.sh /dev/ttyUSB0 --hv  # 4. real PS370, output ON at -200 V for a few seconds
```

What the real-hardware tests (step 3) check, all with HV off:
identity; readings near 0 V / 0 µA; settings in range; VSET write/readback (−100 V); that the
**instrument itself** rejects `VSET 500` and unknown commands; that the driver refuses positive
voltages. The GUI then connects to the real port and checks readings, settings, the console,
HV OFF and CSV logging. If the front-panel HV switch is OFF, it also checks that HVON is refused.
The tests stop if HV is on when they start. Every setting is restored at the end.

Step 4 (`--hv`) sets VLIM −500 V and ITRP 50 µA, turns the output on at −200 V
(`--hv-volts` to change it, limited to −1000 V), checks VOUT within ±5 V, then switches off and
checks that the output discharges. The front-panel HV switch must be ON, and the output must be
safe to energise.

**GUI tests need a display.** On the Pi's desktop or over VNC they just run. Over plain SSH,
`sudo apt install xvfb` and the script runs them in a virtual X server automatically.
Without either, the GUI tests are skipped (not failed). Each GUI test runs in its own process
with a time limit, so a stuck window fails that test instead of freezing the run.

You can also call pytest directly, e.g. `pytest tests/test_hardware.py --port=/dev/ttyUSB0 -v` (use the `=`; with a space pytest mistakes the port for a test path)
or `PS370_TEST_PORT=/dev/ttyUSB0 pytest -v`.

## 5. First real session (do this with the HV switch OFF and the output safe)

```bash
./ps370_cli.py idn                 # StanfordResearchSystems,PS370,<serial>,<fw>
./ps370_cli.py status
./ps370_cli.py set-vlim -1000
./ps370_cli.py set-ilim 50         # microamps
./ps370_cli.py set-itrip 60
./ps370_cli.py set-v -500
./ps370_cli.py on                  # should FAIL with "is the front-panel HV switch ON?"
```

When the load and cabling are safe, turn the front-panel switch on and repeat
`on`, then `monitor --interval 1 --csv hv_log.csv`. Ctrl-C stops monitoring. Add
`--off-on-exit` if HV should drop when the logger stops. `off` turns the output off.

## 6. GUI and command console

```bash
python3 ps370_gui.py                        # choose the port from the drop-down, press Connect
python3 ps370_gui.py --port /dev/ttyUSB0 --interval 1 --csv hv_log.csv
```

* **Output:** large voltage and current readouts, plus status lights: HV ON, STABLE, I-LIMIT, V-TRIP and I-TRIP.
  Trip lights stay lit until you press *Clear trips*, so a brief trip isn't missed.
* **Settings:** VSET/VLIM in volts (negative), ILIM/ITRP in µA. Type a value and press Enter or *Set*.
  The current value next to each field is read back from the supply.
* **HV ON…** asks for confirmation and shows VSET. **HV OFF** or **Esc** (from anywhere in the window) switches it off.
  If you close the window with HV on, it asks whether to switch it off first.
* **History chart** of the last 600 samples. **Log to CSV** writes the same columns as `monitor`.
* **Command console:** type any command from the manual (`*IDN?`, `VSET -500`, `VOUT?; IOUT?`, `TCLR`, …),
  plus `help`, `status` and `clear`. ↑/↓ recall earlier commands. Note that the console sends commands
  exactly as typed, so only the instrument's own checks apply there.

The same console in a terminal, e.g. over SSH with no desktop:

```bash
python3 ps370_cli.py --port /dev/ttyUSB0 shell
PS370> *IDN?
PS370> VSET -500
PS370> VOUT?; IOUT?
```

**On the Pi** the GUI needs `sudo apt install python3-tk` (already included in Raspberry Pi OS Desktop).
Run it on the Pi's own desktop, over VNC (`sudo raspi-config` → Interface → VNC), or from your Mac with
`ssh -X pi@<pi-host>` (needs XQuartz on the Mac). Only one program can use the serial port at a time,
so close the GUI before running the CLI or the logger service.

## 7. Using it from your own Python

```python
from ps370 import PS370
with PS370("/dev/ttyUSB0") as psu:
    psu.set_voltage_limit(-6000)
    psu.set_current_trip(100e-6)
    psu.set_voltage(-5000)
    psu.hv_on()
    print(psu.read_voltage(), psu.read_current(), psu.status())
```

Every set command is followed by `*ESR?` (and `LERR?` when needed), so a value the
instrument rejects raises `PS370Error` with the manual's error text.

## 8. Optional: run the logger as a service

`/etc/systemd/system/ps370-monitor.service`:

```
[Unit]
Description=PS370 HV logger
After=dev-ttyUSB0.device

[Service]
User=pi
WorkingDirectory=/home/pi/ps370
ExecStart=/usr/bin/python3 ps370_cli.py --port /dev/ttyUSB0 monitor --interval 5 --csv /home/pi/hv_log.csv
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now ps370-monitor`. Only **one** program can hold the serial port at a
time. While the service runs, stop it before using the CLI.

## Notes and caveats
* Reading `*STB?` clears the latched trip and limit bits. The monitor logs them on every sample so trips aren't missed.
  Your own polling code shouldn't call `status()` from two places.
* The manual's `TMOD` text is ambiguous (it says `i = 1` for both modes). The simulator assumes 0 = manual,
  1 = auto. Check against the front-panel RESET indicator before relying on it.
* The simulator follows the manual, not a real unit. Response formatting, and anything the manual leaves
  unspecified, is a best guess. The driver parses numbers with `float()`, so small format differences don't matter.
