"""The last turn as it really was, for Ctrl+O.

What is printed during a turn is cut to fit: an argument to 120 characters, a
tool result to 600, and a model's thinking not at all unless `SHOW_THINKING`
is on. That is right while it scrolls past and wrong the moment the cut part
is the part that matters - the command that failed, the file that was written,
the reasoning behind a strange call. The conversation history does not have it
either: thinking is never kept there.

So the turn is recorded here as it happens, whole, and Ctrl+O at the prompt
prints it. Only the last turn: it is what Ctrl+O is pressed about, and keeping
every turn whole would hold every file the model ever wrote in memory twice.
"""
from aetheris import config
from aetheris.config import S

_entries: list = []          # (kind, name, body), in the order they happened


def start() -> None:
    """A new turn: what the last one did is no longer what Ctrl+O is about."""
    _entries.clear()


def thought(text: str) -> None:
    if text.strip():
        _entries.append(("thinking", "", text.strip("\n")))


def said(text: str) -> None:
    if text.strip():
        _entries.append(("answer", "", text.strip("\n")))


def call(name: str, arguments: dict) -> None:
    _entries.append(("call", name, dict(arguments or {})))


def result(name: str, text: str) -> None:
    _entries.append(("result", name, str(text)))


def entries() -> list:
    return list(_entries)


def show() -> None:
    """Print the last turn uncut."""
    if not _entries:
        print(f"\n  {S.MUTED}Ctrl+O: nothing to show yet - it shows the last "
              f"turn in full once there is one.{S.R}\n")
        return
    width = max(20, config.tw() - 6)
    print(f"\n  {S.PURPLE}◆ the last turn, uncut{S.R}")
    print(f"  {config._hr(width=min(60, width))}")
    for kind, name, body in _entries:
        if kind == "thinking":
            print(f"  {S.MUTED}╭─ thinking{S.R}")
            for line in body.split("\n"):
                for row in config.wrap_cells(line, width - 2):
                    print(f"  {S.MUTED}│ {row}{S.R}")
            print(f"  {S.MUTED}╰─{S.R}")
        elif kind == "answer":
            for line in body.split("\n"):
                for row in config.wrap_cells(line, width):
                    print(f"  {row}")
        elif kind == "call":
            print(f"\n  {S.INFO}▸{S.R} {S.BOLD}{name}{S.R}")
            for key, value in body.items():
                text = str(value)
                print(f"  {S.MUTED}├─{S.R} {S.GRAY}{key}:{S.R}"
                      + (f" {text}" if "\n" not in text else ""))
                if "\n" in text:
                    for line in text.split("\n"):
                        for row in config.wrap_cells(line, width - 3):
                            print(f"  {S.MUTED}│{S.R}    {row}")
        else:
            print(f"  {S.MUTED}│{S.R}")
            for line in body.split("\n"):
                for row in config.wrap_cells(line, width):
                    print(f"  {S.MUTED}│{S.R}  {row}")
            print(f"  {S.MUTED}╰─ {len(body):,} chars{S.R}")
    print(f"  {config._hr(width=min(60, width))}\n")


if __name__ == "__main__":
    print("This file can not run directly.")
