"""Run the project's own checks after an edit, and hand a failure back.

A model that has just written a file says it is done. It has not run anything -
often it never occurs to it to, and a small one that is told to check its work
in the system prompt forgets by the third tool call. So the file is wrong, the
turn ends, and the person finds out by running the program themselves, copying
the traceback, and pasting it back in. That round trip is the single most
repeated thing anybody does with a harness like this.

This closes it. When a tool changes a file, the harness works out what kind of
project the file belongs to, runs that project's own check once the turn's tool
calls are done, and if it fails, puts the failure in front of the model as a
message. The model reads a concrete error and fixes it, which is the kind of
work a 4B model is actually good at - far easier than noticing unprompted that
something might be wrong.

Three things keep it from being worse than the problem:

* **It only runs what the project already declares.** No check is invented: a
  marker file has to be there (`pyproject.toml`, `package.json`, `Cargo.toml`,
  `go.mod`), the runner has to be installed, and for npm the `test` script has
  to be a real one rather than the placeholder `npm init` writes.
* **It is bounded.** `VERIFY_TIMEOUT` seconds, no stdin, output trimmed to the
  tail where the failure actually is. A suite that runs over the timeout turns
  itself off for the rest of the session rather than costing that every edit.
* **It gives up.** `llm_client` stops feeding failures back after three in a
  row and tells the model to explain itself instead, the same way it stops a
  model knocking on a refused tool.

`/undo` is what makes the whole thing safe: every retry is its own commit, so a
loop that went the wrong way is undone one step at a time.

Nothing here raises. A check that cannot run is not an error - it is a project
without that kind of check, which is most of them.
"""

import os
import re
import json
import shutil
import subprocess
import sys
import time
from collections import namedtuple

from aetheris import config
from aetheris import git_ops
from aetheris.config import S

# `suffixes` is what decides which check a written file belongs to, not the
# order of this table: a repository with both a `package.json` and a
# `pyproject.toml` should run pytest for a `.py` file and npm for a `.ts` one,
# and asking the file settles that with no precedence rule to get wrong.
#
# `ok_codes` is the exit codes that are not a failure. pytest's 5 is "no tests
# were collected", which is the normal state of a project that has a
# `pyproject.toml` and no tests yet - reporting that as a failure would put a
# model into a loop trying to fix a suite that does not exist.
Check = namedtuple("Check", "name markers suffixes command ok_codes")

