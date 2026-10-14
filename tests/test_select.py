"""A drag selects what it crosses the way the program understands it.

The terminal selects cells: a drag down a thought takes the `│` in front of
every row, and one that runs past the end of an answer takes the tool call
under it. With `MOUSE_SELECT` on, the mouse is the program's while the prompt
is open, and the selection is made from its own account of the screen:

- **The ledger.** What was printed, kept as the rows it became - wrapped where
  the terminal wraps, each row knowing whether it is a thought or an answer.
- **The selection.** Inside the block it began in, and inside the part of each
  row that is text: no gutter, no frame, no margin.
- **The copy.** The text as it was before it was laid out - a row the terminal
  wrapped is the same line going on, and so is one the program cut itself.
- **The paste.** More than a line of it is shown as its size and sent as it was.

Run:  python tests/test_select.py
"""
import base64
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="select-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import promptkeys, screen   # noqa: E402
from aetheris.config import S             # noqa: E402

failures = []
WIDTH = 40
config.tw = lambda: WIDTH


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra and not ok else ''}")


def fresh():
    ledger = screen.Ledger()
    screen.ledger = ledger
    ledger.on_change = promptkeys.rows_moved
    return ledger


def thought(ledger, line):
    took = []
    rows = config.wrap_cells(line, WIDTH - 5, by_word=True, joins=took)
    for index, row in enumerate(rows):
        with screen.block("thinking", took[index - 1] if index else None):
            ledger.feed(f"  {S.MUTED}│ {row}{S.R}\n")


print("--- the ledger: what was printed, as the rows it became ---")
ledger = fresh()
ledger.feed(f"  {S.MUTED}╭─ thinking{S.R}\n")
THOUGHT = "the quick brown fox jumps over the lazy dog and keeps going far"
thought(ledger, THOUGHT)
ledger.feed(f"  {S.MUTED}╰─{S.R}\n")
ANSWER = "가나다라 " * 12 + "end"
with screen.block("answer"):
    ledger.feed("  " + ANSWER + "\n")
    ledger.feed("  second line\n")
ledger.feed("  ▸ read_file notes.txt\n")
rows = list(ledger.rows)
check("a line longer than the window is the rows the terminal makes of it",
      [row.soft for row in rows[4:7]] == [False, True, True]
      and all(screen._cells(row.plain) <= WIDTH for row in rows), str([r.plain for r in rows]))
check("a wide character that does not fit goes to the next row whole",
      screen._cells(rows[4].plain) == WIDTH and rows[5].plain.startswith("나"), rows[5].plain)
check("each row knows what it is part of", [row.kind for row in rows] == [
    "", "thinking", "thinking", "", "answer", "answer", "answer", "answer", ""])
check("and a row the program cut knows what the cut took",
      rows[1].glue is None and rows[2].glue == " ")
check("the colour a row was printed in is kept with it", S.MUTED in rows[1].raw)

ledger.feed("\r  · thinking…  ")
ledger.feed("\r\033[K")
ledger.feed("  TPS: 12.0")
ledger.feed("\r\033[K")
ledger.feed("  kept\n")
check("a line drawn over - a spinner, a counter - leaves no row",
      len(ledger.rows) == len(rows) + 1 and ledger.rows[-1].plain == "  kept",
      str([r.plain for r in list(ledger.rows)[-3:]]))
ledger.feed("half an esc\033[38;2;1")
ledger.feed(";2;3mape\n")
check("an escape cut in two by the end of a write is still one escape",
      ledger.rows[-1].plain == "half an escape", repr(ledger.rows[-1].plain))
ledger.feed("\033[2J\033[H")
check("clearing the screen clears the account of it", len(ledger.rows) == 0)

print("\n--- the selection: inside the block, inside the text ---")
ledger = fresh()
ledger.feed(f"  {S.MUTED}╭─ thinking{S.R}\n")
thought(ledger, THOUGHT)
ledger.feed(f"  {S.MUTED}╰─{S.R}\n")
with screen.block("answer"):
    ledger.feed("  " + ANSWER + "\n")
    ledger.feed("  second line\n")
    ledger.feed(f"  {S.MUTED}╭─ python ──────────────────────────╮{S.R}\n")
    ledger.feed(f"  {S.MUTED}│{S.R} print('hi')                       {S.MUTED}│{S.R}\n")
    ledger.feed(f"  {S.MUTED}╰───────────────────────────────────╯{S.R}\n")
ledger.feed("  ▸ read_file notes.txt\n")

