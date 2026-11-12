"""Ctrl+C stops what is running; leaving takes a second press within a second.

One press used to end the program from anywhere - a turn half answered, a
command half run - because `asyncio.run` turns the first Ctrl+C into a
cancellation nothing caught, and raises every later one wherever the thread
happens to be. So the harness takes the signal itself:

- **Counted once.** A press is counted where it arrives - by the handler for a
  signal, at the prompt for the key - and never twice for being caught twice.
- **Two inside the window leave.** One says how; one after the window has
  passed is a first one again.
- **Heard where something is listening.** A turn, the prompt, a `!command`, a
  question on screen: the Ctrl+C reaches them, as a cancellation when the loop
  is waiting and as an exception when code is holding the thread.
- **Deferred where nothing is.** Half-way through a slash command a raised
  Ctrl+C would end the program on one press, so it is said at the next prompt
  instead - unless it is the second, which is the one that means it.
- **A reply nobody is reading is not read.** The thread behind a stream stops,
  and closes the request under it, when its reader goes away.
"""
import asyncio
import contextlib
import io
import os
import signal
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="interrupt-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import app, promptkeys, providers, tui   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra and not ok else ''}")


def fresh():
    """As if Ctrl+C had never been pressed."""
    app._last_interrupt, app._leaving = 0.0, False
    app._stoppable, app._deferred_interrupt = False, False
    tui.listening = False


def press():
    """One Ctrl+C, delivered the way the terminal delivers it: as SIGINT."""
    signal.raise_signal(signal.SIGINT)
    time.sleep(0.01)           # the handler runs between two bytecodes; give it some


# ---------------------------------------------------------------------------
print("\n--- two inside the window leave; one says how ---")
fresh()
check("the first Ctrl+C does not leave", not app._interrupted(KeyboardInterrupt()))
check("a second one straight after does", app._interrupted(KeyboardInterrupt()))
fresh()
app._interrupted(KeyboardInterrupt())
app._last_interrupt -= app.EXIT_WINDOW + 1
check("a second one after the window is a first one again",
      not app._interrupted(KeyboardInterrupt()))
check("and the one after that leaves", app._interrupted(KeyboardInterrupt()))
fresh()
app._interrupted(KeyboardInterrupt())
app._last_interrupt -= 1.05
check("a second one more than a second later is a first one again",
      not app._interrupted(KeyboardInterrupt()))

print("\n--- how to leave is said beside the prompt, not printed ---")
fresh()
printed = io.StringIO()
with contextlib.redirect_stdout(printed):
    app._say_how_to_leave()
if config.PROMPT_TOOLKIT_AVAILABLE:
    check("nothing is printed above the prompt", printed.getvalue() == "",
          printed.getvalue().strip())
    check("it is said beside the prompt while the window is open",
          "Press Ctrl+C again to quit" in promptkeys.note(), promptkeys.note())
    time.sleep(app.EXIT_WINDOW + 0.1)
    check("and gone once a press would no longer quit", promptkeys.note() == "",
          promptkeys.note())

    # The redraw that takes the note away runs after a draw and asks for one,
    # so it is counted past the note's end: the state it leaves looks the same
    # whether it stopped once or kept going.
    class Redrawn:
        def __init__(self, loop):
            self.loop, self.draws = loop, 0

        def invalidate(self):       # a draw, then prompt_toolkit's after_render
            self.draws += 1
            self.loop.call_soon(promptkeys._expire_note, self)

    async def redraws():
        drawn = Redrawn(asyncio.get_running_loop())
        promptkeys.say("Press Ctrl+C again to quit", 0.2)
        promptkeys._expire_note(drawn)          # the draw that showed it
        await asyncio.sleep(1.0)
        return drawn.draws

    draws = asyncio.run(redraws())
    check("an expired note is taken away by one redraw, not one every 50 ms",
          draws == 1, f"{draws} redraws in 1s after a 0.2s note")
else:
    check("a terminal with no prompt_toolkit is still told",
          "Press Ctrl+C again to quit" in printed.getvalue(), printed.getvalue().strip())

print("\n--- at an open prompt the first press does not close it ---")


class Buffer:
    def __init__(self, text):
        self.text, self.kept = text, None

    def reset(self, append_to_history=False):
        self.kept = self.text if append_to_history else None
        self.text = ""


class Prompt:
    def __init__(self):
        self.closed_with = None

    def exit(self, result=None, exception=None, style=""):
        self.closed_with = exception


class Event:
    def __init__(self, text=""):
        self.current_buffer, self.app = Buffer(text), Prompt()


