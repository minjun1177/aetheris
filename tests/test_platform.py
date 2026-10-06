#!/usr/bin/env python3
"""Check how well this machine can tell "waiting for input" from "busy".

`run_cmd` keeps a command alive and hands its prompt to the model when it stops
printing. Deciding that it really has stopped is the hard part, and what is
available to decide it with differs by platform:

    output ends without a newline   everywhere   it is shaped like a prompt
    /proc says read() on fd 0       Linux        it really is waiting on stdin
    the process tree burns no CPU   Windows/mac  it is idle, but so is a sleep
    silence for CMD_WAIT_TIMEOUT    everywhere   nothing better was available

It also checks that non-ASCII text survives the trip in both directions, which
is where Windows differs most: a Python older than 3.15 prints to a pipe in the
console code page, not UTF-8.

Run this after changing anything in `shell_session.py`, and on any machine the
harness is new to - especially Windows, where `/proc` is unavailable and the
weaker CPU signal has to carry it:

    python tests/test_platform.py

On Linux both paths are exercised: the second run switches `/proc` off, which
leaves exactly the code Windows and macOS run. Exits non-zero if anything fails.
"""

import atexit
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aetheris import config
config.AUTO_ALLOW = True            # a test run cannot answer approval prompts
config.SAVE_CHAT_HISTORY = False
config.MCP_ENABLED = False

from aetheris import shell_session
from aetheris import tools


failures = []


