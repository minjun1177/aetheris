"""A long line inside a gutter keeps the gutter on every row.

A thought is drawn as `│ text`, a tool result as `│  text`, a quoted line as
`┃ text`. Left to the terminal, a line longer than the window wraps by itself
and the rows it runs on to start at column 0 - outside the mark that says what
they are. So the line is cut to the window first, and each row is drawn with
its own mark:

- **Cut by cells, not characters.** Hangul and CJK take two; colour codes take
  none; a combining mark rides on the character before it.
- **At a space where there is one** for prose, and exactly at the edge for a
  command's output, which has columns worth keeping.
- **Nothing is lost.** Put back together, the rows are the line.
"""
import asyncio
import contextlib
import io
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="wrap-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import llm_client, providers, renderer, tui   # noqa: E402

failures = []
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra and not ok else ''}")


def cells(text):
    return sum(config.cell_width(c) for c in ANSI.sub("", text))


def drawn(function, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        function(*args)
    # What is left on a row once the spinner that was drawn over is gone.
    return [ANSI.sub("", line).rsplit("\r", 1)[-1] for line in out.getvalue().split("\n")]


WIDTH = 40
config.tw = lambda: WIDTH
tui.tw = renderer.tw = config.tw

# ---------------------------------------------------------------------------
print("\n--- cutting a line to the window ---")
prose = "the quick brown fox jumps over the lazy dog and keeps on running"
rows = config.wrap_cells(prose, 20, by_word=True)
check("no row is wider than asked", all(cells(r) <= 20 for r in rows), str(rows))
check("prose breaks at its spaces", " ".join(rows) == prose, str(rows))
rows = config.wrap_cells("x" * 45, 20)
check("output breaks at the edge", [len(r) for r in rows] == [20, 20, 5], str(rows))
check("and nothing is lost", "".join(rows) == "x" * 45)
korean = "한글은 한 글자가 두 칸을 차지합니다"
rows = config.wrap_cells(korean, 11, by_word=True)
check("a wide character counts as two cells", all(cells(r) <= 11 for r in rows), str(rows))
check("so eleven cells hold five of them, not eleven",
      config.wrap_cells("가" * 11, 11) == ["가" * 5, "가" * 5, "가"])
coloured = f"{config.S.ERR}red and long enough to wrap{config.S.R} plain"
rows = config.wrap_cells(coloured, 10, by_word=True)
check("a colour code takes no room", all(cells(r) <= 10 for r in rows), str(rows))
check("and is not cut in half", ANSI.sub("", " ".join(rows)) == "red and long enough to wrap plain"
      and "".join(rows).count("\x1b") == coloured.count("\x1b"), str(rows))
check("a word longer than the row is cut rather than run over",
      all(cells(r) <= 6 for r in config.wrap_cells("supercalifragilistic", 6, by_word=True)))
check("a short line is one row", config.wrap_cells("short", 20) == ["short"])
check("and an empty one is one empty row", config.wrap_cells("", 20) == [""])
check("a combining mark takes no cell of its own", cells("é") == 1)
check("a width of nothing does not loop", config.wrap_cells("abc", 0) == ["a", "b", "c"])

# ---------------------------------------------------------------------------
print("\n--- a thought keeps its rail on every row ---")
THOUGHT = ("First I should read the file the person named and then compare it "
           "with the test that fails, 그리고 한글이 섞인 긴 줄도 같은 방식으로 접혀야 한다.")


class Thinker:
    name, model, label, drives_turns = "fake", "fake", "Fake", False

    def stream(self, messages, tools=None):
        async def chunks():
            yield {"thinking": THOUGHT + "\n"}
            yield {"thinking": "a short second thought"}
            yield {"text": "The answer.\n"}
            yield {"done": True}
        return chunks()


providers.current = lambda: Thinker()
config.SHOW_THINKING = True
lines = drawn(lambda: asyncio.run(llm_client.stream_reply([{"role": "user", "content": "q"}])))
thought = lines[lines.index("  ╭─ thinking") + 1:lines.index("  ╰─")]
check("the long thought ran on to several rows", len(thought) > 3, str(thought))
check("and every one of them starts inside the box",
      all(row.startswith("  │ ") for row in thought), str(thought))
check("none is wider than the window", all(cells(row) <= WIDTH for row in thought),
      str([cells(row) for row in thought]))
check("with all of the thought still there",
      " ".join(row[4:] for row in thought) == THOUGHT + " a short second thought",
      " ".join(row[4:] for row in thought))

# ---------------------------------------------------------------------------
print("\n--- so does a tool's result, and the call above it ---")
long_line = "/home/someone/project/src/" + "nested/" * 6 + "module.py: line 12: something"
lines = drawn(tui._fmt_tool_result, "run_cmd", f"{long_line}\nshort")
body = [row for row in lines if row.strip() and not row.startswith("  ╰")]
check("every row of the result has the rail", all(row.startswith("  │") for row in body),
      str(body))
check("none is wider than the window", all(cells(row) <= WIDTH for row in body))
check("and the long line is all there",
      "".join(row[5:] for row in body if row.startswith("  │  ")).startswith(long_line))
lines = drawn(tui._fmt_tool_call, "write_file",
              {"filepath": "a/" * 30 + "b.py", "content": "one\ntwo"})
rows = [row for row in lines if row.strip()][1:]
check("a long argument runs on under its own name", len(rows) > 2
      and all(row.startswith(("  ├─", "  │", "  ╰─", "   ")) for row in rows), str(rows))
check("none of it wider than the window", all(cells(row) <= WIDTH for row in rows),
      str([cells(row) for row in rows]))
check("and a newline in an argument does not start a row outside the tree",
      "one two" in rows[-1], rows[-1])

# ---------------------------------------------------------------------------
print("\n--- and a quoted line ---")
quote = "> " + "a quotation that goes on for rather longer than the window is wide " * 2
rendered, _, _ = renderer._render_line(quote.strip(), False)
rows = [ANSI.sub("", row) for row in rendered.split("\n")]
check("every row of the quotation is marked", len(rows) > 1
      and all(row.startswith("  ┃ ") for row in rows), str(rows))
check("none is wider than the window", all(cells(row) <= WIDTH for row in rows))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    sys.exit(1)
print("all wrap checks passed")
