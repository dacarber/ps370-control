#!/usr/bin/env bash
# Watch the GUI run a scripted session against the simulator.
# The simulator sits on a virtual serial port, so the GUI uses the same
# pyserial path it will use with /dev/ttyUSB0 on the Pi.
#   ./run_gui_demo.sh          scripted demo, then free to play
#   ./run_gui_demo.sh --nodemo just the GUI on the simulated port
set -euo pipefail
cd "$(dirname "$0")"
if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi
PORT=./sim_port

"$PYTHON" -c "import serial, tkinter" 2>/dev/null || {
  echo "Need pyserial and tkinter: $PYTHON -m pip install pyserial (and python3-tk on Linux)"; exit 1; }

"$PYTHON" sim/ps370_sim.py --link "$PORT" --load-mohm 100 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
for _ in $(seq 50); do [ -e "$PORT" ] && break; sleep 0.1; done

DEMO=--demo
[ "${1:-}" = "--nodemo" ] && DEMO=
"$PYTHON" ps370_gui.py --port "$PORT" --interval 0.25 $DEMO
