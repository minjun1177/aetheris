"""What is on the screen, row by row - so a drag can select part of it.

A terminal selects cells, and knows nothing of what they are: a drag down a
block of reasoning takes the `│` in front of every row with it, and one that
runs past the end of an answer takes the tool call under it. The terminal
cannot be told otherwise, so while the prompt is open the mouse is taken from
it (`MOUSE_SELECT`) and the selecting is done here, where it is known which
rows are a thought, which are an answer, and where on each row the text
starts.

Three parts:

- **The ledger** (`Ledger`). Everything the program prints passes through a
  `Recorder` on `sys.stdout`, and is kept as the screen rows it became -
  wrapped where the terminal wraps, with the colour it was printed in. The
  code that prints a thought or an answer says so (`block`), and each row
  remembers.
- **The selection** (`Ledger.select`, `Ledger.text`). A drag is two points; the
  selection is the cells between them, kept inside the block it started in
  and inside the part of each row that is text.
- **The clipboard** (`copy`). The platform's own tool where there is one, and
  OSC 52 as well, which is what reaches the clipboard through SSH.

Nothing here reads the terminal. The ledger is this program's own account of
what it printed, and `frame` paints the rows above the prompt from that
account before anything is highlighted - so what is selected is always what
is shown, even where the account and the screen had drifted apart.
"""
import base64
import collections
import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading

from aetheris import config

# More than any terminal is tall. Only the rows on screen can be dragged over,
# so nothing older than this is ever asked for.
ROWS_KEPT = 600

# What the program writes that is not text: colour (`m`), erasing the line
# (`K`) or the screen (`J`), and - with the screen - going home (`H`).
_TOKEN = re.compile(r"(\033\[[0-9;?]*[A-Za-z]|\033\][^\a\033]*(?:\a|\033\\)|\r|\n|\t)")
_PARTIAL = re.compile(r"\033(?:\[[0-9;?]*)?$")
_ESCAPE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")

# Where a row's text starts when it is drawn in a gutter, and the rows that are
# only the box around something.
_GUTTER = re.compile(r"^\s*[│┃❯$] ")
_FRAME = re.compile(r"^\s*(?:[╭╰]─|[─━]{4,}\s*$)")
_INDENT = 2                   # the margin everything else is printed behind

REVERSE, PLAIN = "\033[7m", "\033[27m"
OSC52_LIMIT = 100_000         # bytes of text; terminals refuse far less than this


class Row:
    """One row of the screen: what was written on it, and what it is part of."""

    __slots__ = ("raw", "kind", "soft", "glue")

    def __init__(self, raw: str, kind: str, soft: bool, glue):
        self.raw = raw            # the row as printed, colour included
        self.kind = kind          # "thinking", "answer", or "" for everything else
        self.soft = soft          # the terminal wrapped the row before into this one
        self.glue = glue          # what joins it to the row before; None is a new line

    @property
    def plain(self) -> str:
        return _ESCAPE.sub("", self.raw)

    def bounds(self, wrapped: bool = False) -> tuple:
        """The cells of this row that are text: (first, one past the last).

        `wrapped` is a row the terminal carried over into the next: it runs to
        its last cell, and a space there is the one between two words.
        """
        plain = self.plain if wrapped else self.plain.rstrip()
        if not plain.strip() or _FRAME.match(plain):
            return 0, 0
        gutter = None if self.soft else _GUTTER.match(plain)
        if gutter:
            if plain.endswith(" │"):          # the far wall of a code frame
                plain = plain[:-2].rstrip()
            return _cells(gutter.group(0)), max(_cells(gutter.group(0)), _cells(plain))
        first = 0 if self.soft else min(_INDENT, len(plain) - len(plain.lstrip(" ")))
        return first, _cells(plain)


def _cells(text: str) -> int:
    return sum(config.cell_width(char) for char in text)


