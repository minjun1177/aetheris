"""What the prompt does with a paste, and with the mouse.

Two things arrive at the prompt that are not typing.

**A paste.** Forty lines of a log pasted into the box used to be forty lines
of box: the prompt grew past the screen, and the question typed after them was
somewhere under it. A paste of more than a line is shown as what it is -
`[Copied text Line:40 Chars:2113]` - and sent as what it was.

**The mouse.** With `MOUSE_SELECT` on, a drag over what is printed above the
prompt selects it the way `screen` understands it: inside the thought or the
answer it began in, without the gutter drawn down its side. Letting go copies
it. See `screen` for where the rows come from. A right click pastes and the
wheel still scrolls - both things the terminal did for itself until the mouse
became this program's.
"""
import re
import time

from aetheris import config, screen
from aetheris.config import S

# A paste of this many lines, or this many characters on fewer, is shown as a
# placeholder. Under both it is ordinary typing, only faster.
PASTE_LINES = 2
PASTE_CHARS = 400

FLASH = 0.35                  # seconds a selection stays lit after it is copied
NOTE_SECONDS = 2.5            # ... and its "copied" stays beside the prompt

_pastes: dict = {}            # placeholder -> what was pasted, for this run


class Expand(str):
    """What the prompt returns for Ctrl+O: the line as it stood, to put back.

    A `str`, so whatever reads the closed prompt's line - the record of what
    is on screen - reads the half-typed text, and a type of its own, so the
    main loop can tell it from a line that was sent.
    """


def paste_label(text: str) -> str:
    """The placeholder for `text`, remembered so it can be put back."""
    lines = text.count("\n") + 1
    label, number = f"[Copied text Line:{lines} Chars:{len(text)}]", 1
    # Two pastes of the same shape are still two pastes.
    while _pastes.get(label, text) != text:
        number += 1
        label = f"[Copied text #{number} Line:{lines} Chars:{len(text)}]"
    _pastes[label] = text
    return label


def is_long(text: str) -> bool:
    return text.count("\n") + 1 >= PASTE_LINES or len(text) > PASTE_CHARS


def expand_pastes(line: str) -> str:
    """`line` with every placeholder in it turned back into what was pasted.

    A placeholder somebody edited is no longer one, and goes as typed.
    """
    for label in sorted((label for label in _pastes if label in line), key=len,
                        reverse=True):
        line = line.replace(label, _pastes[label])
    return line


# ---------------------------------------------------------------------------
# the mouse
# ---------------------------------------------------------------------------

_SGR_MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

_drag = None                  # the selection being dragged out, if one is
_note = ("", 0.0)             # what to say beside the prompt, and until when
_note_timer = None            # the redraw that will take an expired note away


WHEEL_ROWS = 3                # rows a drag scrolls by for one notch of the wheel
EDGE_SECONDS = 0.05           # how often a drag held at the edge scrolls a row


class Drag:
    def __init__(self, app, anchor: tuple, rows_above: int):
        self.app = app
        self.anchor = anchor          # (ledger row, column) the button went down on
        self.rows_above = rows_above
        self.seen = _last_known(app)  # where the prompt was, when that was true
        self.chosen: dict = {}
        self.scroll = 0               # rows the view is drawn back from the last one
        self.pointer = None           # (x, y) on screen, where the button is now
        self.edging = False           # a timer is scrolling it from the edge

    def settle(self) -> None:
        """Move with the prompt, if the prompt has moved since this was last right."""
        now = _last_known(self.app)
        if now is not None and self.seen is not None:
            self.rows_above += now - self.seen
        self.seen = now


# Where each prompt's first row is on screen, kept up to date as it draws.
#
# prompt_toolkit knows this only as of the last time it asked the terminal. A
# prompt that grows at the bottom of the screen - the command menu opening
# under a `/` - scrolls everything above it up, and when it shrinks again the
# rows stay where they were pushed: its own `rows_above_layout` goes back to
# the old figure, eight rows out. So the tallest it has been since it last
# asked is remembered here, which is how far it has pushed.
_prompts: dict = {}