chosen = ledger.select((1, 0), (9, 5))
check("a drag that leaves the thought stops at its last word", sorted(chosen) == [1, 2], str(chosen))
check("and starts after the gutter, wherever the drag began", chosen[1][0] == 4, str(chosen))
check("copied, it is the thought as it was written", ledger.text(chosen) == THOUGHT,
      repr(ledger.text(chosen)))
chosen = ledger.select((1, 8), (1, 12))
check("part of a row is that part", ledger.text(chosen) == "quick", repr(ledger.text(chosen)))
chosen = ledger.select((1, 12), (1, 8))
check("dragged backwards, the same", ledger.text(chosen) == "quick")

chosen = ledger.select((4, 3), (99, 0))
check("an answer is copied across the terminal's wrap as one line",
      ledger.text(chosen).startswith(ANSWER + "\nsecond line"), repr(ledger.text(chosen)))
check("without the margin it was printed behind", not ledger.text(chosen).startswith(" "))
check("with what is in the code frame, and none of the frame",
      ledger.text(chosen).endswith("\nprint('hi')") and "│" not in ledger.text(chosen)
      and "╭" not in ledger.text(chosen), repr(ledger.text(chosen)))
check("and never what is under it", "read_file" not in ledger.text(chosen))
chosen = ledger.select((4, 9), (5, 6))
check("a drag that starts in the middle of a wide character takes all of it",
      ledger.text(chosen).startswith("라 가나") and chosen[4][0] == 8, repr(ledger.text(chosen)))
check("a frame is not something to select",
      ledger.select((0, 0), (0, 30)) == {} and ledger.select((3, 0), (3, 9)) == {})
chosen = ledger.select((11, 0), (11, 99))
check("anything else is selected as a line of its own",
      ledger.text(chosen) == "▸ read_file notes.txt", repr(ledger.text(chosen)))
check("a point past the end of the ledger selects nothing", ledger.select((40, 0), (41, 3)) == {})

print("\n--- drawing it ---")
lit = ledger.paint(1, (4, 9))
check("the chosen cells are drawn in reverse, the rest as they were",
      lit == f"  {S.MUTED}│ {screen.REVERSE}the q{screen.PLAIN}uick brown fox jumps over the{S.R}",
      repr(lit))
lit = screen._relight(f"ab{S.OK}cd{S.R}ef", 1, 5)
check("a colour change inside the selection does not end it",
      lit == f"a{screen.REVERSE}b{S.OK}{screen.REVERSE}cd{S.R}{screen.REVERSE}e{screen.PLAIN}f",
      repr(lit))
check("a row nothing is chosen on is drawn as it was printed",
      ledger.paint(1) == ledger.rows[1].raw)
drawn = screen.frame(3, {11: (2, 5)})
check("the rows above the prompt are drawn where they are, the cursor put back",
      drawn.startswith("\0337") and drawn.endswith("\0338")
      and "\033[1;1H" in drawn and "\033[3;1H" in drawn and "\033[4;1H" not in drawn, repr(drawn))
check("the newest row is the one just above the prompt",
      drawn.index("ead_file") > drawn.index("\033[3;1H"))
short = screen.frame(20, {})
check("a screen taller than what was printed is drawn from the bottom up",
      "\033[20;1H" in short and "\033[9;1H" in short and "\033[8;1H" not in short,
      repr(short[:80]))

print("\n--- the mouse: down, across, up ---")


class Output:
    def __init__(self):
        self.written = ""

    def write_raw(self, text):
        self.written += text

    def flush(self):
        pass


class Loop:
    def __init__(self):
        self.later = []

    def call_later(self, delay, function, *args):
        self.later.append((delay, function, args))


class App:
    def __init__(self):
        self.output, self.loop = Output(), Loop()

    def invalidate(self):
        pass


copied = []
screen.copy = lambda text: copied.append(text) or True
app = App()
ABOVE = len(ledger.rows)               # every row is on screen, the prompt under them
promptkeys.handle(app, ("down", 0, 1, ABOVE))
check("pressing draws the rows as the ledger has them, nothing lit",
      "\0337" in app.output.written and screen.REVERSE not in app.output.written)
promptkeys.handle(app, ("move", 20, 6, ABOVE))
check("dragging lights what is chosen", screen.REVERSE in app.output.written)
app.output.written = ""
promptkeys.handle(app, ("move", 20, 6, ABOVE))
check("a move that changes nothing draws nothing", app.output.written == "")
promptkeys.handle(app, ("up", 20, 6, ABOVE))
check("letting go copies it", copied == [THOUGHT], str(copied))
asked = re.search(r"\033\]52;c;([A-Za-z0-9+/=]+)\a", app.output.written)
check("and asks the terminal to as well, for a clipboard this machine cannot reach",
      asked and base64.b64decode(asked.group(1)).decode("utf-8") == THOUGHT)