class Ledger:
    """The rows this program has printed, newest last."""

    def __init__(self):
        self.rows: collections.deque = collections.deque(maxlen=ROWS_KEPT)
        self.kind = ""            # what is being printed now, as `block` set it
        self.glue = None
        self.lock = threading.RLock()
        self.on_change = None     # called before the rows move, with the lock held
        self._raw = ""            # the row being written
        self._used = 0            # cells of it
        self._soft = False
        self._sgr = ""            # the colour in force
        self._opened = ""         # ... as it was when this row began
        self._pending = ""        # an escape cut in two by the end of a write

    # -- writing ------------------------------------------------------------

    def feed(self, text: str) -> None:
        """Account for `text` having been written to the terminal."""
        if not text:
            return
        with self.lock:
            text = self._pending + text
            cut = _PARTIAL.search(text)
            self._pending = text[cut.start():] if cut else ""
            if cut:
                text = text[:cut.start()]
            width = max(1, config.tw())
            for piece in _TOKEN.split(text):
                if not piece:
                    continue
                if piece == "\n":
                    self._end_row()
                    self._soft = False
                elif piece == "\r":
                    # Back to the margin to draw over the row: a spinner frame,
                    # a line about to be erased. What was there is gone.
                    self._raw, self._used, self._opened = "", 0, self._sgr
                elif piece == "\t":
                    self._write(" " * (8 - self._used % 8), width)
                elif piece[0] == "\033":
                    self._escape(piece)
                else:
                    self._write(piece, width)

    def _write(self, text: str, width: int) -> None:
        for char in text:
            cells = config.cell_width(char)
            if self._used + cells > width:
                # Where the terminal wraps, and what it wraps into: a row that
                # starts at the margin with nothing in front of it.
                self._end_row()
                self._soft = True
            self._raw += char
            self._used += cells

    def _escape(self, piece: str) -> None:
        final = piece[-1]
        if piece[1] != "[":
            return                            # a title, a hyperlink: nothing drawn
        if final == "m":
            self._sgr = "" if piece in ("\033[m", "\033[0m") else (self._sgr + piece)[-240:]
            self._raw += piece
        elif final == "K":
            if self._used == 0 or piece == "\033[2K":
                self._raw, self._used = "", 0
        elif final == "J" and piece in ("\033[2J", "\033[3J"):
            if self.on_change:
                self.on_change()
            self.rows.clear()
            self._raw, self._used, self._soft = "", 0, False

    def _end_row(self) -> None:
        if self.on_change:
            self.on_change()
        glue = None if self._soft else self.glue
        self.rows.append(Row(self._opened + self._raw, self.kind, self._soft, glue))
        self._raw, self._used, self._opened = "", 0, self._sgr

    # -- reading ------------------------------------------------------------

    def block(self, index: int) -> tuple:
        """The first and last row of the block row `index` is in."""
        kind = self.rows[index].kind
        first = last = index
        while first > 0 and self.rows[first - 1].kind == kind:
            first -= 1
        while last + 1 < len(self.rows) and self.rows[last + 1].kind == kind:
            last += 1
        return first, last

    def select(self, anchor: tuple, head: tuple) -> dict:
        """The cells between two (row, column) points: {row: (first, past)}.

        Kept to the block the drag began in, and on each row to the part that
        is text - so a drag that leaves a thought stops at its last word, and
        one that crosses the gutter does not take the gutter.
        """
        with self.lock:
            if not 0 <= anchor[0] < len(self.rows):
                return {}
            first, last = self.block(anchor[0])
            if head[0] < first:
                head = (first, 0)
            elif head[0] > last:
                head = (last, sys.maxsize)
            (top, left), (bottom, right) = sorted((anchor, head))
            chosen = {}
            for index in range(top, bottom + 1):
                lo, hi = self.rows[index].bounds(
                    index + 1 < len(self.rows) and self.rows[index + 1].soft)
                start = max(lo, self._snap(index, left)) if index == top else lo
                end = min(hi, self._snap(index, right, after=True)) if index == bottom else hi
                if end > start:
                    chosen[index] = (start, end)
            return chosen

    def _snap(self, index: int, column: int, after: bool = False) -> int:
        """`column` moved to the edge of the character it falls in.

        A wide character is two cells and cannot be half selected: a drag
        that starts or ends in the middle of one takes all of it.
        """
        at = 0
        for char in self.rows[index].plain:
            cells = config.cell_width(char)
            if at <= column < at + cells:
                return at + cells if after else at
            at += cells
        return column + 1 if after else column

    def text(self, chosen: dict) -> str:
        """What `chosen` says, as it was before it was laid out in rows."""
        with self.lock:
            out = ""
            for count, index in enumerate(sorted(chosen)):
                row = self.rows[index]
                if count:
                    # A row the terminal wrapped is the same line going on; one
                    # the program wrapped says what it broke on. Rows skipped
                    # between two chosen ones were frames, and end the line.
                    whole = index - 1 in chosen
                    out += ("" if row.soft and whole else
                            row.glue if row.glue is not None and whole else "\n")
                out += _slice(row.plain, *chosen[index])
            return out

    def paint(self, index: int, span=None) -> str:
        """Row `index` as it is drawn, with `span` of it in reverse video."""
        raw = self.rows[index].raw
        return _relight(raw, *span) if span else raw