def watch(app) -> None:
    """Follow `app`'s place on the screen. Called once, when the prompt is made."""
    state = _prompts.setdefault(app, {"peak": 0, "above": None, "last": None})

    def rendered(sender):
        _expire_note(sender)
        try:
            renderer = sender.renderer
            room = renderer._min_available_height
            height = renderer._last_screen.height if renderer._last_screen else 0
            rows = renderer.output.get_size().rows
        except Exception:
            state["above"] = None
            return
        if room <= 0:
            # It has asked the terminal where it is and not been told yet:
            # something was printed above it, or it has only just opened.
            state["peak"], state["above"] = 0, None
            return
        state["peak"] = max(state["peak"], height)
        state["above"] = state["last"] = rows - max(room, state["peak"])

    def closing(sender):
        # The prompt's last drawing puts the cursor under it - and at the
        # bottom of the screen that scrolls every row up by one. Whatever is
        # lit is put out before that, while it is still where it was drawn.
        if sender.is_done:
            rows_moved()

    app.after_render += rendered
    app.before_render += closing
    def keyed(processor):
        # Not every "key" is one: a mouse report still on its way when the
        # mouse was handed back, the terminal answering where the cursor is.
        handled = getattr(processor, "_previous_key_sequence", None) or []
        key = handled[-1].key if handled else None
        if getattr(key, "value", key) not in _NOT_TYPING:
            take_back(app)

    app.key_processor.after_key_press += keyed


def rows_known(app):
    """The rows above `app`'s prompt, or None while that is not known."""
    return _prompts.get(app, {}).get("above")


def _last_known(app):
    """The same, as of the last time it was known - which a closed prompt's
    last drawing still is, until something is printed under it."""
    return _prompts.get(app, {}).get("last")


def note() -> str:
    """What was just copied, for a moment, on the right of the prompt."""
    text, until = _note
    try:
        from prompt_toolkit.application import get_app
        if get_app().is_done:
            return ""             # not part of the line the prompt leaves behind
        if get_app() in _scrolling:
            return f"{S.MUTED}scrolling - any key takes the mouse back{S.R}"
    except Exception:
        pass
    return text if time.monotonic() < until else ""


def say(text: str, seconds: float) -> None:
    """Say something beside the prompt, for `seconds`, on the prompt's own row.

    Set while the prompt is open it shows on the next draw; set while it is
    closed, it is there on the row being typed at when the prompt opens again -
    the same place a drag says what it copied.
    That is where a notice about the key that was just pressed belongs: printed
    above, it pushes the conversation up a line every time it is said, for
    something that is over in a second.
    """
    global _note
    _note = (f"{S.MUTED}{text}{S.R}", time.monotonic() + seconds)


def _expire_note(app) -> None:
    """Draw the prompt again once a note beside it has had its moment.

    prompt_toolkit redraws only when something happens, so without this a note
    that has expired would sit there looking true until the next keystroke.

    Called after every draw, the redraw this asks for included - so a note that
    has already had its moment asks for nothing. Expiry does not clear the
    text, and without that check every draw scheduled the next one: the prompt
    redrawn twenty times a second for as long as it stayed open.
    """
    global _note_timer
    text, until = _note
    if not text or _note_timer is not None or time.monotonic() >= until:
        return

    def settle():
        global _note_timer
        _note_timer = None
        if time.monotonic() < _note[1]:
            _expire_note(app)      # a newer note took this one's place
            return
        try:
            app.invalidate()
        except Exception:
            pass

    try:
        _note_timer = app.loop.call_later(
            max(until - time.monotonic(), 0) + 0.05, settle)
    except Exception:
        _note_timer = None         # nothing to schedule on: it goes on the next draw


# A terminal reports all of the mouse or none of it, so taking the drag takes
# the wheel too - and the wheel is how what was said an hour ago is read. So
# the first turn of the wheel hands the mouse back to the terminal, which
# scrolls from then on as it always did, and the next key pressed takes it
# again. That one first notch is the price: it is reported here, not scrolled.
_scrolling: set = set()       # prompts whose mouse is the terminal's for now
_NOT_TYPING = ("<vt100-mouse-event>", "<windows-mouse-event>", "<scroll-up>",
               "<scroll-down>", "<cursor-position-response>")


