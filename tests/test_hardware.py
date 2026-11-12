"""Tests against a REAL PS370. Skipped unless a port is given:

    pytest tests/test_hardware.py --port /dev/ttyUSB0          # HV stays OFF
    pytest tests/test_hardware.py --port /dev/ttyUSB0 --hv     # + low-voltage HV-on test

Before running: HV output OFF, output cable safe/terminated. The tests only change
VSET (with HV off) and put every setting back at the end. The --hv test turns the
output on at --hv-volts (default -200 V) with a 50 uA trip, then switches it off.
"""

import time

import pytest

from ps370 import PS370, PS370Error

pytestmark = pytest.mark.hardware

TEST_VSET = -100.0  # value written to VSET while HV is off


@pytest.fixture(scope="module", autouse=True)
def saved_settings(hw_port):
    """Refuse to run with HV on; restore all settings afterwards."""
    with PS370(hw_port) as psu:
        try:
            psu.idn()
        except TimeoutError as e:
            pytest.fail(f"No reply from {hw_port}: {e}\nCheck cable, port name, power.")
        if psu.status()["hvon"]:
            pytest.fail("High voltage is ON. Turn it off before running hardware tests.")
        if psu.set_mode() != 0:
            pytest.fail("Supply is in rear-panel (SMOD 1) mode; set it to front/remote first.")
        saved = {"vlim": psu.voltage_limit(), "vset": psu.voltage_setpoint(),
                 "ilim": psu.current_limit(), "itrp": psu.current_trip()}
    yield saved
    with PS370(hw_port) as psu:
        psu.hv_off()
        psu.command(f"VLIM {saved['vlim']:.6g}")  # order: VLIM first, then VSET
        psu.command(f"VSET {saved['vset']:.6g}")
        psu.command(f"ILIM {saved['ilim']:.6g}")
        psu.command(f"ITRP {saved['itrp']:.6g}")
        psu.clear_status()


@pytest.fixture
def psu(hw_port):
    with PS370(hw_port) as p:
        p.clear_status()
        yield p


# ---- driver against hardware -------------------------------------------------------
def test_identity(psu):
    idn = psu.idn()
    assert idn.startswith("StanfordResearchSystems,PS370"), idn


def test_readings_are_numbers_and_hv_off(psu):
    v, i = psu.read_voltage(), psu.read_current()
    assert abs(v) < 100, f"HV is off but VOUT = {v} V"
    assert 0 <= i < 5e-6, f"HV is off but IOUT = {i} A"
    assert psu.status()["hvon"] is False


def test_settings_readback_in_range(psu):
    assert -20000 <= psu.voltage_limit() <= 0
    assert -20000 <= psu.voltage_setpoint() <= 0
    assert 0 <= psu.current_limit() <= 525e-6
    assert 0 <= psu.current_trip() <= 525e-6


def test_vset_roundtrip_with_hv_off(psu, saved_settings):
    if abs(saved_settings["vlim"]) < abs(TEST_VSET):
        pytest.skip("VLIM is smaller than the test value")
    psu.set_voltage(TEST_VSET)
    assert psu.voltage_setpoint() == pytest.approx(TEST_VSET, abs=1)


def test_instrument_rejects_bad_values(psu):
    # Sent raw to bypass the driver's own checks: the PS370 itself must refuse these.
    psu.write("VSET 500")
    with pytest.raises(PS370Error) as e:
        psu.check_error("VSET 500")
    assert e.value.code == 10
    psu.write("NOPE")
    with pytest.raises(PS370Error) as e:
        psu.check_error("NOPE")
    assert e.value.code in (110, 111, 125, 126)


def test_driver_refuses_positive_voltage(psu):
    with pytest.raises(ValueError):
        psu.set_voltage(100)


def test_hv_on_blocked_when_front_switch_off(psu):
    """Only meaningful with the front-panel HV switch OFF; skipped otherwise."""
    psu.set_voltage(0)  # if the switch is ON after all, the output only goes to 0 V
    try:
        psu.hv_on()
    except PS370Error as e:
        assert e.code == 10
    else:
        psu.hv_off()
        pytest.skip("front-panel HV switch is ON (HVON was accepted and turned off again)")


# ---- GUI against hardware (each in its own process; HV stays off) ---------------------
def test_gui_connects_and_reads(gui_scenario, hw_port):
    gui_scenario("hw_connect", hw_port)


def test_gui_setting_and_console(gui_scenario, hw_port):
    gui_scenario("hw_setting_and_console", hw_port)


def test_gui_logs_csv_from_hardware(gui_scenario, hw_port, tmp_path):
    gui_scenario("any_csv", hw_port, "--csv", str(tmp_path / "hw.csv"))


# ---- optional: real high voltage ----------------------------------------------------------
@pytest.mark.hv
def test_hv_on_low_voltage(request, psu, saved_settings):
    if not request.config.getoption("--hv"):
        pytest.skip("HV-on test not requested (add --hv)")
    volts = request.config.getoption("--hv-volts")
    if not -1000 <= volts < 0:
        pytest.fail("--hv-volts must be between -1000 and 0 for this test")
    psu.set_voltage_limit(min(-500.0, volts * 2))
    psu.set_current_trip(50e-6)
    psu.set_voltage(volts)
    try:
        psu.hv_on()
    except PS370Error:
        pytest.skip("HVON refused: turn the front-panel HV switch ON for this test")
    try:
        deadline = time.monotonic() + 5
        v = psu.read_voltage()
        while abs(v - volts) > 5 and time.monotonic() < deadline:
            time.sleep(0.2)
            v = psu.read_voltage()
        assert v == pytest.approx(volts, abs=5), f"VOUT {v} V, expected {volts} V"
        st = psu.status()
        assert st["hvon"] and not st["itrip"]
    finally:
        psu.hv_off()
    deadline = time.monotonic() + 10  # manual: <6 s discharge
    while abs(psu.read_voltage()) > 20 and time.monotonic() < deadline:
        time.sleep(0.3)
    assert abs(psu.read_voltage()) <= 20, "output did not discharge after HVOF"
