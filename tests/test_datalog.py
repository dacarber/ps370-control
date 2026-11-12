"""Tests for the always-on readings/commands CSV logs (datalog.py and its hooks)."""

import csv
import threading
from datetime import datetime
from pathlib import Path

import pytest

import ps370_cli
from console import execute_line, run_shell
from datalog import COMMAND_FIELDS, READING_FIELDS, DataLogger, stamp
from monitor import monitor
from ps370 import PS370, PS370Error
from sim.ps370_sim import FakeSerial, SimPS370


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def cmd_rows(logger):
    day = datetime.now().strftime("%Y-%m-%d")
    return rows(logger.path("commands", day))


@pytest.fixture
def logger(tmp_path):
    lg = DataLogger(tmp_path / "logs", source="test")
    yield lg
    lg.close()


@pytest.fixture
def psu(logger):
    return PS370(ser=FakeSerial(SimPS370(noise=False)), logger=logger)


# ---- DataLogger itself ----------------------------------------------------------------
def test_stamp_has_timezone_and_matches_unix():
    iso, unix = stamp(1_800_000_000.1234)
    assert unix == 1_800_000_000.123
    assert datetime.fromisoformat(iso).timestamp() == pytest.approx(unix, abs=1e-3)
    assert iso[-6] in "+-"  # UTC offset present, e.g. -07:00


def test_header_written_once_and_appends_across_sessions(tmp_path):
    for _ in range(2):
        with DataLogger(tmp_path, source="a") as lg:
            lg.command("VSET -1", "OK")
    files = list(tmp_path.glob("commands_*.csv"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert lines[0] == ",".join(COMMAND_FIELDS)
    assert len(lines) == 3


def test_midnight_rollover(tmp_path):
    t = [datetime(2026, 11, 12, 23, 59, 59).timestamp()]
    with DataLogger(tmp_path, clock=lambda: t[0]) as lg:
        lg.command("before midnight", "OK")
        lg.reading({"vout_V": -1, "iout_A": 0})
        t[0] += 2
        lg.command("after midnight", "OK")
        lg.reading({"vout_V": -2, "iout_A": 0})
    assert [r["command"] for r in rows(tmp_path / "commands_2026-11-12.csv")] == ["before midnight"]
    assert [r["command"] for r in rows(tmp_path / "commands_2026-11-13.csv")] == ["after midnight"]
    assert rows(tmp_path / "readings_2026-11-13.csv")[0]["vout_V"] == "-2"
    assert list(rows(tmp_path / "readings_2026-11-12.csv")[0]) == READING_FIELDS


def test_threads_do_not_interleave(logger):
    def work(k):
        for i in range(100):
            logger.command(f"T{k} {i}", "OK")
    ts = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    r = cmd_rows(logger)
    assert len(r) == 400 and all(x["status"] == "OK" for x in r)


def test_pause_drops_rows(logger):
    logger.command("kept", "OK")
    logger.enabled = False
    logger.command("dropped", "OK")
    logger.enabled = True
    assert [r["command"] for r in cmd_rows(logger)] == ["kept"]


# ---- driver hooks ---------------------------------------------------------------------
def test_driver_logs_ok_error_refused(psu, logger):
    psu.set_voltage(-500)
    psu.write("VSET 500")            # raw, bypasses the driver check
    with pytest.raises(PS370Error):
        psu.check_error("VSET 500")
    psu.command("VLIM -1000")
    with pytest.raises(PS370Error):
        psu.command("VSET 500")      # through command(): logged as ERROR
    with pytest.raises(ValueError):
        psu.set_voltage(1000)        # driver refuses before sending
    psu.hv_on()
    r = [(x["command"], x["status"]) for x in cmd_rows(logger)]
    assert r == [("VSET -500", "OK"), ("VLIM -1000", "OK"), ("VSET 500", "ERROR"),
                 ("VSET 1000", "REFUSED"), ("HVON", "OK")]
    err = cmd_rows(logger)[2]
    assert err["response"].startswith("10 Illegal value")
    assert all(x["source"] == "test" for x in cmd_rows(logger))


def test_polling_is_not_in_commands_log(psu, logger):
    psu.read_voltage(), psu.read_current(), psu.status(), psu.voltage_setpoint()
    assert not list(Path(logger.log_dir).glob("commands_*"))


def test_console_queries_logged_with_reply_and_source(psu, logger):
    execute_line(psu, "*IDN?; VSET -200; BOGUS", source="console")
    r = cmd_rows(logger)
    assert [(x["source"], x["command"], x["status"]) for x in r] == [
        ("console", "*IDN?", "REPLY"), ("console", "VSET -200", "OK"),
        ("console", "BOGUS", "ERROR")]
    assert r[0]["response"].startswith("StanfordResearchSystems,PS370")
    psu.set_voltage(-100)
    assert cmd_rows(logger)[-1]["source"] == "test"  # tag restored after the block


def test_shell_logs(psu, logger):
    inputs = iter(["VSET -300", "VSET?", "quit"])
    run_shell(psu, input_fn=lambda p: next(inputs), out=lambda s: None)
    r = [(x["command"], x["status"]) for x in cmd_rows(logger)]
    assert r == [("shell started", "EVENT"), ("VSET -300", "OK"), ("VSET?", "REPLY")]


def test_monitor_writes_readings_log(psu, logger):
    psu.set_voltage(-1000)
    psu.hv_on()
    monitor(psu, samples=3, sleep=lambda s: None, out=lambda s: None)
    day = datetime.now().strftime("%Y-%m-%d")
    r = rows(logger.path("readings", day))
    assert len(r) == 3 and list(r[0]) == READING_FIELDS
    t = [float(x["unix_time"]) for x in r]
    assert t == sorted(t)
    # the HVON command precedes the readings on the same clock
    assert float(cmd_rows(logger)[-1]["unix_time"]) <= t[0]


def test_cli_logs_to_log_dir(tmp_path):
    sim = SimPS370(noise=False)
    d = tmp_path / "cli_logs"
    for argv in (["set-vlim", "-3000"], ["set-v", "-1500"], ["set-v", "-5000"], ["status"]):
        ps370_cli.main(["--log-dir", str(d), *argv],
                       psu_factory=lambda: PS370(ser=FakeSerial(sim)))
    day = datetime.now().strftime("%Y-%m-%d")
    c = rows(d / f"commands_{day}.csv")
    assert [(x["source"], x["command"], x["status"]) for x in c] == [
        ("cli", "VLIM -3000", "OK"), ("cli", "VSET -1500", "OK"), ("cli", "VSET -5000", "REFUSED")]
    assert len(rows(d / f"readings_{day}.csv")) == 1  # from `status`


def test_cli_no_log(tmp_path, monkeypatch):
    monkeypatch.setenv("PS370_LOG_DIR", str(tmp_path / "x"))
    ps370_cli.main(["--no-log", "set-v", "-100"],
                   psu_factory=lambda: PS370(ser=FakeSerial(SimPS370(noise=False))))
    assert not (tmp_path / "x").exists()