fresh()
first = Event("half a ques")
app._ctrl_c_at_prompt(first)
check("the first press leaves the prompt open", first.app.closed_with is None)
check("and clears the line into the history",
      first.current_buffer.text == "" and first.current_buffer.kept == "half a ques")
if config.PROMPT_TOOLKIT_AVAILABLE:
    check("and says how to leave beside it",
          "Press Ctrl+C again to quit" in promptkeys.note(), promptkeys.note())
second = Event()
app._ctrl_c_at_prompt(second)
check("the second inside the window closes it, already counted",
      isinstance(second.app.closed_with, app._Signalled))
check("and the main loop reads that as leaving", app._interrupted(second.app.closed_with))
if config.PROMPT_TOOLKIT_AVAILABLE:
    bound = {b.keys[0] for b in promptkeys.bindings(interrupt=app._ctrl_c_at_prompt).bindings}
    from prompt_toolkit.keys import Keys
    check("the prompt is given it as its Ctrl+C", Keys.ControlC in bound, str(bound))

print("\n--- a press is counted once, however it is caught ---")
fresh()
app._count_interrupt()                       # what the signal handler does
at = app._last_interrupt
check("a signal the handler counted is not counted again where it is caught",
      not app._interrupted(app._Signalled()) and app._last_interrupt == at)
fresh()
app._count_interrupt()
check("nor is the cancellation it asked for",
      not app._interrupted(asyncio.CancelledError()) and app._last_interrupt > 0)
check("but the next real press is the second", app._interrupted(KeyboardInterrupt()))

# ---------------------------------------------------------------------------
print("\n--- the signal, taken from asyncio for the life of the loop ---")
seen = {}


async def scenarios():
    loop = asyncio.get_running_loop()
    before = signal.getsignal(signal.SIGINT)
    give_back = app._own_ctrl_c()
    seen["taken"] = signal.getsignal(signal.SIGINT) is not before

    # Waiting, with something listening: the wait is cancelled.
    fresh()
    app._stoppable = True
    loop.call_later(0.05, signal.raise_signal, signal.SIGINT)
    try:
        await asyncio.sleep(5)
        seen["waiting"] = "slept through it"
    except asyncio.CancelledError as cancelled:
        seen["waiting"] = "cancelled"
        seen["waiting leaves"] = app._interrupted(cancelled)
    await app._absorb_cancel()
    try:
        await asyncio.sleep(0.01)
        seen["clean after"] = True
    except asyncio.CancelledError:
        seen["clean after"] = False

    # Holding the thread - a tool running: it is raised into that code.
    fresh()
    app._stoppable = True
    try:
        press()
        seen["holding"] = "not raised"
    except KeyboardInterrupt as raised:
        seen["holding"] = type(raised).__name__
    await app._absorb_cancel()
    try:
        await asyncio.sleep(0.01)
        seen["no second arrival"] = True
    except asyncio.CancelledError:
        seen["no second arrival"] = False

    # A `!command` catches its own Ctrl+C; the cancellation left owed must not
    # close the next prompt as a second press.
    fresh()
    pressed_at = app._last_interrupt
    app._stoppable = True
    try:
        press()
    except KeyboardInterrupt:
        pass                       # what `safe_run_cmd` does with it
    app._stoppable = False
    seen["caught leaves"] = await app._settle_caught(pressed_at)
    try:
        await asyncio.sleep(0.01)
        seen["caught clean after"] = True
    except asyncio.CancelledError:
        seen["caught clean after"] = False
    seen["nothing pressed"] = await app._settle_caught(app._last_interrupt)

    # Half-way through a slash command: nothing is listening.
    fresh()
    try:
        press()
        seen["deferred"] = app._deferred_interrupt
    except KeyboardInterrupt:
        seen["deferred"] = "raised"
    try:
        await asyncio.sleep(0.01)
        seen["deferred cancels"] = False
    except asyncio.CancelledError:
        seen["deferred cancels"] = True
    # ...and a second one there is the one that means it.
    try:
        press()
        seen["second there"] = "not raised"
    except KeyboardInterrupt as raised:
        seen["second there"] = app._interrupted(raised)
    await app._absorb_cancel()

    # A question on screen is somewhere a Ctrl+C is listened for.
    fresh()
    tui.listening = True
    try:
        press()
        seen["asking"] = "not raised"
    except KeyboardInterrupt:
        seen["asking"] = "raised"
    tui.listening = False
    try:
        await asyncio.sleep(0.01)
        seen["asking cancels"] = False
    except asyncio.CancelledError:
        seen["asking cancels"] = True

    give_back()
    seen["given back"] = signal.getsignal(signal.SIGINT) is before

