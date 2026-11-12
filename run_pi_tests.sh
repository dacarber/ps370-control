#!/usr/bin/env bash
# Test runner for the Raspberry Pi (works on the Mac too).
#
#   ./run_pi_tests.sh                     simulator tests: driver, CLI, console, GUI
#   ./run_pi_tests.sh --rehearse          + hardware tests against the simulator on a
#                                           virtual serial port (dress rehearsal)
#   ./run_pi_tests.sh /dev/ttyUSB0        + tests against the REAL PS370, HV stays OFF
#   ./run_pi_tests.sh /dev/ttyUSB0 --hv   + turns HV ON at -200 V for a few seconds
#
# GUI tests need a display. On the Pi desktop or VNC that's automatic. Over SSH this
# script uses xvfb-run (sudo apt install xvfb) or, failing that, skips the GUI tests.
set -uo pipefail
cd "$(dirname "$0")"
if [ -z "${PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi

PORT=""; HV=""; REHEARSE=""
for a in "$@"; do
  case "$a" in
    --hv) HV="--hv" ;;
    --rehearse) REHEARSE=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) PORT="$a" ;;
  esac
done

for mod in serial pytest tkinter; do
  "$PYTHON" -c "import $mod" 2>/dev/null || {
    echo "Missing Python module '$mod'. On the Pi: sudo apt install python3-serial python3-pytest python3-tk"
    exit 1; }
done

# Display for the GUI tests
WRAP=()
if [ "$(uname)" = "Linux" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  if command -v xvfb-run >/dev/null; then
    WRAP=(xvfb-run -a)
    echo "No display: running GUI tests in a virtual X server (xvfb-run)."
  else
    echo "No display and no xvfb-run: GUI tests will be SKIPPED (sudo apt install xvfb)."
  fi
fi

SIM_PID=""
cleanup() { [ -n "$SIM_PID" ] && kill "$SIM_PID" 2>/dev/null; }
trap cleanup EXIT

if [ -n "$REHEARSE" ]; then
  "$PYTHON" sim/ps370_sim.py --link ./sim_port >/dev/null 2>&1 &
  SIM_PID=$!
  for _ in $(seq 50); do [ -e ./sim_port ] && break; sleep 0.1; done
  PORT=./sim_port
  HV="--hv"
  echo "Rehearsal: hardware tests run against the simulator on $PORT"
fi

if [ -n "$PORT" ] && [ -z "$REHEARSE" ]; then
  echo
  echo "About to test the REAL supply on $PORT."
  echo "  * HV output must be OFF; settings are restored afterwards."
  [ -n "$HV" ] && echo "  * --hv: the output WILL be switched ON at -200 V (50 uA trip). Make the output safe."
  read -r -p "Continue? [y/N] " ans
  [[ "$ans" =~ ^[Yy] ]] || { echo "Aborted."; exit 1; }
fi

ARGS=(-v -rs)
[ -n "$PORT" ] && ARGS+=(--port "$PORT")
[ -n "$HV" ] && ARGS+=("$HV")

"${WRAP[@]}" "$PYTHON" -m pytest "${ARGS[@]}"
rc=$?
echo
if [ $rc -eq 0 ]; then echo "ALL TESTS PASSED"; else echo "SOME TESTS FAILED (exit $rc). See the output above."; fi
exit $rc
