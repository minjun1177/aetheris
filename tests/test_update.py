"""`/update`, and the once-a-day look for a newer version.

Nothing may be installed without a yes, and the wrong way to upgrade is worse
than none: pip into a git checkout's interpreter installs a second copy beside
the one being edited. What is checked: versions compare as numbers, the check
runs at most once a day and says so once, `/update` asks before running
anything, runs the command for how this copy was installed, and reports a
failure with its output. PyPI and the installer are both stood in for.
"""
import builtins
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="update-home-")

from aetheris import __version__, config, update   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def said_by(function, *args):
    lines, original = [], builtins.print
    builtins.print = lambda *a, **k: lines.append(" ".join(str(x) for x in a))
    try:
        function(*args)
    finally:
        builtins.print = original
    return "\n".join(lines)


print("--- versions ---")
check("compared as numbers, not text", update.newer("1.10.0", "1.9.0"))
check("the same is not newer", not update.newer("1.1.0", "1.1.0"))
check("older is not newer", not update.newer("1.0.9", "1.1.0"))
check("a pre-release suffix keeps its number", update.newer("2.0.0rc1", "1.9.9"))
check("nothing found is not newer", not update.newer(""))

print("\n--- the daily check ---")
asked = []
real_latest = update.latest
update.latest = lambda timeout=update.CHECK_TIMEOUT: asked.append(1) or "99.0.0"
try:
    update._check()
    check("asks PyPI when it never has", asked == [1])
    with open(update.STATE_FILE, encoding="utf-8") as handle:
        check("and writes down what it found", json.load(handle)["latest"] == "99.0.0")
    update._found = ""
    update._check()
    check("not again the same day - the answer written down is used",
          asked == [1] and update._found == "99.0.0")
    first = said_by(update.announce)
    again = said_by(update.announce)
    check("says so once", "99.0.0" in first and "/update" in first and again == "", first)
    with open(update.STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump({"checked": time.time() - update.CHECK_EVERY - 1, "latest": "0.1"}, handle)
    update._found = ""
    update._check()
    check("a day later it asks again", asked == [1, 1] and update._found == "99.0.0")
    update.latest = lambda timeout=0: (_ for _ in ()).throw(OSError("offline"))
    with open(update.STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump({"checked": 0}, handle)
    update._found = ""
    update._check()
    check("offline is silent", update._found == "")
    config.UPDATE_CHECK = False
    started = []
    real_thread = update.threading.Thread
    update.threading.Thread = lambda **k: started.append(k) or real_thread(target=lambda: 0)
    update.start_check()
    update.threading.Thread = real_thread
    config.UPDATE_CHECK = True
    check("UPDATE_CHECK off starts nothing", started == [])
finally:
    update.latest = real_latest

print("\n--- how this copy is upgraded ---")
kind, command = update.how_installed()
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.exists(os.path.join(here, ".git")):
    check("a git checkout is pulled, never pip-installed over",
          kind == "git" and command[:2] == ["git", "-C"] and command[-1] == "pull")
else:
    check("an installed copy is upgraded by the interpreter running it",
          kind in ("pip", "pipx"), kind)

print("\n--- /update ---")
ran = []
real_run, real_latest, real_how = subprocess.run, update.latest, update.how_installed
update.latest = lambda timeout=update.CHECK_TIMEOUT: "99.0.0"
update.how_installed = lambda: ("pip", [sys.executable, "-m", "pip", "install",
                                        "--upgrade", "aetheris"])


class Finished:
    def __init__(self, code, err=""):
        self.returncode, self.stdout, self.stderr = code, "", err


try:
    subprocess.run = lambda command, **k: ran.append(command) or Finished(0)
    said = said_by(update.run, lambda question: "n")
    check("it shows the command and asks first; no is no",
          ran == [] and "pip install --upgrade aetheris" in said and "Left as it is" in said,
          said)
    said = said_by(update.run, lambda question: "y")
    check("yes runs it", len(ran) == 1 and ran[0][-1] == "aetheris")
    check("and says it takes effect on the next start",
          "Installed 99.0.0" in said and "next time" in said, said)
    subprocess.run = lambda command, **k: Finished(1, "ERROR: no permission\n")
    said = said_by(update.run, lambda question: "y")
    check("a failure is reported with what it said", "failed (exit 1)" in said
          and "no permission" in said, said)
    update.how_installed = lambda: ("git", ["git", "-C", "/src", "pull"])
    ran.clear()
    subprocess.run = lambda command, **k: ran.append(command) or Finished(0)
    said = said_by(update.run, lambda question: "y")
    check("a git checkout is told to pull, and nothing is run",
          ran == [] and "git -C /src pull" in said, said)
    update.latest = lambda timeout=update.CHECK_TIMEOUT: __version__
    said = said_by(update.run, lambda question: "y")
    check("already the newest says so", "is the newest" in said, said)
finally:
    subprocess.run, update.latest, update.how_installed = real_run, real_latest, real_how


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("update checks passed")
