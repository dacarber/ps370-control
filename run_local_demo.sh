#!/usr/bin/env bash
# End-to-end demo on your own computer: starts the PS370 simulator on a virtual
# serial port and drives it with the real CLI (same code that runs on the Pi).
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
PORT=./sim_port

"$PY" -c "import serial" 2>/dev/null || {
  echo "pyserial missing: run  $PY -m pip install pyserial  (or activate your venv)"; exit 1; }

"$PY" sim/ps370_sim.py --link "$PORT" --load-mohm 100 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
for _ in $(seq 50); do [ -e "$PORT" ] && break; sleep 0.1; done

cli() { echo; echo "\$ ps370_cli.py $*"; "$PY" ps370_cli.py --port "$PORT" "$@"; }

cli idn
cli status
cli set-vlim -10000
cli set-ilim 200
cli set-itrip 300
cli set-v -2000
echo; echo "# A positive voltage should be refused (PS370 is negative-only):"
cli set-v 1000 || true
echo; echo "# VSET beyond VLIM should be refused:"
cli set-v -15000 || true
cli on --yes
cli monitor --interval 0.5 --samples 4 --csv demo_log.csv
cli ramp -5000 --rate 6000 --step 1000
cli monitor --interval 0.5 --samples 3 --csv demo_log.csv
cli off
cli status
echo; echo "CSV written to $(pwd)/demo_log.csv:"; cat demo_log.csv
