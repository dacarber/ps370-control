import os
import sys

# Let tests import ps370, monitor, ps370_cli and sim.* from this folder.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pytest_addoption(parser):
    parser.addoption("--port", default=os.environ.get("PS370_TEST_PORT"),
                     help="serial port of a real PS370; enables the hardware tests")
    parser.addoption("--hv", action="store_true", default=os.environ.get("PS370_TEST_HV") == "1",
                     help="also run the test that turns high voltage ON (low voltage)")
    parser.addoption("--hv-volts", type=float, default=-200.0,
                     help="voltage used by the HV-on test (default -200 V)")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_log_dir(tmp_path_factory, monkeypatch):
    """Tests never write into the real logs/ folder (also inherited by GUI subprocesses)."""
    d = tmp_path_factory.mktemp("logs")
    monkeypatch.setenv("PS370_LOG_DIR", str(d))
    return d