def _relight(raw: str, lo: int, hi: int) -> str:
    """`raw` with cells `lo` to `hi` in reverse video.

    A colour change inside the span is followed by the reverse again: a reset
    there would end the highlight along with everything else.
    """
    out, at, lit = "", 0, False
    for piece in re.split(f"({_ESCAPE.pattern})", raw):
        if not piece:
            continue
        if _ESCAPE.fullmatch(piece):
            out += piece + (REVERSE if lit else "")
            continue
        for char in piece:
            if lit and at >= hi:
                out, lit = out + PLAIN, False
            if not lit and lo <= at < hi:
                out, lit = out + REVERSE, True
            out += char
            at += config.cell_width(char)
    return out + (PLAIN if lit else "")


def _slice(plain: str, lo: int, hi: int) -> str:
    out, at = "", 0
    for char in plain:
        if lo <= at < hi:
            out += char
        at += config.cell_width(char)
    return out


ledger = Ledger()


@contextlib.contextmanager
def block(kind: str, glue=None):
    """Say what is being printed, for as long as it is being printed.

    `glue` is for a row the program cut out of a longer line itself: what the
    cut took - a space, or nothing - and so what puts the line back together.
    """
    before = ledger.kind, ledger.glue
    ledger.kind, ledger.glue = kind, glue
    try:
        yield
    finally:
        ledger.kind, ledger.glue = before


def typed(prompt: str, answer: str = "") -> None:
    """Account for a line the terminal echoed rather than the program printed.

    `input()` and the prompt write straight to the terminal, past the recorder,
    and what they leave behind is a row like any other.
    """
    ledger.feed(f"{prompt}{answer}\033[0m\n")


def frame(rows_above: int, chosen: dict | None = None) -> str:
    """Draw the rows above the prompt from the ledger, `chosen` highlighted.

    The whole stretch, not just the rows that changed: it is what makes the
    screen agree with the ledger before a selection is drawn over it. The
    cursor and its colour are saved around it, so the prompt underneath never
    learns that anything was written.
    """
    with ledger.lock:
        count = min(rows_above, len(ledger.rows))
        start = len(ledger.rows) - count
        out = ["\0337"]
        for offset in range(count):
            index = start + offset
            out.append(f"\033[{rows_above - count + offset + 1};1H\033[0m\033[2K"
                       + ledger.paint(index, (chosen or {}).get(index)) + "\033[0m")
        out.append("\0338")
        return "".join(out)


# ---------------------------------------------------------------------------
# the recorder
# ---------------------------------------------------------------------------