CHECKS = (
    Check("pytest",
          markers=("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"),
          suffixes=(".py",),
          # This interpreter, not whatever `pytest` is first on PATH: the
          # harness is normally running inside the project's own virtualenv,
          # and that is the environment the tests are meant to be run in.
          #
          # `-l` is `--showlocals`: the variables in each failing frame, printed
          # under it. "KeyError on line 45" asks a 4B model to work backwards to
          # what was in scope; "KeyError on line 45, user_id = None, data = {}"
          # asks it to read. Working backwards is the thing it is worst at, so
          # this is most of the value of running the tests at all. pytest cuts
          # a long repr down itself - a 500-key dict of 200-character strings
          # came to 500 more characters, not 100,000 - so it fits the budget.
          command=(sys.executable, "-m", "pytest", "-x", "-q", "-l"),
          ok_codes=(0, 5)),
    Check("npm test",
          markers=("package.json",),
          suffixes=(".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
          command=("npm", "test", "--silent"),
          ok_codes=(0,)),
    Check("cargo test",
          markers=("Cargo.toml",),
          suffixes=(".rs",),
          command=("cargo", "test", "--quiet"),
          ok_codes=(0,)),
    Check("go test",
          markers=("go.mod",),
          suffixes=(".go",),
          command=("go", "test", "./..."),
          ok_codes=(0,)),
)

# What a test file is called, by convention, in the languages the checks above
# cover. `/tdd` denies writing to these for one request, so "make the test
# pass" cannot be answered by editing the test. `fnmatch`'s `*` crosses `/`,
# so one pattern covers a bare name, a relative path and an absolute one.
#
# Generous rather than exact: locking a file that only looks like a test costs
# one refusal the model can read and work around, while missing a real one
# costs the whole point of the mode. `run_cmd` is not covered and cannot be -
# what a shell command writes is not knowable from the call.
TEST_PATTERNS = ("*test_*.py", "*_test.py", "*conftest.py", "*tests/*",
                 "*/test/*", "*_test.go", "*_test.rs", "*tests.rs",
                 "*.test.js", "*.test.jsx", "*.test.ts", "*.test.tsx",
                 "*.spec.js", "*.spec.jsx", "*.spec.ts", "*.spec.tsx",
                 "*_spec.rb", "*Test.java", "*Tests.cs")


# What one run of a check came to. `ok` is True, False, or None for "it never
# really ran" - a timeout or a runner that would not start, which is a fact
# about this machine rather than about the code the model just wrote, and is
# never shown to the model. `output` is already trimmed to what it should see.
Report = namedtuple("Report", "ok name command root paths output seconds problem")

# Paths written since the last run. A set because a model that rewrites the
# same file twice in one turn should still only cost one run of the suite.
_pending: set = set()

# (check name, root) that will not be tried again this session, and why: the
# runner is not installed, the project has no real test script, the suite ran
# past the timeout. Probing costs a subprocess, and the answer does not change.
_off: dict = {}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MAX_LEVELS = 8          # how far up from a file to look for a marker


def enabled() -> bool:
    return bool(getattr(config, "AUTO_VERIFY", True))


# ---------------------------------------------------------------------------
# what changed
# ---------------------------------------------------------------------------

def note_written(paths) -> None:
    """Remember files a tool just changed, to be checked after the turn's calls.

    Called with exactly what `tools._paths_written` returns, so a write the
    user declined or one that failed never schedules a check - there is nothing
    new to run against.
    """
    if not enabled():
        return
    for path in paths or ():
        if isinstance(path, str) and path:
            _pending.add(path)


def clear() -> None:
    """Forget what is waiting. A new turn does not inherit the last one's."""
    _pending.clear()


def pending() -> bool:
    return bool(_pending)


# ---------------------------------------------------------------------------
# working out what to run
# ---------------------------------------------------------------------------

def _marker_root(path: str, markers: tuple) -> str:
    """The directory above `path` holding one of `markers`, or "".

    Walks up from the file. The search stops at the git working tree if there
    is one, because a marker outside the project is somebody else's project -
    a checkout under a home directory that happens to have a `package.json` in
    it should not have its owner's test suite run by this.
    """
    try:
        # realpath, not abspath: `repo_root` reports the working tree with its
        # symlinks already resolved, so an unresolved directory would never
        # compare equal to the ceiling and the walk would climb straight past
        # it. The same trap `git_ops._resolved` exists for.
        directory = os.path.dirname(os.path.realpath(path))
    except (OSError, ValueError):
        return ""
    ceiling = git_ops.repo_root(directory)
    for _ in range(_MAX_LEVELS):
        for marker in markers:
            if os.path.isfile(os.path.join(directory, marker)):
                return directory
        if ceiling and os.path.normcase(directory) == os.path.normcase(
                os.path.realpath(ceiling)):
            return ""
        parent = os.path.dirname(directory)
        if parent == directory:
            return ""
        directory = parent
    return ""


def _npm_has_test(root: str) -> str:
    """"" if `npm test` would run something, else why it would not.

    `npm init` writes a `test` script that prints an error and exits 1. Running
    that after every edit would report a failure the model cannot fix and did
    not cause.
    """
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as handle:
            package = json.load(handle)
    except (OSError, ValueError):
        return "package.json could not be read"
    script = (package.get("scripts") or {}).get("test")
    if not isinstance(script, str) or not script.strip():
        return "package.json has no test script"
    if "no test specified" in script:
        return "package.json still has the placeholder test script"
    return ""


def _why_not(check: Check, root: str) -> str:
    """"" if this check can run here, else the reason it cannot.

    Asked once per project - `run_pending` records the answer in `_off` and
    does not ask again. It is a fact about the machine, and probing costs a
    subprocess.
    """
    program = check.command[0]
    if program != sys.executable and not shutil.which(program):
        return f"{program} is not installed"
    if check.name == "pytest" and not _importable("pytest"):
        return "pytest is not installed"
    if check.name == "npm test":
        return _npm_has_test(root)
    return ""


def _importable(module: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _jobs(paths) -> list:
    """The distinct (check, root, key, paths) the written files call for.

    One job per (check, project), however many files went into it: a model that
    edits four files in one turn should pay for one run of the suite, not four.
    """
    found, order = {}, []
    for path in sorted(paths):
        suffix = os.path.splitext(path)[1].lower()
        for check in CHECKS:
            if suffix not in check.suffixes:
                continue
            root = _marker_root(path, check.markers)
            if not root:
                continue
            key = (check.name, os.path.normcase(root))
            if key not in found:
                found[key] = (check, root, key, [])
                order.append(key)
            found[key][3].append(path)
            break
    return [found[key] for key in order]


# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------

def _trim(text: str) -> str:
    """The tail of the output, which is where the failure is written down."""
    text = _ANSI.sub("", text).replace("\r\n", "\n").strip()
    limit = max(200, int(getattr(config, "VERIFY_OUTPUT_CHARS", 2000)))
    if len(text) <= limit:
        return text
    return "...[earlier output trimmed]\n" + text[-limit:]


def _environment() -> dict:
    """The child's environment: no colour, and nothing that waits for a human.

    `CI` is the flag every watcher and prompt in the JavaScript world already
    reads to mean "run once and exit". Without it `npm test` on a project using
    a watch-mode runner would sit there until the timeout killed it.
    """
    environment = dict(os.environ)
    environment.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0",
                        "PYTHONUNBUFFERED": "1"})
    return environment


