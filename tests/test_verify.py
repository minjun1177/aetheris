"""Auto-verify: the right check, once, bounded, and only when it can run.

The loop is worth having only if it is quiet when there is nothing to check.
A harness that runs somebody's whole test suite because a `.md` file changed,
or that runs it four times for four edits in one reply, is worse than one that
runs nothing. So most of what is checked here is restraint:

* the check is chosen by the written file's extension, not by whichever marker
  turns up first;
* a marker above the git working tree belongs to somebody else's project;
* `npm init`'s placeholder test script is not a test suite;
* four files in one turn are one run;
* a suite that runs past the timeout turns itself off instead of costing that
  again after every edit.

Then the part it exists for: a failing check comes back as a message the model
can act on, and after three of those the harness stops asking.

The four real checks need pytest, npm, cargo or go installed to *run*, so the
running is done against a check defined here - one that always exists and can
be made to pass, fail, or hang on demand. What the real table is asked is what
it selects, which is the part that has to be right on a machine with none of
them.
"""
import inspect
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import llm_client, tools, verify      # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


# A check that needs nothing installed: the marker holds the exit code and the
# text to print, so one project can be made to pass and the next to fail.
PROBE = verify.Check(
    "probe",
    markers=("probe.txt",),
    suffixes=(".probe",),
    command=(sys.executable, "-c",
             "import sys;"
             "spec=open('probe.txt').read().split('|',1);"
             "sys.stderr.write(spec[1]);"
             "sys.exit(int(spec[0]))"),
    ok_codes=(0, 5))


def probe_project(root, code, text="", suffix=".probe"):
    write(os.path.join(root, "probe.txt"), f"{code}|{text}")
    return write(os.path.join(root, f"unit{suffix}"), "x")


class using:
    """`verify.CHECKS` replaced for one block, and the module state cleared."""

    def __init__(self, *checks):
        self.checks = checks

    def __enter__(self):
        self.saved = verify.CHECKS
        verify.CHECKS = self.checks
        verify.reset()
        return self

    def __exit__(self, *_):
        verify.CHECKS = self.saved
        verify.reset()


HOME = tempfile.mkdtemp(prefix="verify-test-")

# ---------------------------------------------------------------------------
print("--- the check is chosen by what was written, not by what is lying around ---")
both = os.path.join(HOME, "both")
write(os.path.join(both, "pyproject.toml"), "[project]\nname='x'\n")
write(os.path.join(both, "package.json"), '{"scripts": {"test": "jest"}}')

jobs = verify._jobs([os.path.join(both, "a.py")])
check("a .py file in a mixed project picks pytest",
      [j[0].name for j in jobs] == ["pytest"], str([j[0].name for j in jobs]))
jobs = verify._jobs([os.path.join(both, "a.ts")])
check("a .ts file in the same project picks npm",
      [j[0].name for j in jobs] == ["npm test"], str([j[0].name for j in jobs]))
check("a .md file picks nothing at all",
      verify._jobs([os.path.join(both, "README.md")]) == [])

bare = os.path.join(HOME, "bare")
write(os.path.join(bare, "loose.py"), "x = 1\n")
check("a .py file with no marker above it picks nothing",
      verify._jobs([os.path.join(bare, "loose.py")]) == [])

nested = os.path.join(both, "pkg", "deep")
write(os.path.join(nested, "thing.py"), "x = 1\n")
jobs = verify._jobs([os.path.join(nested, "thing.py")])
check("a marker is found from a file well below it",
      len(jobs) == 1 and os.path.realpath(jobs[0][1]) == os.path.realpath(both))

# ---------------------------------------------------------------------------
print("\n--- four files in one turn are one run of the suite ---")
with using(PROBE):
    verify.note_written([probe_project(os.path.join(HOME, "batch"), 0)])
    for name in ("a", "b", "c"):
        verify.note_written([write(os.path.join(HOME, "batch", f"{name}.probe"), "x")])
    reports = verify.run_pending()
    check("one report for four written files", len(reports) == 1, str(len(reports)))
    check("and it names all four", reports and len(reports[0].paths) == 4,
          str(reports[0].paths if reports else []))
    check("nothing is left pending afterwards", not verify.pending())
    check("a second run with nothing written does nothing",
          verify.run_pending() == [])

# ---------------------------------------------------------------------------
print("\n--- a failure comes back, a pass does not have to ---")
with using(PROBE):
    verify.note_written([probe_project(os.path.join(HOME, "red"), 1,
                                       "E   assert 1 == 2\n1 failed")])
    reports = verify.run_pending()
    check("a non-zero exit is a failure", len(reports) == 1 and reports[0].ok is False)
    message = verify.failure_message(reports[0])
    check("the message carries what the check printed",
          "assert 1 == 2" in message, message[:80])
    check("and names the file that was changed", "unit.probe" in message)
    check("and tells the model not to call the work done",
          "not done" in message or "finished while it is failing" in message)

with using(PROBE):
    verify.note_written([probe_project(os.path.join(HOME, "green"), 0, "3 passed")])
    reports = verify.run_pending()
    check("a zero exit is a pass", len(reports) == 1 and reports[0].ok is True)

