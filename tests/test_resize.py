"""A terminal resized under the prompt, and the screen drawn again for it.

What was printed is laid out for the width it was printed at, and a terminal
does not reflow it. So the prompt watches the size while it waits:

- **Settled, not every step.** Dragging a window edge passes through dozens of
  sizes; only the one it stops at is drawn.
- **The prompt makes way and keeps the line.** It closes with `RESIZED`, what
  was half-typed is set aside, and the next prompt opens with it put back.
- **Typing still wins.** A line entered before any resize comes back as a
  line, with the watcher stopped behind it - and Ctrl+C comes back to the
  main loop as a `KeyboardInterrupt`, not out through the event loop.
- **The redraw** clears the screen and its scrollback, draws the banner and the
  conversation again, and records the size it drew for.
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="resize-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import app             # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def run(coro, timeout=5):
    return asyncio.run(asyncio.wait_for(coro, timeout))


size = [(80, 24)]
app._terminal_size = lambda: size[0]
app.RESIZE_POLL = 0.01


class FakeBuffer:
    text = "half a sentence"


class FakeSession:
    def __init__(self, answer=None):
        self.default_buffer = FakeBuffer()
        self.defaults = []
        self.answer = answer

    async def prompt_async(self, message, default=None):
        self.defaults.append(default)
        if self.answer is not None:
            return self.answer
        await asyncio.Event().wait()       # nobody types


# ---------------------------------------------------------------------------
print("\n--- noticing a size change ---")
app._drawn_size = None
check("the first look only records the size", not app._size_changed())
check("and records it", app._drawn_size == (80, 24), str(app._drawn_size))
size[0] = (100, 24)
check("a different width is a change", app._size_changed())
size[0] = (80, 30)
check("so is a different height", app._size_changed())
size[0] = (80, 24)
check("and the drawn size is no change at all", not app._size_changed())

# ---------------------------------------------------------------------------
print("\n--- a drag is drawn once, at the size it stops at ---")
app._drawn_size = (80, 24)
steps = iter([(80, 24), (78, 24), (74, 24), (70, 23), (66, 22), (60, 20)])
seen = []


def dragged():
    try:
        size[0] = next(steps)
    except StopIteration:
        pass
    seen.append(size[0])
    return size[0]


app._terminal_size = dragged
run(app._resized())
check("it resolves only once the size holds still", seen[-1] == (60, 20) and seen[-2] == (60, 20),
      str(seen[-3:]))
app._terminal_size = lambda: size[0]
size[0] = (60, 20)

# ---------------------------------------------------------------------------
print("\n--- the prompt makes way for a resize, and keeps the line ---")
app._drawn_size = (60, 20)
app._kept_typing = ""
prompt = FakeSession()


async def resize_soon():
    task = asyncio.ensure_future(app._typed_or_remote(prompt, "> "))
    await asyncio.sleep(0.05)
    size[0] = (120, 40)
    return await task

outcome = run(resize_soon())
check("the prompt closes for the resize", outcome is app.RESIZED)
check("the first prompt opened the ordinary way", prompt.defaults == [None], str(prompt.defaults))
check("what was half-typed is set aside", app._kept_typing == "half a sentence")


async def reopen():
    task = asyncio.ensure_future(app._typed_or_remote(prompt, "> "))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

app._drawn_size = (120, 40)
run(reopen())
check("and the next prompt opens with it put back",
      prompt.defaults[-1] == "half a sentence", str(prompt.defaults))

# ---------------------------------------------------------------------------
print("\n--- typing still wins ---")
app._kept_typing = ""
outcome = run(app._typed_or_remote(FakeSession(answer="  hello  "), "> "))
check("a line typed comes back as the line", outcome == "hello", repr(outcome))

# ---------------------------------------------------------------------------
print("\n--- Ctrl+C at the prompt reaches the main loop ---")
# Raised inside a task, a KeyboardInterrupt skips whoever awaits it and leaves
# through the event loop - past the main loop's `except`, and reported at exit
# as "Task exception was never retrieved".


class Interrupted(FakeSession):
    async def prompt_async(self, message, default=None):
        raise KeyboardInterrupt


async def press_ctrl_c():
    try:
        await app._typed_or_remote(Interrupted(), "> ")
    except KeyboardInterrupt:
        return "caught where the main loop catches it"
    return "not raised at all"

try:
    outcome = run(press_ctrl_c())
except KeyboardInterrupt:
    outcome = "escaped the event loop"
check("it is raised to the caller, not out of the loop",
      outcome == "caught where the main loop catches it", outcome)

# ---------------------------------------------------------------------------
print("\n--- the redraw ---")
app._welcome = lambda: print("BANNER")
size[0] = (90, 30)
app._drawn_size = (120, 40)
screen = io.StringIO()
before = sys.stdout
with contextlib.redirect_stdout(screen):
    app._redraw([{"role": "system", "content": "never shown"},
                 {"role": "user", "content": "what is a monad"},
                 {"role": "assistant", "content": "A burrito."}])
    restored = sys.stdout is screen
drawn = screen.getvalue()
check("the screen and its scrollback are cleared first",
      drawn.startswith("\033[2J\033[3J\033[H"), repr(drawn[:20]))
check("then the banner", "BANNER" in drawn)
check("then the conversation, in order",
      0 < drawn.index("what is a monad") < drawn.index("A burrito."))
check("without the system prompt", "never shown" not in drawn)
check("the size it drew for is recorded", app._drawn_size == (90, 30), str(app._drawn_size))
check("and stdout is left as it was found", restored and sys.stdout is before)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    sys.exit(1)
print("all resize checks passed")