def _run(check: Check, root: str, paths: list) -> Report:
    """Run one check. Never raises: a runner that will not start is something
    to stop trying, not something to crash a tool call over."""
    timeout = max(5, int(getattr(config, "VERIFY_TIMEOUT", 90)))
    started = time.time()

    def report(ok, output, problem=""):
        return Report(ok, check.name, _spelled(check), root, paths,
                      output, time.time() - started, problem)

    try:
        # No stdin, for the same reason `run_cmd` has none: a suite that stops
        # to ask something would otherwise hold the timeout open with nothing
        # on screen. `timeout` kills the child - a suite that forks and detaches
        # can outlive it, which is the deal every other runner offers too.
        done = subprocess.run(check.command, cwd=root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, env=_environment(),
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return report(None, "", f"it ran for more than {timeout}s without finishing")
    except (OSError, ValueError) as error:
        return report(None, "", f"{check.command[0]} would not start ({error})")

    joined = (done.stdout or "")
    if done.stdout and done.stderr:
        joined += "\n"
    joined += (done.stderr or "")
    return report(done.returncode in check.ok_codes, _trim(joined))


def _spelled(check: Check) -> str:
    """The command as a person would type it, with no absolute interpreter."""
    parts = list(check.command)
    if parts and parts[0] == sys.executable:
        parts[0] = "python"
    return " ".join(parts)


def run_pending() -> list:
    """Run the checks the files written this turn call for. Returns Reports.

    Empty when there is nothing to check, which is the common case: a project
    with no test suite, a file the checks do not cover, a turn that only read.
    """
    paths = sorted(_pending)
    _pending.clear()
    if not paths or not enabled():
        return []

    reports = []
    for check, root, key, changed in _jobs(paths):
        if key in _off:
            continue
        reason = _why_not(check, root)
        if reason:
            # Not a failure and not worth a line on screen: most projects do
            # not have this kind of check, and saying so after every edit would
            # be noise. Recorded so it is not probed again, and `/autoverify`
            # will say it if anybody asks.
            _off[key] = reason
            continue

        print(f"  {S.MUTED}⟳ auto-verify: {_spelled(check)}{S.R}"
              f"  {S.GRAY}in {os.path.basename(root) or root}{S.R}")
        report = _run(check, root, changed)

        if report.ok is None:
            # It timed out, or the runner would not start. Neither is going to
            # be different after the next edit, and paying for it every time is
            # worse than not having it at all.
            _off[key] = report.problem
            print(f"  {S.WARN}⚠ auto-verify is off for this project: "
                  f"{report.problem}{S.R}"
                  f"  {S.GRAY}/autoverify on to try again{S.R}")
            continue

        colour, mark = (S.OK, "✓") if report.ok else (S.ERR, "✗")
        print(f"  {colour}{mark} {_spelled(check)} "
              f"{'passed' if report.ok else 'failed'}{S.R}"
              f"  {S.GRAY}{report.seconds:.1f}s{S.R}")
        reports.append(report)
    return reports


def reset() -> None:
    """Try every check again - what `/autoverify on` means after one was off."""
    _off.clear()
    _pending.clear()


def turned_off() -> dict:
    """(check, root) that will not be tried again, and why. For `/autoverify`."""
    return dict(_off)


# ---------------------------------------------------------------------------
# what the model is told
# ---------------------------------------------------------------------------

def failure_message(report: Report) -> str:
    """The failure, worded so the next thing the model does is fix it."""
    changed = ", ".join(os.path.basename(p) for p in report.paths[:4])
    return (f"[System] Auto-verify ran `{report.command}` after your change"
            f"{f' to {changed}' if changed else ''}, and it failed. This is the "
            f"project's own check, not a suggestion - the work is not done "
            f"until it passes.\n\nFix what this says, then stop. It is run "
            f"again by itself after your next edit; do not run it yourself, and "
            f"do not tell the user it is finished while it is failing.\n\n"
            f"{report.output or '(the check printed nothing)'}")


def recovered_message(report: Report) -> str:
    """One line, so a model that has just fixed something knows it is fixed."""
    return (f"[System] Auto-verify: `{report.command}` passes now. "
            f"Say what you changed and stop.")


def gave_up_message(limit: int) -> str:
    return (f"[System] Auto-verify has come back with the same failure {limit} "
            f"times, so it is off for the rest of this turn. Stop editing. "
            f"Tell the user which "
            f"check is failing, what you changed, and what you think is wrong - "
            f"a fourth guess is worth less to them than an honest description. "
            f"They can take your changes back with /undo.")


if __name__ == "__main__":
    print("This file can not run directly.")