with using(PROBE):
    verify.note_written([probe_project(os.path.join(HOME, "empty"), 5, "no tests ran")])
    reports = verify.run_pending()
    check("an ok_code is a pass, not a failure to chase",
          len(reports) == 1 and reports[0].ok is True)

# ---------------------------------------------------------------------------
print("\n--- a check that will not finish turns itself off ---")
HANG = PROBE._replace(
    name="hang",
    command=(sys.executable, "-c", "import time; time.sleep(30)"))
with using(HANG):
    saved_timeout = config.VERIFY_TIMEOUT
    config.VERIFY_TIMEOUT = 1        # floored at 5 by _run, so this is a 5s wait
    try:
        verify.note_written([probe_project(os.path.join(HOME, "slow"), 0)])
        reports = verify.run_pending()
        check("a timeout is not reported to the model as a failure", reports == [])
        off = verify.turned_off()
        check("it is recorded as off instead", len(off) == 1, str(off))
        check("with a reason a person can read",
              any("without finishing" in reason for reason in off.values()),
              str(list(off.values())))
        verify.note_written([write(os.path.join(HOME, "slow", "again.probe"), "x")])
        check("and it is not run a second time", verify.run_pending() == [])
    finally:
        config.VERIFY_TIMEOUT = saved_timeout

# ---------------------------------------------------------------------------
print("\n--- nothing is invented: a check has to be one the project declares ---")
placeholder = os.path.join(HOME, "placeholder")
write(os.path.join(placeholder, "package.json"),
      '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}')
check("npm init's placeholder script is not a test suite",
      "placeholder" in verify._npm_has_test(placeholder),
      verify._npm_has_test(placeholder))
scripted = os.path.join(HOME, "scripted")
write(os.path.join(scripted, "package.json"), '{"scripts": {"test": "vitest run"}}')
check("a real one is", verify._npm_has_test(scripted) == "")
none = os.path.join(HOME, "none")
write(os.path.join(none, "package.json"), '{"name": "x"}')
check("no test script at all is not either",
      "no test script" in verify._npm_has_test(none))
check("and a check whose runner is missing is refused before it is run",
      "not installed" in verify._why_not(
          PROBE._replace(command=("definitely-not-a-real-program",)), HOME))

# ---------------------------------------------------------------------------
print("\n--- a marker above the working tree belongs to somebody else ---")
if shutil.which("git"):
    outer = os.path.join(HOME, "outer")
    inner = os.path.join(outer, "inner")
    write(os.path.join(outer, "pyproject.toml"), "[project]\nname='theirs'\n")
    write(os.path.join(inner, "mine.py"), "x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=inner, capture_output=True)
    verify.git_ops._repo_root_cache.clear()
    check("a marker outside this repository is not used",
          verify._marker_root(os.path.join(inner, "mine.py"), ("pyproject.toml",)) == "")
    write(os.path.join(inner, "pyproject.toml"), "[project]\nname='mine'\n")
    found = verify._marker_root(os.path.join(inner, "mine.py"), ("pyproject.toml",))
    check("one inside it is",
          os.path.realpath(found) == os.path.realpath(inner), found)
    verify.git_ops._repo_root_cache.clear()
else:
    check("git is available to test the working-tree ceiling", False, "git not found")

# ---------------------------------------------------------------------------
print("\n--- the switch really switches it off ---")
with using(PROBE):
    saved = config.AUTO_VERIFY
    config.AUTO_VERIFY = False
    try:
        verify.note_written([probe_project(os.path.join(HOME, "offswitch"), 1, "boom")])
        check("nothing is even remembered while it is off", not verify.pending())
        check("and nothing is run", verify.run_pending() == [])
    finally:
        config.AUTO_VERIFY = saved

# ---------------------------------------------------------------------------
print("\n--- the turn loop owns the running, dispatch only notes it ---")
# A verify failure that came back as a *tool result* would be counted by the
# refusal counter (5.9) and could end the turn on the third one. It is a
# message for that reason, and dispatch must not run the check inline: the
# files are only in the state the model meant when its whole reply has run.
source = inspect.getsource(tools.dispatch_tool)
check("dispatch_tool notes what was written", "verify.note_written" in source)
check("and does not run the check itself", "verify.run_pending" not in source)
loop = inspect.getsource(llm_client.chat_turn)
check("chat_turn is what runs it", "verify.run_pending" in loop)
check("a failure is appended as a message, not returned as a tool result",
      "verify.failure_message" in loop
      and "tool_result = verify" not in loop)
check("three failures in a row is where it gives up",
      llm_client.MAX_VERIFY_FAILURES == 3, str(llm_client.MAX_VERIFY_FAILURES))
giving_up = verify.gave_up_message(llm_client.MAX_VERIFY_FAILURES)
check("and it tells the model to explain rather than guess again",
      "Stop editing" in giving_up and "/undo" in giving_up)

shutil.rmtree(HOME, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("auto-verify checks passed")
