"""`/copy`: an answer, or a code block from one, as the model wrote it.

A code block is drawn in a frame and wrapped to the window, so the terminal's
own selection takes the `│` down each side and stops at the top of the screen.
What is checked: the answers are found newest first without their calls or
thinking, a block is offered on its own, the text goes to the clipboard tool
and to the terminal both, and a file is written only where nothing is.
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="copy-home-")

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False

from aetheris import app, connect, screen   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


CODE = "def add(a, b):\n    return a + b"
history = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "first"},
    {"role": "assistant", "content": "The first answer."},
    {"role": "user", "content": "write add"},
    {"role": "assistant", "content": '<tool_call>\n{"name": "read_file"}\n</tool_call>'},
    {"role": "user", "content": "[tool result]"},
    {"role": "assistant", "content": f"<think>hmm</think>Here:\n\n```python\n{CODE}\n```\n"
                                     "\nDone."},
]

print("--- the answers ---")
said = app._answers(history)
check("newest first, a reply that was only a call skipped",
      len(said) == 2 and said[1] == "The first answer.", str(said))
check("calls and thinking are not part of an answer",
      "<think>" not in said[0] and "tool_call" not in said[0] and said[0].startswith("Here:"),
      said[0])
check("a code block is found as it was written", app._CODE_BLOCK.findall(said[0]) == [CODE])


def run(arg, pick=None, tool=True, terminal=True):
    """`app._copy`: what it sent, where, and what it said."""
    import builtins
    taken, said, raw = [], [], io.StringIO()
    saved = (screen.copy, screen.osc52, connect._choose, builtins.print, sys.stdout)
    screen.copy = lambda text: taken.append(text) or tool
    screen.osc52 = lambda text: f"<osc:{text}>" if terminal else ""
    connect._choose = lambda title, rows, fmt: (said.append("asked") or pick) \
        if pick is not None else -1
    builtins.print = lambda *a, **k: said.append(" ".join(str(x) for x in a))
    sys.stdout = raw
    try:
        app._copy(history, arg)
    finally:
        screen.copy, screen.osc52, connect._choose, builtins.print, sys.stdout = saved
    return taken, raw.getvalue(), "\n".join(said)


print("\n--- to the clipboard ---")
taken, raw, said = run("", pick=1)
check("an answer with code asks which part, and a block goes alone",
      "asked" in said and taken == [CODE], str(taken))
check("the terminal is asked as well, for when the tool is on the wrong machine",
      raw == f"<osc:{CODE}>", raw)
check("and it says so", "Copied 2 lines" in said, said)
taken, _, _ = run("", pick=0)
check("the whole answer is the first choice",
      taken == [app._answers(history)[0]] and "```" in taken[0], str(taken))
taken, _, said = run("2")
check("/copy 2 is the answer before, asked nothing - it has no code",
      taken == ["The first answer."] and "asked" not in said, str(taken))
taken, _, said = run("")
check("cancelling copies nothing", taken == [], said)
_, _, said = run("2", tool=False)
check("with only the terminal to ask, it says the terminal decides",
      "terminal's clipboard" in said, said)
_, _, said = run("2", tool=False, terminal=False)
check("with neither, it says how to write it out instead",
      "Nothing here can reach" in said and "/copy 2 <file>" in said, said)
_, _, said = run("9")
check("past the oldest answer is an error that says the reach", "1 to 2" in said, said)

print("\n--- to a file ---")
folder = tempfile.mkdtemp(prefix="copy-out-")
target = os.path.join(folder, "add.py")
taken, raw, said = run(f"1 {target}", pick=1)
with open(target, encoding="utf-8") as handle:
    written = handle.read()
check("the block is written with a final newline", written == CODE + "\n", repr(written))
check("and nothing goes to the clipboard", taken == [] and raw == "")
_, _, said = run(f"1 {target}", pick=0)
with open(target, encoding="utf-8") as handle:
    check("an existing file is never written over",
          "already exists" in said and handle.read() == CODE + "\n", said)
plain = os.path.join(folder, "notes.md")
run(f"2 {plain}")
with open(plain, encoding="utf-8") as handle:
    check("a file name alone takes the latest; with n, that one",
          handle.read() == "The first answer.\n")

print("\n--- nothing to copy ---")
saved_history = history[:]
history[:] = [{"role": "system", "content": "s"}]
_, _, said = run("")
history[:] = saved_history
check("before any answer it says so", "no answer to copy" in said, said)


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("copy checks passed")
