"""`/loop`, `/schedule`, `/tasks`, and the prompt they type into.

What is checked: an interval and a time are read the way a person writes them
and refused when they are not one; a one-off runs once and a loop runs on,
moved on from when it ran rather than catching up; a task is typed into an
open prompt only when the prompt is empty, ahead of nothing a person typed;
a background command that ended is passed on the same way, first; and the
commands list, create and stop what they say.
"""
import asyncio
import contextlib
import datetime
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="tasks-home-")

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True

from aetheris import app, background, scheduler, tools   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def py(code: str) -> str:
    """A command that runs `code` - Python rather than sh, so it reads the same
    to cmd on the Windows runner. `code` must not contain a double quote."""
    return f'"{sys.executable}" -c "{code}"'


SLEEP = py("import time; time.sleep(30)")


def quietly(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


print("--- an interval is read as it is written ---")
check("5m", scheduler.split_interval("5m check the CI") == (300.0, "check the CI"))
check("two words", scheduler.split_interval("2 hours run it") == (7200.0, "run it"))
check("compound", scheduler.split_interval("1h30m go") == (5400.0, "go"))
check("seconds", scheduler.parse_interval("90s") == 90.0)
check("a bare number is not one", scheduler.split_interval("10 things") == (0.0, "10 things"))
check("nor a word", scheduler.parse_interval("soon") == 0.0)
check("nor a made-up unit", scheduler.parse_interval("5x") == 0.0)

print("\n--- so is a time ---")
now = datetime.datetime(2026, 11, 20, 12, 0).timestamp()
due, rest = scheduler.split_when("in 20m deploy it", now)
check("in 20m", due == now + 1200 and rest == "deploy it", f"{due - now} {rest!r}")
due, rest = scheduler.split_when("20m deploy it", now)
check("20m on its own", due == now + 1200 and rest == "deploy it")
due, rest = scheduler.split_when("at 14:30 go", now)
check("at 14:30 is today, later",
      datetime.datetime.fromtimestamp(due) == datetime.datetime(2026, 11, 20, 14, 30)
      and rest == "go")
due, _ = scheduler.split_when("09:00 go", now)
check("a time already past is tomorrow's",
      datetime.datetime.fromtimestamp(due) == datetime.datetime(2026, 11, 21, 9, 0))
due, rest = scheduler.split_when("2026-12-01 08:15 release", now)
check("a date and a time",
      datetime.datetime.fromtimestamp(due) == datetime.datetime(2026, 12, 1, 8, 15)
      and rest == "release")
due, why = scheduler.split_when("2026-11-19 08:15 release", now)
check("a date and a time already past are refused, not run at once",
      due == 0 and "already passed" in why, why)
due, why = scheduler.split_when("tomorrow go", now)
check("anything else is refused, with what to type instead",
      due == 0 and "in 20m" in why, why)
due, why = scheduler.split_when("at 25:00 go", now)
check("and so is an hour that does not exist", due == 0 and "25:00" in why, why)

print("\n--- what falls due, and what it leaves behind ---")
scheduler.clear()
once = scheduler.add_once(now + 60, "check the deploy")
loop = scheduler.add_loop(300, "look at the CI", now=now)
check("nothing is due before its time", scheduler.due(now + 59) is None)
first = scheduler.due(now + 61)
check("the one-off goes when it is due", first is once)
check("and only once", once.id not in [t.id for t in scheduler.tasks()])
late = now + 300 + 1000          # held up by a long turn
ran = scheduler.due(late)
check("a loop that was held up goes once", ran is loop and scheduler.due(late) is None)
check("and its next run is an interval from when it ran", loop.due == late + 300,
      f"{loop.due - late}")
check("a loop tighter than the floor is held to it",
      scheduler.add_loop(1, "x", now=now).every == scheduler.MIN_INTERVAL)
started = scheduler.add_loop(600, "now please", now=now, first_now=True)
check("/loop's first run is now", started.due == now)
check("cancel takes one by id", [t.id for t in scheduler.cancel(loop.id)] == [loop.id])
check("and all of them with 'all'", scheduler.cancel("all") and scheduler.tasks() == [])


class FakeBuffer:
    def __init__(self, text=""):
        self.text = text


class FakePromptApp:
    def __init__(self, text=""):
        self.is_running = True
        self.current_buffer = FakeBuffer(text)
        self.result = None

    def exit(self, result=None):
        self.result = result
        self.is_running = False


class FakeSession:
    def __init__(self, text=""):
        self.app = FakePromptApp(text)


print("\n--- a due task is typed into an empty prompt, and only an empty one ---")
scheduler.clear()
task = scheduler.add_once(time.time() - 1, "/compact the parser work")
busy = FakeSession("half a sentence")
fired, _ = quietly(app._maybe_task_turn, busy)
check("a half-typed line is left alone", not fired and busy.app.result is None)
check("and the task waits for it", [t.id for t in scheduler.tasks()] == [task.id])
idle = FakeSession("")
app._auto_woke = False
fired, shown = quietly(app._maybe_task_turn, idle)
check("an empty prompt is closed with the task's line",
      fired and idle.app.result == "/compact the parser work", str(idle.app.result))
check("it says which task it was", task.id in shown, shown)
check("and the line is not counted as a person arriving", app._auto_woke is True)
app._auto_woke = False
check("nothing is due after it", not quietly(app._maybe_task_turn, FakeSession(""))[0])
closed = FakeSession("")
closed.app.is_running = False
scheduler.add_once(time.time() - 1, "later")
check("a prompt that is not open is not typed into",
      not quietly(app._maybe_task_turn, closed)[0])
scheduler.clear()

print("\n--- a background command that ended goes first ---")
started_text = tools.safe_run_cmd(py("print('bu' + 'ilt'); raise SystemExit(2)"), "", "true")
check("run_cmd with background returns a job id at once", "job b" in started_text,
      started_text)
job = background.jobs()[-1]
check("the job ends on its own", job.wait(10))
scheduler.add_once(time.time() - 1, "the scheduled one")
idle = FakeSession("")
fired, shown = quietly(app._maybe_task_turn, idle)
check("the ended job is what the prompt is closed with",
      fired and idle.app.result.startswith("[Background] Job " + job.id), str(idle.app.result))
# The command is spelled `'bu' + 'ilt'` so the output, not the command line quoted
# back, is what has to carry the word: a job that ended at once used to be
# collected before its output had arrived, and reported as printing nothing.
check("with its exit code and its output",
      "exit code 2" in idle.app.result and "\nbuilt" in idle.app.result,
      idle.app.result)
check("and it is said on screen", job.id in shown and "ended" in shown, shown)
idle = FakeSession("")
quietly(app._maybe_task_turn, idle)
check("the task goes next", idle.app.result == "the scheduled one", str(idle.app.result))
check("a turn started that way is not drawn back as typed",
      idle.app.result and app._NOT_TYPED[-1] == "[Background]")
config.BACKGROUND_WAKE = False
tools.safe_run_cmd(py("pass"), "", "true")
background.jobs()[-1].wait(10)
check("with BACKGROUND_WAKE off an ended job starts nothing",
      not quietly(app._maybe_task_turn, FakeSession(""))[0])
check("and is still there for the next turn to pass on",
      len(background.take_finished()) == 1)
config.BACKGROUND_WAKE = True
scheduler.clear()

print("\n--- the commands ---")


class Chat:
    messages = [{"role": "system", "content": "sys"}]
    session_id = None
    mcp_servers = []


def run(handler, line):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        outcome = asyncio.run(handler(Chat(), line, line.lower()))
    return outcome, out.getvalue()


outcome, shown = run(app._cmd_loop, "/loop 5m check the CI and fix what fails")
loops = scheduler.tasks()
check("/loop makes a loop", len(loops) == 1 and loops[0].repeats
      and loops[0].prompt == "check the CI and fix what fails", shown)
check("which runs now, then every 5m", loops[0].due <= time.time() and loops[0].every == 300)
check("and is handled, not sent", outcome is app._HANDLED)
_, shown = run(app._cmd_loop, "/loop check it")
check("/loop without an interval says how", "Usage: /loop" in shown, shown)
_, shown = run(app._cmd_loop, "/loop 5m /loop 1m x")
check("a task cannot start another task", "cannot be what a task sends" in shown
      and len(scheduler.tasks()) == 1, shown)
_, shown = run(app._cmd_schedule, "/schedule in 20m check the deploy")
check("/schedule makes a one-off", any(not t.repeats and t.prompt == "check the deploy"
                                       for t in scheduler.tasks()), shown)
_, shown = run(app._cmd_schedule, "/schedule whenever check")
check("and refuses a time it cannot read", "not a time" in shown, shown)
long_job = tools.safe_run_cmd(SLEEP, "", "true")
job_id = background.running()[-1].id
_, shown = run(app._cmd_tasks, "/tasks")
check("/tasks lists loops, schedules and background commands",
      "l" in shown and "check the deploy" in shown and job_id in shown and "running" in shown,
      shown)
_, shown = run(app._cmd_tasks, f"/tasks stop {job_id}")
check("/tasks stop ends a background command", job_id in shown
      and not background.get(job_id).running, shown)
check("without telling the model it ended - it was stopped on purpose",
      job_id not in [j.id for j in background.take_finished()])
_, shown = run(app._cmd_tasks, "/tasks stop all")
check("/tasks stop all ends every task", scheduler.tasks() == [], shown)
_, shown = run(app._cmd_tasks, "/tasks stop nothing-here")
check("and says so when there is nothing by that name", "Nothing running" in shown, shown)

print("\n--- read_background and end_process ---")
tools.safe_run_cmd(py("import time; print('one', flush=True); time.sleep(0.3); print('two')"),
                   "", "true")
job = background.jobs()[-1]
job.wait(10)
first = tools.handle_read_background(job.id)
again = tools.handle_read_background(job.id)
check("read_background hands over what was printed", "one" in first and "two" in first, first)
check("and only what is new the next time", "Nothing new" in again, again)
check("a job read after it ended is not announced again",
      job.id not in [j.id for j in background.take_finished()])
check("an unknown job is named as unknown",
      tools.handle_read_background("b999").startswith("[Error]"))
listing = tools.handle_read_background("")
check("without an id it lists every job", job.id in listing and "Background jobs" in listing)
config.BACKGROUND_MAX_JOBS = 1
tools.safe_run_cmd(SLEEP, "", "true")
holder = background.running()[-1].id
refused = tools.safe_run_cmd(SLEEP, "", "true")
check("past BACKGROUND_MAX_JOBS a job is refused and the running ones named",
      refused.startswith("[Error] Not started") and f"{holder} (" in refused, refused)
stopped = tools.handle_end_process(background.running()[-1].id)
check("end_process stops a background job", "Stopped background job" in stopped, stopped)
config.BACKGROUND_MAX_JOBS = 4
check("a plain run_cmd still waits for its command",
      tools.safe_run_cmd(py("print('plain')"), "", "").strip() == "plain")

print("\n--- a job that ends mid-turn is passed on before the next request ---")
from aetheris import llm_client, providers   # noqa: E402


class Scripted(providers.Provider):
    name = "scripted"
    label = "Scripted"
    needs_key = False

    def __init__(self, replies):
        super().__init__({"model": "s-1"})
        self.replies = list(replies)
        self.seen = []

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        self.seen.append([dict(m) for m in messages])
        yield {"text": self.replies.pop(0) if self.replies else "done."}
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


def call(arguments):
    import json
    return f'<tool_call>\n{json.dumps({"name": "run_cmd", "arguments": arguments})}\n</tool_call>'


scripted = Scripted([call({"command": py("print('fin' + 'ished')"), "background": "true"}),
                     call({"command": py("import time; time.sleep(1)")}),
                     "All done."])
saved_active, providers._active = providers._active, scripted
config.AUTO_VERIFY = False
history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "build it"}]
try:
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(llm_client.chat_turn(history))
finally:
    providers._active = saved_active
