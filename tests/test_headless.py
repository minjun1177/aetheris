"""`aetheris -p`: one prompt in, one answer out, nothing drawn on stdout.

The point of a headless mode is that something other than a person reads the
output - a pipe, a CI job, a benchmark that runs the same request fifty times
and counts. So what is checked is the contract that reader depends on: stdout
is the answer alone or one JSON object, the JSON says which tools ran and how
each ended, the prompt can arrive on stdin, and a failure is an exit status and
an `error` field rather than a traceback in the middle of the output.
"""
import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp(prefix="headless-home-")
os.environ["AETHERIS_HOME"] = HOME
# `-p` reads a piped stdin to its end, as it should - so the in-process runs
# below would read whatever this script was started with. CI closes it; a
# runner that leaves a pipe open made the suite wait for ever. Nothing is piped
# here unless a check says so.
sys.stdin = io.StringIO("")

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.GIT_AUTO_COMMIT = False
config.AUTO_VERIFY = False
config.CHANNEL_ENABLED = False

from aetheris import app, providers, tools   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


print("--- the command line ---")
args = app._parse_args(["-p", "hello"])
check("-p takes the prompt", args.prompt == "hello" and not args.json_out)
args = app._parse_args(["-p"])
check("-p alone means the prompt comes from stdin", args.prompt == "")
args = app._parse_args(["--print", "x", "--json", "--auto-allow"])
check("--json and --auto-allow go with it", args.json_out and args.auto_allow)
check("and no -p is an interactive session", app._parse_args([]).prompt is None)
with contextlib.redirect_stderr(io.StringIO()):
    try:
        app._parse_args(["--json"])
        refused = False
    except SystemExit as stop:
        refused = stop.code == 2
check("--json without -p is a usage error, not ignored", refused)

print("\n--- where the prompt comes from ---")
piped = tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8")
piped.write("line one\nline two\n")
piped.flush()
piped.seek(0)
check("a piped file is appended to the prompt",
      app._headless_prompt("explain this:", piped) == "explain this:\n\nline one\nline two",
      repr(app._headless_prompt("explain this:", open(piped.name, encoding="utf-8"))))
check("and is the whole prompt when -p has none",
      app._headless_prompt("", open(piped.name, encoding="utf-8")) == "line one\nline two")


class Socketish:
    """A stdin that is neither a terminal nor a pipe, and never closes."""
    read_called = False

    def fileno(self):
        raise OSError("not a real file")

    def isatty(self):
        return False

    def read(self):
        Socketish.read_called = True
        return "should not be read"


check("a stdin nobody sent anything on is not waited on when -p has a prompt",
      app._headless_prompt("just this", Socketish()) == "just this"
      and not Socketish.read_called)


print("\n--- a turn, reported ---")


class Scripted(providers.Provider):
    name = "scripted"
    label = "Scripted"
    needs_key = False

    def __init__(self, replies):
        super().__init__({"model": "scripted-1"})
        self.replies = list(replies)

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        yield {"text": self.replies.pop(0) if self.replies else "done."}
        yield {"done": True, "prompt_tokens": 100, "completion_tokens": 10,
               "total_seconds": 0.0, "eval_seconds": 0.0}


here = tempfile.mkdtemp(prefix="headless-work-")
replies = [
    '<tool_call>{"name": "list_dir", "arguments": {"dirpath": "%s"}}</tool_call>'
    % here.replace("\\", "/"),
    '<tool_call>{"name": "read_file", "arguments": {"filepath": "%s"}}</tool_call>'
    % os.path.join(here, "missing.txt").replace("\\", "/"),
    "The directory is empty.",
]
saved_active, saved_startup = providers._active, providers.apply_startup
providers.apply_startup = lambda: None
providers._active = Scripted(replies)
real_out = sys.stdout
captured = io.StringIO()
# Run from the directory being looked at: a read outside the working directory
# is asked about (test_trust.py), and under -p nothing can answer.
was_in = os.getcwd()
os.chdir(here)
try:
    sys.stdout = captured
    with contextlib.redirect_stderr(io.StringIO()):
        status = app._headless(app._parse_args(["-p", "what is in here?", "--json"]), "")