def hand_back(app) -> None:
    """The wheel turned: let the terminal have the mouse until a key is pressed."""
    if app in _scrolling:
        return
    rows_moved()                  # nothing left lit under a view about to scroll
    _scrolling.add(app)
    app.output.disable_mouse_support()
    app.output.flush()


def take_back(app) -> None:
    """A key was pressed: the person is at the prompt again, and so is the mouse."""
    if app not in _scrolling:
        return
    _scrolling.discard(app)
    if config.MOUSE_SELECT and not app.is_done:
        app.output.enable_mouse_support()
        app.output.flush()


def _row_of(y: int, rows_above: int, scroll: int = 0) -> int:
    """The ledger row drawn on screen row `y`, the last being just above the prompt
    - or `scroll` rows before it, once a drag has scrolled the view back."""
    return len(screen.ledger.rows) - rows_above + y - scroll


def _draw(drag) -> None:
    drag.settle()
    out = drag.app.output
    out.write_raw(screen.frame(drag.rows_above, drag.chosen, drag.scroll))
    out.flush()


def _choose(drag) -> bool:
    """Select from the anchor to where the button is now. Whether that changed."""
    if drag.pointer is None:
        return False
    x, y = drag.pointer
    head = (_row_of(y, drag.rows_above, drag.scroll), x)
    chosen = screen.ledger.select(drag.anchor, head)
    if chosen == drag.chosen:
        return False
    drag.chosen = chosen
    return True


def press(app, x: int, y: int, rows_above: int) -> None:
    """The button went down on row `y`, column `x`, above the prompt."""
    global _drag
    release(app, copy=False)
    index = _row_of(y, rows_above)
    if not 0 <= index < len(screen.ledger.rows):
        return
    _drag = Drag(app, (index, x), rows_above)
    _drag.pointer = (x, y)
    # Drawn with nothing chosen yet: from here the rows on screen are the rows
    # in the ledger, whatever they were a moment ago.
    _draw(_drag)


def move(app, x: int, y: int, rows_above: int) -> None:
    """The mouse moved with the button held."""
    drag = _drag
    if drag is None:
        return
    drag.rows_above, drag.seen = rows_above, _last_known(app)
    drag.pointer = (x, y)
    if _choose(drag):
        _draw(drag)
    _keep_edging(drag)


def scroll(app, rows: int) -> None:
    """Carry the drag `rows` back through what was printed (forward when negative).

    A terminal scrolls its own scrollback, which a drag cannot reach: what it
    holds is cells, and the drag selects from the ledger. So the wheel turned
    with the button held scrolls the rows above the prompt here instead,
    redrawn from the ledger, and the selection runs on into them. The screen
    is put back as it was when the button comes up.
    """
    drag = _drag
    if drag is None or not rows:
        return
    drag.settle()
    deepest = max(len(screen.ledger.rows) - drag.rows_above, 0)
    wanted = min(max(drag.scroll + rows, 0), deepest)
    if wanted == drag.scroll:
        return
    drag.scroll = wanted
    _choose(drag)
    _draw(drag)
    _say_scrolled(drag)


def _say_scrolled(drag) -> None:
    global _note
    if drag.scroll:
        _note = (f"{S.MUTED}⇡ {drag.scroll} rows up - let go to copy{S.R}",
                 time.monotonic() + NOTE_SECONDS)
    else:
        _note = ("", 0.0)
    try:
        drag.app.invalidate()
    except Exception:
        pass


def _edge_step(drag) -> int:
    """Which way a drag held at the edge scrolls: back from the top row, on
    from the bottom one once it has scrolled back, else not at all."""
    if drag.pointer is None:
        return 0
    y = drag.pointer[1]
    if y <= 0:
        return 1
    if y >= drag.rows_above - 1 and drag.scroll > 0:
        return -1
    return 0