last_request = scripted.seen[-1] if scripted.seen else []
told = [m["content"] for m in last_request if m["content"].startswith("[Background]")]
check("the third request carries the job's ending", len(told) == 1 and "\nfinished" in told[0],
      str(told))
check("and it is not handed out again", background.take_finished() == [])

print("\n--- a command that ends at once keeps what it printed, every time ---")
lost = []
for n in range(25):
    tools.safe_run_cmd(py(f"print('mark{n}')"), "", "true")
    job = background.jobs()[-1]
    job.wait(10)
    if f"mark{n}" not in job.output():
        lost.append(n)
check("25 instant jobs, no output lost", not lost, str(lost))
background.take_finished()

print("\n--- a job's ending is redacted like a tool result ---")
# The notice is a message of its own, not a tool result, so the redaction that
# `run_cmd` and `read_background` get on the way out never saw it. The command
# reads the variable by name, so only the output can carry the value.
SECRET = "ghp_" + "notice0secret0value0123456789"
os.environ["GH_TOKEN"] = SECRET
try:
    tools.safe_run_cmd(py("import os; print(os.environ['GH_TOKEN'])"), "", "true")
    job = background.jobs()[-1]
    job.wait(10)
    check("the job did print the value", SECRET in job.output(), job.output()[:80])
    told = background.note(background.take_finished())
    check("the notice the model is given does not", SECRET not in told
          and "[hidden: $GH_TOKEN]" in told, told)
finally:
    del os.environ["GH_TOKEN"]

background.shutdown()
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("task checks passed")