finally:
    sys.stdout = real_out
    providers._active = saved_active
    os.chdir(was_in)
try:
    report = json.loads(captured.getvalue())
except ValueError:
    report = {}
check("stdout is one JSON object and nothing else", bool(report) and status == 0,
      captured.getvalue()[:200])
check("with the answer", report.get("text") == "The directory is empty.", report.get("text"))
calls = report.get("tool_calls") or []
check("the tool calls in order", [c.get("name") for c in calls] == ["list_dir", "read_file"],
      str([c.get("name") for c in calls]))
check("each with how it ended", [c.get("outcome") for c in calls] == ["ok", "error"],
      str([c.get("outcome") for c in calls]))
check("and what it was called with", calls and calls[0].get("arguments", {}).get("dirpath"))
check("tokens summed over the turn's three requests",
      report.get("tokens") == {"prompt": 300, "completion": 30, "total": 330, "requests": 3},
      str(report.get("tokens")))
check("the time it took, the model, and no error",
      isinstance(report.get("seconds"), (int, float)) and report.get("model") == "scripted-1"
      and report.get("error") == "", str({k: report.get(k) for k in ("seconds", "model", "error")}))
check("the call log is put away afterwards", tools.call_log is None)

print("\n--- plain text, and failures ---")
providers._active = Scripted(["Just the answer."])
captured = io.StringIO()
try:
    sys.stdout = captured
    with contextlib.redirect_stderr(io.StringIO()):
        status = app._headless(app._parse_args(["-p", "hi"]), "")
finally:
    sys.stdout = real_out
    providers._active = saved_active
check("without --json stdout is the answer alone",
      captured.getvalue() == "Just the answer.\n" and status == 0, repr(captured.getvalue()))


class Thinker(Scripted):
    async def stream(self, messages, max_tokens=None, tools=None):
        yield {"thinking": "SECRET-THOUGHT"}
        async for chunk in Scripted.stream(self, messages, max_tokens, tools):
            yield chunk


for flags, label in ((["-p", "hi"], "plain"), (["-p", "hi", "--json"], "--json")):
    providers._active = Thinker(["Just the answer."])
    captured, drawn = io.StringIO(), io.StringIO()
    saved_show = config.SHOW_THINKING
    config.SHOW_THINKING = True
    try:
        sys.stdout = captured
        with contextlib.redirect_stderr(drawn):
            status = app._headless(app._parse_args(flags), "")
    finally:
        sys.stdout = real_out
        providers._active = saved_active
        config.SHOW_THINKING = saved_show
    check(f"{label}: nothing of the turn is drawn on stderr - no thinking, "
          f"no token line, no spinner",
          status == 0 and "SECRET-THOUGHT" not in drawn.getvalue()
          and "TPS" not in drawn.getvalue() and "tokens" not in drawn.getvalue(),
          repr(drawn.getvalue()[:200]))

providers._active = Thinker(["Just the answer."])
captured, drawn = io.StringIO(), io.StringIO()
saved_show = config.SHOW_THINKING
config.SHOW_THINKING = True
try:
    sys.stdout = captured
    with contextlib.redirect_stderr(drawn):
        status = app._headless(app._parse_args(["-p", "hi", "--verbose"]), "")
finally:
    sys.stdout = real_out
    providers._active = saved_active
    config.SHOW_THINKING = saved_show
check("--verbose draws it on stderr after all",
      "SECRET-THOUGHT" in drawn.getvalue(), repr(drawn.getvalue()[:200]))
check("and as plain text when stderr is not a terminal",
      "\x1b[" not in drawn.getvalue() and "\r" not in drawn.getvalue(),
      repr(drawn.getvalue()[:200]))
check("stdout is still the answer alone",
      captured.getvalue() == "Just the answer.\n", repr(captured.getvalue()))

sink = config.HeadlessOutput(io.StringIO())
with contextlib.redirect_stdout(sink):
    print("dropped")
    with config.speaking():
        print("\x1b[33m⚠ Approval Required\x1b[0m")