def check(label, ok, detail=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {detail}' if detail else ''}")


# --- the programs we run ----------------------------------------------------
# Written to files rather than passed with -c: quoting a multi-line program
# through cmd.exe and through sh are not the same problem. ASCII only, so a
# console code page cannot turn a passing test into a failing one.

PROGRAMS = {
    "inline_prompt":  "name = input('name: ')\nprint('hello', name)\n",
    "newline_prompt": "print('what is your name?')\nname = input()\nprint('hello', name)\n",
    "sleepy":         "import time\ntime.sleep(3)\nprint('computed')\n",
    "busy":           ("import time\nt = time.time()\n"
                       "while time.time() - t < 3:\n    pass\nprint('crunched')\n"),
    "chatty":         ("import sys, time\nwhile True:\n"
                       "    print('spam')\n    sys.stdout.flush()\n    time.sleep(0.05)\n"),
    "spawner":        ("import subprocess, sys, time\n"
                       "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
                       "time.sleep(300)\n"),
    # Non-ASCII, in whatever a program on this platform naturally emits: UTF-8
    # on Linux, the console code page on Windows before Python 3.15.
    "korean_out":     "print('\uc548\ub155\ud558\uc138\uc694 \ubc18\uac11\uc2b5\ub2c8\ub2e4')\n",
    "korean_prompt":  ("name = input('\uc774\ub984\uc744 \uc785\ub825\ud558\uc138\uc694: ')\n"
                       "print('\uc548\ub155\ud558\uc138\uc694,', name, '\ub2d8')\n"),
    # Raw cp949 bytes on any platform, to force the fallback path itself.
    "cp949_raw":      ("import sys\n"
                       "sys.stdout.buffer.write('\ud55c\uae00 \ud14c\uc2a4\ud2b8'.encode('cp949'))\n"
                       "sys.stdout.buffer.flush()\n"),
}

WORKDIR = tempfile.mkdtemp(prefix="platform-test-")
atexit.register(shutil.rmtree, WORKDIR, True)   # also on a failure or a Ctrl+C
atexit.register(lambda: shell_session.shutdown())

PATHS = {}
for name, source in PROGRAMS.items():
    path = os.path.join(WORKDIR, f"{name}.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)
    PATHS[name] = path


def command(name):
    return f'"{sys.executable}" "{PATHS[name]}"'


def run(name, stdin_text=""):
    start = time.time()
    result = tools.safe_run_cmd(command(name), stdin_text)
    return result, time.time() - start


def stop_everything():
    shell_session.shutdown()


# --- what this machine can do -----------------------------------------------

print(f"platform          {sys.platform}  (os.name={os.name!r})")
print(f"/proc usable      {shell_session.PROC_READABLE}")
try:
    import psutil
    print(f"psutil            {psutil.__version__}")
except ImportError:
    print("psutil            MISSING - the CPU signal will not work")
print(f"CMD_IDLE_TIMEOUT  {config.CMD_IDLE_TIMEOUT}s   "
      f"CMD_IDLE_GRACE {config.CMD_IDLE_GRACE}s   "
      f"CMD_WAIT_TIMEOUT {config.CMD_WAIT_TIMEOUT}s")


def scenarios(using_proc):
    label = "/proc (Linux)" if using_proc else "CPU + prompt shape (Windows, macOS)"
    print(f"\n=== deciding with: {label} ===")

    result, secs = run("inline_prompt")
    check("a prompt with no trailing newline is caught at once",
          "[Waiting]" in result and secs < 2.5, f"{secs:.1f}s")
    if "[Waiting]" in result:
        answer = tools.handle_send_input("", "jun")
        check("...and answering it reaches the program", "hello jun" in answer,
              repr(answer[:50]))
    stop_everything()

    result, secs = run("newline_prompt")
    allowed = 2.5 if using_proc else config.CMD_IDLE_GRACE + 2.0
    check("a prompt ending in a newline is caught", "[Waiting]" in result, repr(result[:45]))
    check("...within what that signal allows", secs < allowed, f"{secs:.1f}s < {allowed:.1f}s")
    if "[Waiting]" in result:
        answer = tools.handle_send_input("", "jun")
        check("...and answering it reaches the program", "hello jun" in answer,
              repr(answer[:50]))
    stop_everything()

    result, secs = run("sleepy")
    if using_proc:
        check("a 3s silent sleep is waited out, not called a prompt",
              result.strip() == "computed", repr(result[:60]))
    else:
        # Sleeping and waiting for input are genuinely indistinguishable here.
        # What must hold is that it is not called instantly, and that listening
        # again recovers the output when it turns out not to be a prompt.
        check("a 3s silent sleep is not called a prompt instantly",
              secs >= config.CMD_IDLE_GRACE, f"{secs:.1f}s >= {config.CMD_IDLE_GRACE}s")
        if "[Waiting]" in result:
            more = tools.handle_send_input("", "")
            check("...and listening again picks the output up", "computed" in more,
                  repr(more[:60]))
        else:
            check("...and listening again picks the output up",
                  result.strip() == "computed", repr(result[:60]))
    stop_everything()

    result, secs = run("busy")
    check("a command burning CPU is never called a prompt",
          result.strip() == "crunched", f"{secs:.1f}s {result[:40]!r}")
    stop_everything()


modes = [True, False] if shell_session.PROC_READABLE else [False]
original = shell_session.PROC_READABLE
for using_proc in modes:
    shell_session.PROC_READABLE = using_proc
    scenarios(using_proc)
shell_session.PROC_READABLE = original

if len(modes) == 1:
    print("\n  (no /proc here, so only the fallback path exists to test - which is\n"
          "   the point of running this on Windows)")


# --- the signals on their own -----------------------------------------------

print("\n=== the signals themselves ===")

session = shell_session.start(command("inline_prompt"))
time.sleep(1.0)
check("cpu_seconds() can read the process tree", session.cpu_seconds() is not None,
      str(session.cpu_seconds()))
if shell_session.PROC_READABLE:
    check("/proc sees a blocked read on fd 0", session.blocked_on_stdin())
session.close()

session = shell_session.start(command("busy"))
time.sleep(0.5)
before = session.cpu_seconds()
time.sleep(0.7)
after = session.cpu_seconds()
moved = (after - before) if (before is not None and after is not None) else 0
check("cpu_seconds() rises while the program computes", moved > 0.1, f"+{moved:.2f}s")
if shell_session.PROC_READABLE:
    check("/proc does not mistake a busy loop for a prompt", not session.blocked_on_stdin())
session.close()


# --- text that is not ASCII -------------------------------------------------

print("\n=== non-ASCII output and input ===")

HELLO = "\uc548\ub155\ud558\uc138\uc694 \ubc18\uac11\uc2b5\ub2c8\ub2e4"      # "hello, nice to meet you"
NAME = "\ubbfc\uc900"                                    # a Korean name
GREETS = "\uc548\ub155\ud558\uc138\uc694,"

result, _ = run("korean_out")
check("a program's non-ASCII output survives", result.strip() == HELLO, repr(result[:40]))

result, _ = run("korean_prompt")
check("a non-ASCII prompt survives", "\uc774\ub984\uc744" in result, repr(result[:40]))
if "[Waiting]" in result:
    session = shell_session.active()[0] if shell_session.active() else None
    answer = tools.handle_send_input("", NAME)
    check("non-ASCII input reaches the program and comes back",
          GREETS in answer and NAME in answer, repr(answer[:50]))
    if session is not None:
        print(f"         (the child was read and written as {session.encoding!r})")
stop_everything()

# The Windows failure this guards against, reproduced on any platform: a child
# writing code-page bytes that are not valid UTF-8.
saved_fallbacks = shell_session._fallback_encodings
shell_session._fallback_encodings = lambda: ["cp949"]
try:
    session = shell_session.start(command("cp949_raw"))
    text, _ended, _timed = session.read_until_idle(config.CMD_IDLE_TIMEOUT,
                                                   time.time() + 10)
    check("bytes that are not UTF-8 are decoded with the code page instead",
          "\ud55c\uae00 \ud14c\uc2a4\ud2b8" in text, repr(text[:40]))
    check("...and the choice carries over to what we send back",
          session.encoding == "cp949", repr(session.encoding))
    session.close()
finally:
    shell_session._fallback_encodings = saved_fallbacks

print(f"         (fallbacks available here: {shell_session._fallback_encodings() or 'none - this locale is UTF-8'})")


# --- stopping things --------------------------------------------------------

print("\n=== stopping a command ===")

saved = config.CMD_TIMEOUT
config.CMD_TIMEOUT = 3

start = time.time()
result, _ = run("chatty")
check("a command that never stops printing is killed", time.time() - start < 12,
      f"{time.time() - start:.1f}s")
check("...and the reason is reported", "without stopping" in result, repr(result[:60]))

start = time.time()
run("spawner")
check("a command whose children outlive it is killed too", time.time() - start < 12,
      f"{time.time() - start:.1f}s")
check("no session is left behind", shell_session.active() == [],
      str([s.id for s in shell_session.active()]))

config.CMD_TIMEOUT = saved

session = shell_session.start(command("sleepy"))
pid = session.process.pid
session.close()
time.sleep(0.3)
still_running = False
try:
    import psutil
    still_running = psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
except Exception:
    pass
check("close() really kills the process", not still_running, f"pid {pid}")


# --- ordinary commands are unaffected ---------------------------------------

print("\n=== ordinary commands ===")
check("a command that just finishes still works",
      tools.safe_run_cmd(f'"{sys.executable}" -c "print(42)"').strip() == "42")
check("a non-zero exit is reported",
      "exit code 3" in tools.safe_run_cmd(f'"{sys.executable}" -c "import sys; sys.exit(3)"'))
check("input sent up front is used",
      "hello jun" in run("inline_prompt", "jun\n")[0], "")
stop_everything()


print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("all platform checks passed")