def _keep_edging(drag) -> None:
    """Scroll while the button is held at the edge. A terminal reports the
    mouse only when it moves, so a pointer kept still there needs a timer."""
    if drag.edging or not _edge_step(drag):
        return
    drag.edging = True

    def step():
        drag.edging = False
        if drag is not _drag:
            return                      # let go, or another drag began
        going = _edge_step(drag)
        if not going:
            return
        before = drag.scroll
        scroll(drag.app, going)
        if drag.scroll != before:
            _keep_edging(drag)          # the end of the ledger stops it

    try:
        drag.app.loop.call_later(EDGE_SECONDS, step)
    except Exception:
        drag.edging = False


def release(app, copy: bool = True) -> str:
    """The button came up: copy what was chosen. Returns it."""
    global _drag, _note
    drag, _drag = _drag, None
    if drag is None:
        return ""
    text = screen.ledger.text(drag.chosen) if copy else ""
    if text:
        took = screen.copy(text)
        asked = screen.osc52(text)
        if asked:
            app.output.write_raw(asked)
            app.output.flush()
        size = (f"{text.count(chr(10)) + 1} lines" if "\n" in text
                else f"{len(text)} characters")
        if took:
            _note = (f"{S.MUTED}⧉ copied {size}{S.R}", time.monotonic() + NOTE_SECONDS)
        elif asked:
            # The terminal was asked and never says whether it did - many
            # refuse OSC 52, or leave it switched off - so this is not "copied".
            _note = (f"{S.MUTED}⧉ {size} sent to the terminal to copy{S.R}",
                     time.monotonic() + NOTE_SECONDS)
        else:
            _note = (f"{S.WARN}⧉ nothing here can reach the clipboard{S.R}",
                     time.monotonic() + NOTE_SECONDS)
    elif drag.scroll:
        _note = ("", 0.0)             # "rows up" is over, and nothing was copied
    if not drag.chosen:
        if drag.scroll:
            # Scrolled back with nothing chosen: the screen goes back to what
            # it was, now, since there is no highlight to leave lit a moment.
            drag.scroll = 0
            try:
                _draw(drag)
            except Exception:
                pass
        return text

    _lit.append(drag)
    try:
        # Lit a moment longer, so that it is seen to have been taken.
        app.loop.call_later(FLASH, _unlight, drag)
        app.loop.call_later(NOTE_SECONDS + 0.1, app.invalidate)
    except Exception:
        _unlight(drag)
    return text


_lit: list = []               # selections copied and still lit


def _unlight(drag) -> None:
    """Draw a selection's rows as they were. Once: it may be asked for twice."""
    if drag not in _lit and drag is not _drag:
        return
    if drag in _lit:
        _lit.remove(drag)
    if (drag.chosen or drag.scroll) and _drag in (None, drag):
        # Not under a newer drag: every drawing is of all the rows, and that
        # one has drawn these as they are already. Drawn at the bottom again:
        # the rows a drag scrolled back to were only ever drawn over the screen.
        drag.chosen, drag.scroll = {}, 0
        try:
            _draw(drag)
        except Exception:
            pass


def rows_moved() -> None:
    """Something is about to be printed: put out whatever is lit, now.

    Called by the ledger before its rows change, while every row is still
    where it was drawn. A moment later they will have scrolled, and a
    highlight left on would scroll away with them into the scrollback.
    """
    global _drag
    if _drag is None and not _lit:
        return
    drag, _drag = _drag, None
    for lit in ([drag] if drag else []) + list(_lit):
        if lit is drag:
            _lit.append(lit)
        _unlight(lit)


screen.ledger.on_change = rows_moved