check("it is said beside the prompt, for a moment", "copied" in promptkeys.note()
      and "63 characters" in promptkeys.note(), promptkeys.note())
check("the highlight is put out a moment later", len(app.loop.later) == 2
      and app.loop.later[0][0] == promptkeys.FLASH)
app.output.written = ""
delay, function, args = app.loop.later[0]
function(*args)
check("drawn again as it was", "\0337" in app.output.written
      and screen.REVERSE not in app.output.written)

app = App()
promptkeys.handle(app, ("down", 6, 4, ABOVE))
promptkeys.handle(app, ("up", 6, 4, ABOVE))
check("a click is one character, not a selection of everything", copied[-1] == "다", repr(copied[-1]))
copied.clear()
promptkeys.handle(app, ("up", 3, 3, ABOVE))
check("a button coming up with none down copies nothing", copied == [])
promptkeys.handle(app, ("down", 4, 1, ABOVE))
promptkeys.handle(app, ("move", 30, 2, ABOVE))
app.output.written = ""
ledger.feed("  a message from another agent\n")
check("something printed in the middle of a drag puts the highlight out first",
      "\0337" in app.output.written and screen.REVERSE not in app.output.written
      and promptkeys._drag is None, repr(app.output.written[:60]))
promptkeys.handle(app, ("up", 30, 2, ABOVE))
check("and the drag is over", copied == [])
promptkeys.handle(app, ("down", 4, 0, len(ledger.rows) + 5))
check("a row above everything the ledger holds selects nothing", promptkeys._drag is None)

print("\n--- what a terminal reports, read ---")


class Renderer:
    pass


class Event:
    def __init__(self, data):
        self.data = data
        self.app = App()
        self.app.renderer = Renderer()
        promptkeys._prompts[self.app] = {"peak": 0, "above": 12}


check("a press above the prompt: left button, column and row from 1",
      promptkeys._vt_point(Event("\x1b[<0;5;3M")) == ("down", 4, 2, 12))
check("a drag is a move with the button held",
      promptkeys._vt_point(Event("\x1b[<32;9;4M")) == ("move", 8, 3, 12))
check("a release", promptkeys._vt_point(Event("\x1b[<0;9;4m")) == ("up", 8, 3, 12))
check("the mouse only moving, no button, is not a drag",
      promptkeys._vt_point(Event("\x1b[<35;9;4M"))[0] == "other")
check("the wheel is not a click", promptkeys._vt_point(Event("\x1b[<64;9;4M"))[0] == "wheel")
check("the middle button is not this selection's",
      promptkeys._vt_point(Event("\x1b[<1;9;4M"))[0] == "other")
check("inside the prompt it is the prompt's own business",
      promptkeys._vt_point(Event("\x1b[<0;5;13M")) is None)
check("and so is a report in an older encoding", promptkeys._vt_point(Event("\x1b[M !!")) is None)
check("the right button going down is a paste, wherever it is",
      promptkeys._vt_point(Event("\x1b[<2;5;13M"))[0] == "paste"
      and promptkeys._vt_point(Event("\x1b[<2;5;3M"))[0] == "paste"
      and promptkeys._vt_point(Event("\x1b[<2;5;3m"))[0] == "other")


class Info:
    """A Windows console: the buffer scrolled 100 rows, the prompt on row 110."""

    class dwCursorPosition:
        Y = 110

    class srWindow:
        Top = 100


class WindowsEvent(Event):
    def __init__(self, data):
        super().__init__(data)
        self.app.renderer.output = type("Out", (), {
            "get_win32_screen_buffer_info": lambda self: Info})()
        self.app.renderer._cursor_pos = type("At", (), {"y": 0})


check("a Windows console reports rows of its whole buffer; they become rows of the screen",
      promptkeys._windows_point(WindowsEvent("LEFT;MOUSE_DOWN;4;102")) == ("down", 4, 2, 10))
check("its drag", promptkeys._windows_point(WindowsEvent("LEFT;MOUSE_MOVE;9;103"))
      == ("move", 9, 3, 10))
check("its release, which names no button",
      promptkeys._windows_point(WindowsEvent("NONE;MOUSE_UP;9;103")) == ("up", 9, 3, 10))
check("its mouse only moving", promptkeys._windows_point(
    WindowsEvent("NONE;MOUSE_MOVE;9;103"))[0] == "other")