class Recorder:
    """`sys.stdout`, with everything written through it noted in the ledger."""

    def __init__(self, target):
        self.target = target

    def write(self, text):
        try:
            ledger.feed(text if isinstance(text, str) else str(text))
        except Exception:
            pass                  # the account is a convenience; printing is not
        return self.target.write(text)

    def flush(self):
        return self.target.flush()

    def fileno(self):
        return self.target.fileno()

    def __getattr__(self, name):
        return getattr(self.target, name)


def _layers(stream):
    while stream is not None:
        yield stream
        stream = getattr(stream, "__dict__", {}).get("target")


def install() -> None:
    """Put a recorder on `sys.stdout` unless one is there.

    Called at the top of every prompt as well as at start, for the reason the
    remote's mirror is: `patch_stdout` swaps `sys.stdout` for a proxy of its
    own while a prompt is open, and what is printed above the prompt goes
    through that.
    """
    if not any(isinstance(layer, Recorder) for layer in _layers(sys.stdout)):
        sys.stdout = Recorder(sys.stdout)


def beneath(stream):
    """`stream` without the recorder: where the prompt itself draws.

    What prompt_toolkit writes is cursor movement around its own rows, not
    lines of the transcript; what it leaves behind is accounted for with
    `typed` once it is done.
    """
    while isinstance(stream, Recorder):
        stream = stream.target
    return stream


# ---------------------------------------------------------------------------
# the clipboard
# ---------------------------------------------------------------------------

def _tools() -> list:
    """The commands that take text for the clipboard here: (argv, encoding)."""
    if sys.platform == "win32":
        return [(["clip"], "utf-16le")]
    if sys.platform == "darwin":
        return [(["pbcopy"], "utf-8")]
    found = []
    if shutil.which("clip.exe"):
        # WSL: the Windows clipboard, asked directly rather than through an
        # X server that may or may not be passing its own along.
        found.append((["clip.exe"], "utf-16le"))
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        found.append((["wl-copy"], "utf-8"))
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            found.append((["xclip", "-selection", "clipboard"], "utf-8"))
        if shutil.which("xsel"):
            found.append((["xsel", "--clipboard", "--input"], "utf-8"))
    return found


_ASK_WINDOWS = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                "Get-Clipboard -Raw")


def _readers() -> list:
    """The commands that print what the clipboard holds, as UTF-8."""
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    asked = [[powershell, "-NoProfile", "-NonInteractive", "-Command", _ASK_WINDOWS]]
    if sys.platform == "win32":
        return asked if powershell else []
    if sys.platform == "darwin":
        return [["pbpaste"]]
    found = asked if powershell else []       # WSL, again
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        found.append(["wl-paste", "--no-newline"])
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            found.append(["xclip", "-selection", "clipboard", "-o"])
        if shutil.which("xsel"):
            found.append(["xsel", "--clipboard", "--output"])
    return found


def clipboard() -> str:
    """What is on the clipboard, as text. "" when nothing here can read it.

    A terminal pastes on a right click, and stops when a program takes the
    mouse - the click is reported instead. So the program does the pasting.
    """
    for argv in _readers():
        try:
            done = subprocess.run(argv, timeout=5, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    return ""


def osc52(text: str) -> str:
    """The escape that asks the terminal itself to take `text`. "" if too long."""
    data = text.encode("utf-8")
    if len(data) > OSC52_LIMIT:
        return ""
    return f"\033]52;c;{base64.b64encode(data).decode('ascii')}\a"


def copy(text: str) -> bool:
    """Put `text` on the clipboard with the platform's own tool. False if none did.

    The caller sends `osc52` as well: over SSH the tools here would fill the
    clipboard of the wrong machine, and the terminal is the one thing that is
    always on the right one.
    """
    for argv, encoding in _tools():
        try:
            done = subprocess.run(argv, input=text.encode(encoding), timeout=3,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return True
    return False