check("an approval still gets through, uncoloured",
      sink.target.getvalue() == "⚠ Approval Required\n", repr(sink.target.getvalue()))


class Broken(Scripted):
    def ready(self):
        return "no API key (TEST_KEY)"


providers._active = Broken([])
captured, errors = io.StringIO(), io.StringIO()
try:
    sys.stdout = captured
    with contextlib.redirect_stderr(errors):
        status = app._headless(app._parse_args(["-p", "hi", "--json"]), "")
finally:
    sys.stdout = real_out
    providers._active = saved_active
    providers.apply_startup = saved_startup
report = json.loads(captured.getvalue() or "{}")
check("a provider that is not ready exits 1", status == 1, str(status))
check("and the JSON says why", "no API key" in report.get("error", ""), report.get("error"))

environment = dict(os.environ, PYTHONPATH=ROOT, AETHERIS_ACCEPT_TERMS="1",
                   AETHERIS_HOME=HOME, PYTHONIOENCODING="utf-8")
empty = subprocess.run([sys.executable, "-m", "aetheris", "-p"], input="",
                       capture_output=True, text=True, env=environment, cwd=here, timeout=60)
check("nothing to answer is a usage error (2), said on stderr",
      empty.returncode == 2 and not empty.stdout and "nothing to answer" in empty.stderr,
      f"{empty.returncode} {empty.stderr.strip()[:120]}")

print("\n--- --output-format stream-json: one event per line, as it happens ---")
args = app._parse_args(["-p", "x", "--output-format", "json"])
check("--output-format json is --json", args.json_out)
with contextlib.redirect_stderr(io.StringIO()):
    try:
        app._parse_args(["-p", "x", "--json", "--output-format", "stream-json"])
        refused = False
    except SystemExit as stop:
        refused = stop.code == 2
check("--json with another format is a usage error", refused)

saved_startup = providers.apply_startup
providers.apply_startup = lambda: None
providers._active = Scripted([
    '<tool_call>{"name": "list_dir", "arguments": {"dirpath": "%s"}}</tool_call>'
    % here.replace("\\", "/"),
    "All done here.",
])
captured = io.StringIO()
os.chdir(here)
try:
    sys.stdout = captured
    with contextlib.redirect_stderr(io.StringIO()):
        status = app._headless(app._parse_args(
            ["-p", "look", "--output-format", "stream-json"]), "")
finally:
    sys.stdout = real_out
    providers._active = saved_active
    providers.apply_startup = saved_startup
    os.chdir(was_in)
try:
    stream = [json.loads(line) for line in captured.getvalue().splitlines()]
except ValueError:
    stream = []
kinds = [event.get("type") for event in stream]
check("every line is a JSON object", bool(stream) and status == 0, captured.getvalue()[:300])
check("start first, result last", kinds[:1] == ["start"] and kinds[-1:] == ["result"],
      str(kinds))
check("the tool call is announced before its result",
      "tool_use" in kinds and kinds.index("tool_use") < kinds.index("tool_result"), str(kinds))
used = next((e for e in stream if e.get("type") == "tool_use"), {})
done = next((e for e in stream if e.get("type") == "tool_result"), {})
check("with its name and arguments", used.get("name") == "list_dir"
      and used.get("arguments", {}).get("dirpath"), str(used))
check("and how it ended, with what it returned",
      done.get("outcome") == "ok" and isinstance(done.get("result"), str), str(done)[:200])
said = "".join(e.get("text", "") for e in stream if e.get("type") == "text")
check("the answer arrives as text events, without the tool-call tag",
      "All done here." in said and "<tool_call>" not in said, repr(said))
check("the result is the --json report", stream[-1].get("text") == "All done here."
      and stream[-1].get("error") == "" and len(stream[-1].get("tool_calls", [])) == 1,
      str(stream[-1])[:200])
from aetheris import events   # noqa: E402
check("and nothing is listening afterwards", not events.listening())


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("headless checks passed")