check("its wheel", promptkeys._windows_point(WindowsEvent("NONE;SCROLL_UP;9;103"))[0] == "wheel")
check("its right button", promptkeys._windows_point(
    WindowsEvent("RIGHT;MOUSE_DOWN;9;115"))[0] == "paste")
check("and inside the prompt, the prompt's own",
      promptkeys._windows_point(WindowsEvent("LEFT;MOUSE_DOWN;4;110")) is None)

unknown = Event("\x1b[<0;5;3M")
promptkeys._prompts[unknown.app]["above"] = None
check("while the prompt does not know where it is, nothing is selected",
      promptkeys._vt_point(unknown) is None)

print("\n--- where the prompt is, wherever it has pushed the rows above it ---")


class Hook:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class Terminal:
    """A 30-row terminal with the prompt on its last row."""

    def __init__(self):
        self._min_available_height = 1
        self._last_screen = type("Drawn", (), {"height": 1})()
        self.output = type("Out", (), {"get_size": lambda self: type("Size", (), {"rows": 30})})()


class Mouse(Output):
    def __init__(self):
        super().__init__()
        self.mouse = []

    def enable_mouse_support(self):
        self.mouse.append("on")

    def disable_mouse_support(self):
        self.mouse.append("off")


class Keys_:
    """A key processor, as far as `watch` reads one."""

    def __init__(self):
        self.after_key_press = Hook()
        self._previous_key_sequence = []

    def press(self, key):
        self._previous_key_sequence = [type("Press", (), {"key": key})()]
        for handler in self.after_key_press.handlers:
            handler(self)


class Prompt(App):
    def __init__(self):
        super().__init__()
        self.renderer, self.after_render, self.before_render = Terminal(), Hook(), Hook()
        self.is_done = False
        self.output, self.key_processor = Mouse(), Keys_()

    def render(self, height=None, room=None):
        for handler in self.before_render.handlers:
            handler(self)
        if height is not None:
            self.renderer._last_screen.height = height
        if room is not None:
            self.renderer._min_available_height = room
        for handler in self.after_render.handlers:
            handler(self)


prompt = Prompt()
promptkeys.watch(prompt)
check("not known before the prompt has drawn", promptkeys.rows_known(prompt) is None)
prompt.render()
check("on the last row, every other row is above it", promptkeys.rows_known(prompt) == 29)
prompt.render(height=9)
check("a menu opening under it pushes them up", promptkeys.rows_known(prompt) == 21)
drag = promptkeys.Drag(prompt, (len(ledger.rows) - 1, 2), 29)
drag.seen = 29
drag.settle()
check("and a selection drawn before moves with them", drag.rows_above == 21)
prompt.render(height=1)
check("the menu closing does not bring them back", promptkeys.rows_known(prompt) == 21)
prompt.render(room=0)
check("after something is printed above it, it has to ask again",
      promptkeys.rows_known(prompt) is None)
check("though where it last was is kept, for a prompt that has just closed",
      promptkeys._last_known(prompt) == 21)
prompt.render(room=6)
check("and starts again from what the terminal says", promptkeys.rows_known(prompt) == 24)
prompt.render(height=4)
check("a prompt with room under it pushes nothing", promptkeys.rows_known(prompt) == 24)

# Enter pressed while a selection is still lit: the prompt's last drawing may
# scroll the screen, so the highlight has to be out before it.
ABOVE = len(ledger.rows)
promptkeys.handle(prompt, ("down", 4, 1, ABOVE))
promptkeys.handle(prompt, ("move", 30, 2, ABOVE))
promptkeys.handle(prompt, ("up", 30, 2, ABOVE))
check("a selection just copied is still lit", len(promptkeys._lit) == 1)
prompt.output.written = ""
prompt.is_done = True
prompt.render()
check("and is put out before the prompt draws itself closed",
      not promptkeys._lit and "\0337" in prompt.output.written
      and screen.REVERSE not in prompt.output.written, repr(prompt.output.written[:50]))
prompt.output.written = ""
screen.typed("  ❯ ", "next")
check("so the row the prompt leaves behind draws nothing over it", prompt.output.written == "")

if config.PROMPT_TOOLKIT_AVAILABLE:
    from prompt_toolkit.keys import Keys
    bound = {binding.keys[0] for binding in promptkeys.bindings().bindings}
    check("the prompt is given a paste, and both kinds of mouse report",
          bound == {Keys.BracketedPaste, Keys.Vt100MouseEvent, Keys.WindowsMouseEvent},
          str(bound))
check("a setting, on unless switched off", config.MOUSE_SELECT is True
      and "MOUSE_SELECT" in config.settable())

