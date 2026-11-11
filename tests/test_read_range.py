"""`read_file` with `start_line` and `end_line`, and a long file cut between lines.

A file too large to hand over whole used to be cut at a character count: part
way through a line, with that half line given the anchor of the half, and no
way at all to read what came after the cut. What is checked: a range comes
back under its real line numbers and anchors, an edit made from those anchors
lands, a cut falls between lines and says where to read on, and nonsense
ranges are refused in words the model can act on.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="range-home-")

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.AUTO_ALLOW = True

from aetheris import systemprompt, toolspec, tools   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


folder = tempfile.mkdtemp(prefix="range-")
path = os.path.join(folder, "big.py")
with open(path, "w", encoding="utf-8") as handle:
    handle.write("\n".join(f"line_{n} = {n}" for n in range(1, 101)) + "\n")

print("--- a range ---")
whole = tools.handle_read_file(path)
part = tools.handle_read_file(path, 40, 42)
rows = part.split("\n")
check("only those lines, under their real numbers",
      [r.split(":")[0] for r in rows[:3]] == ["40", "41", "42"], part)
check("with the same anchors the whole file gives them",
      rows[:3] == whole.split("\n")[39:42])
check("and says which part of how much it is", rows[-1] == "[lines 40-42 of 101]", rows[-1])
check("start_line alone reads to the end",
      tools.handle_read_file(path, 99).split("\n")[0].startswith("99:"))
check("end_line alone reads from the top",
      tools.handle_read_file(path, 0, 2).split("\n")[-1] == "[lines 1-2 of 101]")
check("the whole file is exactly what it was", "[lines" not in whole
      and whole.split("\n")[0].startswith("1:"))
check("numbers that came as strings are read as numbers",
      tools.handle_read_file(path, "40", "40").split("\n")[0].startswith("40:"))
spec = toolspec.get("read_file")
counted = tools.handle_read_file(*spec.bind({"filepath": path, "offset": 40, "limit": 3}))
check("offset and limit - a count of lines - read that many from there",
      counted.split("\n")[-1] == "[lines 40-42 of 101]", counted.split("\n")[-1])
check("limit alone counts from the top",
      tools.handle_read_file(path, 0, 0, 2).split("\n")[-1] == "[lines 1-2 of 101]")
check("an end_line given beats a limit",
      tools.handle_read_file(path, 40, 41, 50).split("\n")[-1] == "[lines 40-41 of 101]")
check("limit stays out of what the model is told",
      "limit" not in spec.schema()["parameters"])
check("an end past the last line stops at the last line",
      tools.handle_read_file(path, 100, 500).split("\n")[-1] == "[lines 100-101 of 101]")

anchor = rows[1].split("|")[0]
tools.handle_edit_file(path, "", f"{anchor}|line_41 = 'changed'")
with open(path, encoding="utf-8") as handle:
    check("an edit from a range's anchor lands on that line",
          handle.read().split("\n")[40] == "line_41 = 'changed'")

print("\n--- refused, with the reason ---")
check("a start past the end", "past the end" in tools.handle_read_file(path, 500))
check("an end before the start", "comes before" in tools.handle_read_file(path, 50, 10))
check("a start that is not a number",
      "line numbers" in tools.handle_read_file(path, "forty"))

print("\n--- a cut for length ---")
config.RETURN_ALL_FILE_CONTENT, kept = False, config.RETURN_ALL_FILE_CONTENT
config.FILE_MAX_DISPLAY_LENGTH, kept_length = 200, config.FILE_MAX_DISPLAY_LENGTH
try:
    cut = tools.handle_read_file(path).split("\n")
finally:
    config.RETURN_ALL_FILE_CONTENT, config.FILE_MAX_DISPLAY_LENGTH = kept, kept_length
shown = cut[:-1]
check("falls between lines - every row is a whole one",
      all(r == w for r, w in zip(shown, whole.split("\n"))), str(shown[-1:]))
check("and says where to read on",
      f"start_line={len(shown) + 1}" in cut[-1] and f"line {len(shown)} of 101" in cut[-1],
      cut[-1])

print("\n--- what the model is told ---")
spec = toolspec.get("read_file")
check("the tool takes the range", [p.name for p in spec.params]
      == ["filepath", "start_line", "end_line", "limit"])
check("and tells of start_line and end_line only",
      list(spec.schema()["parameters"]) == ["filepath", "start_line", "end_line"])
check("both optional, so a call with a path alone is still whole",
      not any(p.required for p in spec.params[1:]))
prompt = systemprompt.systemprompt()
check("the prompt says to use them for a very large file",
      "`start_line` and `end_line`" in prompt)
check("and lets calls that do not depend on each other share a reply",
      "can go together in one reply" in prompt and "one at a time and wait" not in prompt)


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("read range checks passed")
