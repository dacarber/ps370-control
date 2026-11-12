"""Tests for the PS370 driver, CLI and monitor against the simulator.

Run from the ps370/ folder:  pytest -v
"""

import csv

import pytest

import ps370_cli
from monitor import CSV_FIELDS, monitor
from ps370 import PS370, PS370Error
from sim.ps370_sim import FakeSerial, SimPS370, sci


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sim(clock):
    return SimPS370(clock=clock, noise=False, load_ohms=100e6)


@pytest.fixture
def psu(sim):
    return PS370(ser=FakeSerial(sim))


# ---- basic I/O ------------------------------------------------------------------
def test_idn(psu):
    assert psu.idn().startswith("StanfordResearchSystems,PS370,")


def test_sci_format_matches_manual():
    assert sci(-18998) == "-1.8998E4"
    assert sci(4.78e-4, 2) == "4.78E-4"


def test_defaults_match_manual(psu):
    assert psu.voltage_setpoint() == 0
    assert psu.voltage_limit() == -20000
    assert psu.current_limit() == pytest.approx(525e-6)
    assert psu.status()["hvon"] is False


def test_set_and_query_roundtrip(psu):
    psu.set_voltage_limit(-10000)
    psu.set_voltage(-5000)
    psu.set_current_limit(100e-6)
    psu.set_current_trip(200e-6)
    assert psu.voltage_limit() == -10000
    assert psu.voltage_setpoint() == -5000
    assert psu.current_limit() == pytest.approx(100e-6)
    assert psu.current_trip() == pytest.approx(200e-6)


# ---- validation (works for either "reject" or "auto-negate" policy) -------------
def test_positive_voltage_never_reaches_device_as_positive(psu, sim):
    try:
        psu.set_voltage(1000)
    except ValueError:
        pass
    assert sim.vset <= 0


def test_beyond_full_scale_rejected(psu, sim):
    with pytest.raises(ValueError):
        psu.set_voltage_limit(-25000)
    assert sim.vlim == -20000


def test_vset_beyond_vlim_rejected(psu, sim):
    psu.set_voltage_limit(-3000)
    with pytest.raises(ValueError):
        psu.set_voltage(-5000)
    assert sim.vset == 0


def test_device_side_errors_are_reported(psu):
    # Bypass the driver's validation to check that instrument errors surface.
    psu.write("VSET 500")
    with pytest.raises(PS370Error) as e:
        psu.check_error("VSET 500")
    assert e.value.code == 10
    psu.write("FOO 1")
    with pytest.raises(PS370Error) as e:
        psu.check_error("FOO 1")
    assert e.value.code == 111


def test_current_out_of_range_rejected(psu):
    with pytest.raises(ValueError):
        psu.set_current_limit(1e-3)


# ---- output behaviour -----------------------------------------------------------
def test_output_ramps_and_current_follows_load(psu, clock):
    psu.set_voltage(-5000)
    psu.hv_on()
    clock.advance(1.0)
    assert psu.read_voltage() == pytest.approx(-5000, abs=1)
    assert psu.read_current() == pytest.approx(50e-6, rel=1e-3)  # 5 kV / 100 MOhm
    st = psu.status()
    assert st["hvon"] and st["stable"]
    psu.hv_off()
    clock.advance(1.0)
    assert psu.read_voltage() == 0


def test_hv_on_fails_with_front_switch_off(sim, psu):
    sim.hv_switch = False
    with pytest.raises(PS370Error, match="HV switch"):
        psu.hv_on()


def test_current_trip_latches_and_clears_on_read(psu, clock):
    psu.set_current_trip(20e-6)
    psu.set_voltage(-5000)       # 50 uA into 100 MOhm > 20 uA trip
    psu.hv_on()
    clock.advance(1.0)
    st = psu.status()
    assert st["itrip"] and not st["hvon"]
    assert psu.status()["itrip"] is False   # latched bit cleared by first read


def test_current_limit_clamps_voltage(psu, clock):
    psu.set_current_limit(20e-6)
    psu.set_voltage(-5000)
    psu.hv_on()
    clock.advance(1.0)
    assert psu.read_voltage() == pytest.approx(-2000, abs=1)  # 20 uA * 100 MOhm
    assert psu.status()["ilim"]


def test_ramp_steps(psu):
    seen, sleeps = [], []
    psu.ramp_to(-1000, rate=500, step=250, sleep=sleeps.append, on_step=seen.append)
    assert seen == [-250, -500, -750, -1000]
    assert sleeps == [0.5, 0.5, 0.5]
    assert psu.voltage_setpoint() == -1000


# ---- monitor & CLI ----------------------------------------------------------------
def test_monitor_writes_csv(psu, tmp_path, clock):
    psu.set_voltage(-1000)
    psu.hv_on()
    clock.advance(1.0)
    path = tmp_path / "log.csv"
    lines = []
    n = monitor(psu, interval=0.01, csv_path=str(path), samples=3,
                sleep=lambda s: None, out=lines.append)
    assert n == 3
    rows = list(csv.DictReader(path.open()))
    assert list(rows[0].keys()) == CSV_FIELDS and "unix_time" in CSV_FIELDS
    assert len(rows) == 3
    assert float(rows[0]["vout_V"]) == pytest.approx(-1000, abs=1)
    assert rows[0]["hv_on"] == "1"
    assert "HV ON" in lines[0]


def test_monitor_off_on_exit(psu, sim):
    psu.set_voltage(-1000)
    psu.hv_on()
    monitor(psu, samples=1, off_on_exit=True, sleep=lambda s: None, out=lambda s: None)
    assert sim.hv_on is False


def test_cli_status_and_errors(psu, capsys):
    rc = ps370_cli.main(["set-v", "-1500"], psu_factory=lambda: psu)
    assert rc == 0
    rc = ps370_cli.main(["status"], psu_factory=lambda: psu)
    out = capsys.readouterr().out
    assert rc == 0 and "VSET         : -1500.0 V" in out
    rc = ps370_cli.main(["set-ilim", "9999"], psu_factory=lambda: psu)
    assert rc == 1
    assert "ILIM must be" in capsys.readouterr().err