print("\n--- the wheel still scrolls: it hands the mouse back until a key is pressed ---")
prompt = Prompt()
promptkeys.watch(prompt)
promptkeys.handle(prompt, ("wheel", 3, 3, 0))
check("the first turn of the wheel gives the mouse to the terminal",
      prompt.output.mouse == ["off"] and prompt in promptkeys._scrolling)
promptkeys.handle(prompt, ("wheel", 3, 3, 0))
check("once", prompt.output.mouse == ["off"])
prompt.key_processor.press("<vt100-mouse-event>")
prompt.key_processor.press("<cursor-position-response>")
check("a mouse report already on its way is not somebody typing",
      prompt.output.mouse == ["off"])
prompt.key_processor.press("a")
check("the next key pressed takes it back", prompt.output.mouse == ["off", "on"]
      and prompt not in promptkeys._scrolling)
prompt.key_processor.press("b")
check("and typing on changes nothing more", prompt.output.mouse == ["off", "on"])
config.MOUSE_SELECT = False
promptkeys.handle(prompt, ("wheel", 3, 3, 0))
prompt.key_processor.press("a")
check("with MOUSE_SELECT off there is nothing to take back",
      prompt.output.mouse == ["off", "on", "off"])
config.MOUSE_SELECT = True
check("the wheel is the wheel wherever it turns, the prompt's own rows included",
      promptkeys._vt_point(Event("\x1b[<65;9;13M"))[0] == "wheel")

print("\n--- a paste is shown as its size and sent as it was ---")
LOG = "alpha\nbeta\ngamma"
label = promptkeys.paste_label(LOG)
check("more than a line of it is a placeholder", label == "[Copied text Line:3 Chars:16]", label)
check("which says how much there is", promptkeys.is_long(LOG) and not promptkeys.is_long("a word"))
check("a long single line is one too", promptkeys.is_long("x" * (promptkeys.PASTE_CHARS + 1)))
check("put back on the way out, where it was",
      promptkeys.expand_pastes(f"why does {label} fail?") == f"why does {LOG} fail?")
other = promptkeys.paste_label("one\ntwo\nthree333")
check("two pastes of the same shape are still two",
      other == "[Copied text #2 Line:3 Chars:16]"
      and promptkeys.expand_pastes(f"{label} and {other}") == f"{LOG} and one\ntwo\nthree333", other)
check("the same thing pasted twice is one placeholder", promptkeys.paste_label(LOG) == label)
check("a placeholder somebody edited goes as typed",
      promptkeys.expand_pastes("[Copied text Line:3 Chars:99]") == "[Copied text Line:3 Chars:99]")
check("and a line with none is untouched", promptkeys.expand_pastes("hello") == "hello")


class Buffer:
    def __init__(self):
        self.text = ""

    def insert_text(self, text):
        self.text += text


class Pasted:
    def __init__(self, data):
        self.data, self.current_buffer = data, Buffer()


if config.PROMPT_TOOLKIT_AVAILABLE:
    paste = next(binding.handler for binding in promptkeys.bindings().bindings
                 if binding.keys[0] == Keys.BracketedPaste)
    event = Pasted("first\r\nsecond\r\n")
    paste(event)
    check("the box gets the placeholder, whatever the line endings were",
          event.current_buffer.text == "[Copied text Line:2 Chars:12]", event.current_buffer.text)
    check("for the text with one kind of line ending",
          promptkeys.expand_pastes(event.current_buffer.text) == "first\nsecond")
    event = Pasted("just this")
    paste(event)
    check("a pasted word is typed, as it always was", event.current_buffer.text == "just this")

    # With the mouse taken, the terminal's right-click paste is the program's to do.
    click = next(binding.handler for binding in promptkeys.bindings().bindings
                 if binding.keys[0] == Keys.Vt100MouseEvent)
    real_clipboard, screen.clipboard = screen.clipboard, lambda: "from\nthe\nclipboard"
    event = Event("\x1b[<2;5;3M")
    event.current_buffer = Buffer()
    try:
        click(event)
    finally:
        screen.clipboard = real_clipboard
    check("a right click pastes what the clipboard holds, the same way",
          event.current_buffer.text == "[Copied text Line:3 Chars:18]", event.current_buffer.text)

print("\n--- the clipboard ---")
check("the terminal is asked in the escape it understands",
      screen.osc52("가") == "\033]52;c;" + base64.b64encode("가".encode()).decode() + "\a")
check("and not asked for more than it would take",
      screen.osc52("x" * (screen.OSC52_LIMIT + 1)) == "")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    sys.exit(1)
print("all selection checks passed")
