"""Shared fixtures and options for the test suite."""

import os
import subprocess
import sys

import pytest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def hw_port(request):
    port = request.config.getoption("--port")
    if not port:
        pytest.skip("no hardware port given (use --port /dev/ttyUSB0)")
    return port


def run_gui_scenario(name, port="SIM", *extra, timeout=90):
    """Run tests/gui_scenarios.py <name> in a fresh process (one Tk window per process)."""
    cmd = [sys.executable, "-m", "tests.gui_scenarios", name, "--port", port, *extra,
           "--timeout", str(timeout - 10)]
    try:
        r = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"GUI scenario {name} hung for {timeout} s")
    if r.returncode == 77:
        pytest.skip("no display: run on the Pi desktop/VNC, or prefix the command with xvfb-run")
    assert r.returncode == 0, f"GUI scenario {name} failed:\n{r.stdout}\n{r.stderr}"


@pytest.fixture
def gui_scenario():
    return run_gui_scenario
