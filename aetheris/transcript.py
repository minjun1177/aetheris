"""The conversation as it really was, for Ctrl+O.

What is printed during a turn is cut to fit: an argument to 120 characters, a
tool result to 600, and a model's thinking not at all unless `SHOW_THINKING`
is on. That is right while it scrolls past and wrong the moment the cut part
is the part that matters - the command that failed, the file that was written,
the reasoning behind a strange call. The conversation history does not have it
either: thinking is never kept there, and compression rewrites the rest.

So each turn is recorded here as it happens, whole, and Ctrl+O at the prompt
is a toggle: it redraws the whole conversation expanded - every thought, every
argument and result in full - and pressed again redraws it collapsed, the way
it scrolled past. Answers are drawn as markdown either way.

Every turn of this conversation, not only the last: `/clear` and loading
another session start the record again (`reset`). It is held to `MAX_CHARS`,
oldest turns first, so a long session that wrote large files is not holding
all of them twice for ever.
"""
import contextlib

from aetheris import config
from aetheris.config import S

MAX_CHARS = 4_000_000

# [{"prompt": str, "entries": [(kind, name, body), ...]}], oldest first.
_turns: list = []
_dropped = 0                 # turns let go of to stay under MAX_CHARS
_paused = 0                  # > 0 while a redraw prints through the recorders
expanded = False             # what Ctrl+O last drew


def reset() -> None:
    """A new conversation: nothing of the last one is what Ctrl+O is about."""
    global _dropped, expanded
    _turns.clear()
    _dropped = 0
    expanded = False


def start(prompt: str = "") -> None:
    """A new turn, begun by `prompt` - the line as it was typed."""
    global expanded
    if _paused:
        return
    _turns.append({"prompt": str(prompt or ""), "entries": []})
    # What is on screen is collapsed again from here on: the turn about to
    # scroll past is drawn the ordinary way.
    expanded = False
    _trim()


@contextlib.contextmanager
def paused():
    """Print through the formatters without recording what they print.

    A redraw - Ctrl+O, a resized window - draws the conversation with the same
    functions that record it live, and recording it again was how one turn
    came to be shown twice, or a cleared conversation came back.
    """
    global _paused
    _paused += 1
    try:
        yield
    finally:
        _paused -= 1


def _add(kind: str, name: str, body) -> None:
    if _paused:
        return
    if not _turns:
        _turns.append({"prompt": "", "entries": []})
    _turns[-1]["entries"].append((kind, name, body))


def thought(text: str) -> None:
    if text.strip():
        _add("thinking", "", text.strip("\n"))


def said(text: str) -> None:
    if text.strip():
        _add("answer", "", text.strip("\n"))


def call(name: str, arguments: dict) -> None:
    _add("call", name, dict(arguments or {}))


def result(name: str, text: str) -> None:
    _add("result", name, str(text))


def shell(command: str, output: str) -> None:
    """A `!command` the person ran: its own turn, with what it printed."""
    start(f"!{command}")
    _add("shell", command, str(output))


def _size(turn: dict) -> int:
    total = len(turn["prompt"])
    for _kind, _name, body in turn["entries"]:
        total += sum(len(str(v)) for v in body.values()) if isinstance(body, dict) \
            else len(str(body))
    return total


def _trim() -> None:
    global _dropped
    total = sum(_size(turn) for turn in _turns)
    while len(_turns) > 1 and total > MAX_CHARS:
        total -= _size(_turns.pop(0))
        _dropped += 1


def entries() -> list:
    """The last turn's entries - what the turn that just ran did."""
    return list(_turns[-1]["entries"]) if _turns else []


def turns() -> list:
    return [{"prompt": t["prompt"], "entries": list(t["entries"])} for t in _turns]


def toggle() -> bool:
    """Flip between expanded and collapsed. Returns the new state."""
    global expanded
    expanded = not expanded
    return expanded


# ---------------------------------------------------------------------------
# drawing it
# ---------------------------------------------------------------------------

def show(full: bool | None = None) -> None:
    """Draw every recorded turn - in full, or as it scrolled past."""
    from aetheris import screen
    full = expanded if full is None else full
    if not _turns:
        print(f"\n  {S.MUTED}Ctrl+O: nothing to show yet - once there is a turn "
              f"it switches between everything in full and the usual view.{S.R}\n")
        return
    if full:
        print(f"  {S.PURPLE}◆ expanded - every thought and tool output in full"
              f"{S.R}  {S.MUTED}Ctrl+O collapses it{S.R}\n")
    if _dropped:
        print(f"  {S.MUTED}… {_dropped} earlier turn{'s' if _dropped != 1 else ''} "
              f"not kept{S.R}\n")
    with paused():
        for turn in _turns:
            if turn["prompt"]:
                with screen.block(""):
                    print(f"  {S.USER_CLR}{S.BOLD}❯{S.R} {turn['prompt']}")
            for kind, name, body in turn["entries"]:
                _draw(kind, name, body, full)
            print()
    if not full:
        print(f"  {S.MUTED}Ctrl+O expands every thought and tool output{S.R}\n")


def _draw(kind: str, name: str, body, full: bool) -> None:
    from aetheris import screen
    from aetheris.renderer import _render_full
    from aetheris import tui
    width = max(20, config.tw() - 6)
    if kind == "thinking":
        if not (full or config.SHOW_THINKING):
            lines = body.count("\n") + 1
            print(f"  {S.MUTED}✻ thought - {lines} line{'s' if lines != 1 else ''} "
                  f"· Ctrl+O shows it{S.R}")
            return
        print(f"  {S.MUTED}╭─ thinking{S.R}")
        took: list = []
        with screen.block("thinking"):
            for line in body.split("\n"):
                took.clear()
                rows = config.wrap_cells(line, width - 2, by_word=True, joins=took)
                for index, row in enumerate(rows):
                    with screen.block("thinking", took[index - 1] if index else None):
                        print(f"  {S.MUTED}│ {row}{S.R}")
        print(f"  {S.MUTED}╰─{S.R}")
    elif kind == "answer":
        with screen.block("answer"):
            print(_render_full(body))
    elif kind == "call":
        if not full:
            tui._fmt_tool_call(name, body)
            return
        print(f"\n  {S.INFO}▸{S.R} {S.BOLD}{name}{S.R}")
        for key, value in body.items():
            text = str(value)
            print(f"  {S.MUTED}├─{S.R} {S.GRAY}{key}:{S.R}"
                  + (f" {text}" if "\n" not in text else ""))
            if "\n" in text:
                for line in text.split("\n"):
                    for row in config.wrap_cells(line, width - 3):
                        print(f"  {S.MUTED}│{S.R}    {row}")
    elif kind == "shell" and not full:
        print(f"\n  {S.MUTED}${S.R} {S.WHITE}{name}{S.R}")
        tui._fmt_tool_result(name, body)
    elif kind == "result" and not full:
        tui._fmt_tool_result(name, body)
    else:
        if kind == "shell":
            print(f"\n  {S.MUTED}${S.R} {S.WHITE}{name}{S.R}")
        print(f"  {S.MUTED}│{S.R}")
        for line in str(body).split("\n"):
            for row in config.wrap_cells(line, width):
                print(f"  {S.MUTED}│{S.R}  {row}")
        print(f"  {S.MUTED}╰─ {len(str(body)):,} chars{S.R}")


if __name__ == "__main__":
    print("This file can not run directly.")