asyncio.run(asyncio.wait_for(scenarios(), 20))
check("the handler is the harness's own while the loop runs", seen.get("taken"))
check("waiting on something: the wait is cancelled", seen.get("waiting") == "cancelled",
      str(seen.get("waiting")))
check("once is not leaving", seen.get("waiting leaves") is False)
check("and the next await is not cancelled again", seen.get("clean after") is True)
check("code holding the thread has it raised into it",
      seen.get("holding") == "_Signalled", str(seen.get("holding")))
check("with the cancellation it left absorbed, not delivered as a second press",
      seen.get("no second arrival") is True)
check("a Ctrl+C a command caught is not leaving", seen.get("caught leaves") is False)
check("and its owed cancellation does not reach the next prompt",
      seen.get("caught clean after") is True)
check("with no press, settling does nothing", seen.get("nothing pressed") is False)
check("half-way through a command it is put off, not raised",
      seen.get("deferred") is True, str(seen.get("deferred")))
check("and cancels nothing", seen.get("deferred cancels") is False)
check("a second one there is raised - and leaves", seen.get("second there") is True,
      str(seen.get("second there")))
check("a question on screen hears it wherever it was asked from",
      seen.get("asking") == "raised", str(seen.get("asking")))
check("without the command that asked being cancelled", seen.get("asking cancels") is False)
check("the handler is given back when the loop ends", seen.get("given back"))

# ---------------------------------------------------------------------------
print("\n--- a reply nobody is reading is not read to its end ---")
closed = threading.Event()
produced = []


def endless():
    try:
        for number in range(10_000):
            produced.append(number)
            time.sleep(0.005)
            yield {"text": str(number)}
    finally:
        closed.set()               # what closes the request under a real one


async def read_a_little():
    taken = []
    stream = providers._as_stream(endless)
    async for chunk in stream:
        taken.append(chunk)
        if len(taken) == 3:
            break
    await stream.aclose()
    return taken

taken = asyncio.run(asyncio.wait_for(read_a_little(), 10))
check("what was read is what was asked for", len(taken) == 3)
check("the generator behind it is closed", closed.wait(3))
count = len(produced)
time.sleep(0.2)
check("and stops producing", len(produced) == count and count < 100, str(len(produced)))

print("\n--- a long turn rings when it ends ---")


class Terminal:
    def __init__(self, tty=True):
        self.tty, self.written = tty, ""

    def isatty(self):
        return self.tty

    def write(self, text):
        self.written += text

    def flush(self):
        pass


config.TURN_BELL_SECONDS = 30
term = Terminal()
check("a turn of 31 seconds rings", app._ring_if_long(31, term) and term.written == "\a")
term = Terminal()
check("one of 5 does not", not app._ring_if_long(5, term) and term.written == "")
term = Terminal(tty=False)
check("nor does a pipe, however long", not app._ring_if_long(300, term) and not term.written)
config.TURN_BELL_SECONDS = 0
term = Terminal()
check("TURN_BELL_SECONDS 0 turns it off", not app._ring_if_long(300, term) and not term.written)
config.TURN_BELL_SECONDS = 30

print("\n--- Ctrl+C at a settings question says it was cancelled ---")
from aetheris import connect      # noqa: E402


def pressed(*_args, **_kwargs):
    raise KeyboardInterrupt


real_ask = tui.ask_the_driver
tui.ask_the_driver = pressed
said = io.StringIO()
try:
    with contextlib.redirect_stdout(said):
        answer = connect._ask("  Model id › ", title="Which model?")
        picked = connect._choose("Connect to", ["a", "b"], str)
finally:
    tui.ask_the_driver = real_ask
check("the answer is empty, as giving up always was", answer == "" and picked == -1)
check("and it is marked, so a caller does not say it twice",
      isinstance(answer, connect.Cancelled))
check("'Cancelled.' is said each time", said.getvalue().count("Cancelled.") == 2,
      repr(said.getvalue()[-120:]))

tui.ask_the_driver = pressed
said = io.StringIO()
try:
    with contextlib.redirect_stdout(said):
        connect._ensure_key("anthropic") if not os.environ.get("ANTHROPIC_API_KEY") else None
finally:
    tui.ask_the_driver = real_ask
check("the API key question says it once, not twice",
      os.environ.get("ANTHROPIC_API_KEY") or said.getvalue().count("Cancelled.") == 1,
      repr(said.getvalue()[-160:]))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    sys.exit(1)
print("all interrupt checks passed")
