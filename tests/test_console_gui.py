"""Tests for the command console and the GUI, using the simulator."""

import pytest

from console import execute_line, run_shell
from ps370 import PS370
from sim.ps370_sim import FakeSerial, SimPS370


@pytest.fixture
def psu():
    return PS370(ser=FakeSerial(SimPS370(noise=False)))


# ---- console ----------------------------------------------------------------------
def test_console_query_and_set(psu):
    assert execute_line(psu, "*IDN?")[0].startswith("StanfordResearchSystems,PS370")
    assert execute_line(psu, "VSET -500") == ["OK"]
    assert execute_line(psu, "vset?") == ["-500"]


def test_console_multiple_commands(psu):
    assert execute_line(psu, "VLIM -3000; VSET -1000; VSET?; VLIM?") == \
        ["OK", "OK", "-1000", "-3.0000E3"]


def test_console_reports_errors_without_raising(psu):
    out = execute_line(psu, "VSET 500")
    assert out[0].startswith("ERROR 10")
    out = execute_line(psu, "BOGUS")
    assert out[0].startswith("ERROR 111")


def test_console_local_commands(psu):
    assert any("HVON" in ln for ln in execute_line(psu, "help"))
    assert execute_line(psu, "status")[0].startswith("STB=")
    assert execute_line(psu, "   ") == []


def test_shell_session(psu):
    inputs = iter(["VSET -200", "VSET?", "quit"])
    out = []
    run_shell(psu, input_fn=lambda prompt: next(inputs), out=out.append)
    assert out[0].startswith("Connected: StanfordResearchSystems")
    assert "OK" in out and "-200" in out


# ---- GUI tests (simulator; each runs in its own process, see gui_scenarios.py) -----
def test_gui_end_to_end(gui_scenario):
    gui_scenario("sim_end_to_end")


def test_gui_demo_runs(gui_scenario):
    gui_scenario("sim_demo")


def test_gui_logs_csv(gui_scenario, tmp_path):
    gui_scenario("any_csv", "SIM", "--csv", str(tmp_path / "gui.csv"))