def _vt_point(event):
    """A terminal's mouse report, as (what, x, y, rows above the prompt).

    None for one that is the prompt's own business: inside its rows, or in an
    encoding with no room for a column past 223.
    """
    report = _SGR_MOUSE.fullmatch(event.data)
    if not report:
        return None
    code, x, y = int(report[1]), int(report[2]) - 1, int(report[3]) - 1
    if code & 0b1100011 == 2 and report[4] == "M":
        return "paste", x, y, 0   # the right button, wherever it went down
    if code & 64:
        # And the wheel, wherever it turned - with how far it would carry a
        # drag in place of rows above: 64 is up, 65 down, 66 and 67 sideways.
        step = {0: WHEEL_ROWS, 1: -WHEEL_ROWS}.get(code & 3, 0)
        return "wheel", x, y, step
    above = rows_known(event.app)
    if above is None:
        return None               # the prompt does not know where it is yet
    if y >= above:
        if _drag is None or above < 1:
            return None
        # A drag carried down onto the prompt is still the drag - the edge it
        # scrolls on, and where the button may come up - not a click there.
        y = above - 1
    if code & 3:
        return "other", x, y, above           # not the left button, or none held
    what = "up" if report[4] == "m" else ("move" if code & 32 else "down")
    return what, x, y, above


def _windows_point(event):
    """The same, from a Windows console's own mouse record."""
    try:
        button, what, x, y = event.data.split(";")
        if button == "RIGHT" and what == "MOUSE_DOWN":
            return "paste", int(x), int(y), 0
        if what.startswith("SCROLL"):
            step = {"SCROLL_UP": WHEEL_ROWS, "SCROLL_DOWN": -WHEEL_ROWS}.get(what, 0)
            return "wheel", int(x), int(y), step
        info = event.app.renderer.output.get_win32_screen_buffer_info()
        top = info.dwCursorPosition.Y - event.app.renderer._cursor_pos.y
        window = info.srWindow.Top
        x, y = int(x), int(y)
    except Exception:
        return None
    if y >= top:
        if _drag is None or top - window < 1:
            return None
        y = top - 1                           # held on the prompt: still the drag
    above, y = top - window, y - window       # rows of the buffer -> rows on screen
    if what == "MOUSE_UP":                    # reported with no button: it is up
        return "up", x, y, above
    if button != "LEFT":
        return "other", x, y, above
    return {"MOUSE_DOWN": "down", "MOUSE_MOVE": "move"}.get(what, "other"), x, y, above


def handle(app, point) -> None:
    what, x, y, above = point
    try:
        if what == "wheel" and _drag is not None:
            scroll(app, above)          # held: the drag scrolls, it is not let go
        elif what == "wheel":
            hand_back(app)
        elif what == "down":
            press(app, x, y, above)
        elif what == "move":
            move(app, x, y, above)
        elif what == "up":
            move(app, x, y, above)
            release(app)
    except Exception:
        # A selection is a convenience. Whatever went wrong with one must not
        # be what closes the prompt.
        rows_moved()


def bindings(interrupt=None):
    """The key bindings a `PromptSession` is given. Needs prompt_toolkit.

    `interrupt`, if given, is what Ctrl+C does instead of closing the prompt.
    """
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.bindings.mouse import load_mouse_bindings
    from prompt_toolkit.keys import Keys

    stock = {binding.keys[0]: binding.handler for binding in load_mouse_bindings().bindings}
    keys = KeyBindings()

    def insert(event, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        held = text.strip("\n")
        event.current_buffer.insert_text(paste_label(held) if is_long(held) else text)

    @keys.add(Keys.BracketedPaste)
    def pasted(event):
        insert(event, event.data)

    def mouse(read, key):
        def handler(event):
            point = read(event) if config.MOUSE_SELECT else None
            if point is None:
                return stock[key](event)
            if point[0] == "paste":
                # The terminal's own right-click paste stopped when the mouse
                # became this program's; this is it, back.
                insert(event, screen.clipboard())
                return None
            handle(event.app, point)
            # Nothing of the prompt's changed, except the note beside it.
            return None if point[0] in ("up", "wheel") else NotImplemented
        return handler

    @keys.add("c-o")
    def expand(event):
        # The last turn in full (`transcript`). Printed by the main loop, not
        # from here: drawn under an open prompt it would be drawn over.
        event.app.exit(result=Expand(event.current_buffer.text))

    if interrupt is not None:
        keys.add("c-c")(interrupt)
    keys.add(Keys.Vt100MouseEvent)(mouse(_vt_point, Keys.Vt100MouseEvent))
    keys.add(Keys.WindowsMouseEvent)(mouse(_windows_point, Keys.WindowsMouseEvent))
    return keys
