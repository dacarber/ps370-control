"""Raw command console shared by the GUI and `ps370_cli.py shell`.

Type instrument commands exactly as in the manual (chapter 3), e.g.
    *IDN?        VSET -500        VOUT?; IOUT?        HVOF
Queries (anything with '?') print the reply; set commands print OK or the
instrument's error. Several commands may be separated by ';'.
"""

from __future__ import annotations

from ps370 import PS370, PS370Error

HELP = """\
Commands are sent to the PS370 as typed (case-insensitive). Separate several with ';'.
  Output   : HVON  HVOF  VOUT?  IOUT?
  Settings : VSET x  VLIM x  ILIM x  ITRP x  (V and A; voltages negative)  e.g. VSET -500, ILIM 100E-6
             VSET?  VLIM?  ILIM?  ITRP?  TCLR  TMOD i  SMOD i  *SAV i  *RCL i
  Status   : *STB?  *ESR?  LERR?  *CLS  *IDN?  *RST
Local commands: help, status (decoded *STB?), clear (clear this screen, GUI only)"""


def execute_line(psu: PS370, line: str, source: str | None = None) -> list[str]:
    """Run one console line (logged with ``source``, e.g. "console"); see _execute."""
    if source:
        with psu.log_as(source):
            return _execute(psu, line)
    return _execute(psu, line)


def _execute(psu: PS370, line: str) -> list[str]:
    """Run one console line; return the lines of output to show.

    Instrument errors are returned as 'ERROR ...' lines rather than raised, so
    a typo in the console never kills the GUI or the shell.
    """
    line = line.strip()
    if not line:
        return []
    low = line.lower()
    if low in ("help", "?"):
        return HELP.splitlines()
    if low == "status":
        st = psu.status()
        if psu.logger:
            psu._log("status (*STB?)", "REPLY", str(st["raw"]))
        on = [k for k in ("hvon", "stable", "ilim", "vtrip", "itrip") if st[k]]
        return [f"STB={st['raw']}  set: {', '.join(on) or 'none'}"]

    out: list[str] = []
    for part in (p.strip() for p in line.split(";")):
        if not part:
            continue
        try:
            if "?" in part:
                out.append(psu.query(part, log=True))  # logged with its reply
            else:
                psu.command(part)                      # logged OK / ERROR
                out.append("OK")
        except PS370Error as e:
            out.append(f"ERROR {e.code}: {e}")
        except TimeoutError as e:
            out.append(f"ERROR: {e}")
    return out


def run_shell(psu: PS370, input_fn=input, out=print) -> None:
    """Interactive prompt for the CLI (`ps370_cli.py shell`)."""
    try:
        import readline  # noqa: F401  (gives arrow-key history where available)
    except ImportError:
        pass
    idn = psu.idn()
    out(f"Connected: {idn}")
    if psu.logger:
        psu.logger.event("shell started", detail=idn)
        out(f"Logging commands to {psu.logger.log_dir}")
    out("Type 'help' for commands, 'quit' or Ctrl-D to exit.")
    while True:
        try:
            line = input_fn("PS370> ")
        except (EOFError, KeyboardInterrupt):
            out("")
            break
        if line.strip().lower() in ("quit", "exit", "q"):
            break
        if line.strip().lower() == "clear":
            continue
        for r in execute_line(psu, line):
            out(r)
