"""multi_edit: several edits, in one file or several, all of them or none.

`edit_file` changes one place. A rename that touches a function and its callers
took one call, one approval and one commit per place, and a refusal half-way
left half a rename on disk. What is checked here:

* the SEARCH/REPLACE blocks are read, path lines and all, and a malformed one
  is refused with the shape that was wanted;
* two edits to one file that change its line count both land, because each
  anchor is resolved against the file as `read_file` gave it;
* edits across several files land together, approved once;
* one edit that cannot be made means nothing is written - to any file;
* the gates see every file: a deny rule on one refuses the batch, an agent's
  claim on one refuses it, and what is committed and noted is every file;
* the text protocol carries it in an `<edits>` raw block.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="multi-edit-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.AUTO_ALLOW = True
config.SAVE_CHAT_HISTORY = False

from aetheris import channel, llm_client, permissions, tools, toolspec   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra and not ok else ''}")


HOME = tempfile.mkdtemp(prefix="multi-edit-")


def write(name, text):
    path = os.path.join(HOME, name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def anchor(path, number):
    line = read(path).split("\n")[number - 1]
    return f"{number}:{tools._line_hash(line)}"


def block(path, old, new):
    return f"{path}\n<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE\n"


# ---------------------------------------------------------------------------
print("--- the blocks are read ---")
edits, problem = tools._parse_edits(
    "```\n/a/one.py\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
    "<<<<<<< SEARCH\n2:abc\n=======\n\n>>>>>>> REPLACE\n"
    "**`/a/two.py`**\n<<<<<<< SEARCH\n=======\n3:def|x\n>>>>>>> REPLACE\n```")
check("three blocks, with their files", not problem and [e[0] for e in edits]
      == ["/a/one.py", "/a/one.py", "/a/two.py"], str(edits) + problem)
check("a block with no path line of its own belongs to the file before it",
      edits and edits[1] == ("/a/one.py", "2:abc", ""))
check("the path is taken out of the markdown a model wraps it in",
      edits and edits[2][0] == "/a/two.py")
check("the distinct files, in order", tools.edit_paths(
    block("/a/x.py", "a", "b") + block("/a/y.py", "c", "d") + block("/a/x.py", "e", "f"))
      == ["/a/x.py", "/a/y.py"])
for bad, why in (("<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE", "no file path"),
                 ("/a.py\n<<<<<<< SEARCH\nx\n=======\ny", "not closed"),
                 ("just some text", "no <<<<<<< SEARCH")):
    _, problem = tools._parse_edits(bad)
    check(f"refused: {why}", why in problem, problem)
result = tools.handle_multi_edit("nothing to see")
check("and the refusal shows the shape wanted",
      result.startswith("[Error] Nothing was written") and "<<<<<<< SEARCH" in result)

# ---------------------------------------------------------------------------
print("\n--- two places in one file, both changing its length ---")
code = write("calc.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n"
             "    return a - b\n")
result = tools.handle_multi_edit(
    block(code, anchor(code, 1) + "-" + anchor(code, 2),
          "def add(a, b):\n    \"\"\"Sum.\"\"\"\n    total = a + b\n    return total")
    + block(code, anchor(code, 5) + "-" + anchor(code, 6),
            "def sub(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b"))
check("it says it made both", result.startswith("[Success] Edited 1 file")
      and "2 edits" in result, result[:200])
check("and both are in the file, the second at the line it named before the "
      "first moved it",
      read(code) == "def add(a, b):\n    \"\"\"Sum.\"\"\"\n    total = a + b\n    return total"
      "\n\n\ndef sub(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
      repr(read(code)))
check("what came back is the file as it now reads", "def mul(a, b):" in result)

# ---------------------------------------------------------------------------
print("\n--- several files, together ---")
lib = write("lib.py", "def total(items):\n    return sum(items)\n")
use = write("use.py", "from lib import total\nprint(total([1, 2]))\n")
result = tools.handle_multi_edit(
    block(lib, "def total(items):", "def total(items, tax=0):")
    + block(lib, "    return sum(items)", "    return sum(items) * (1 + tax)")
    + block(use, "", f"{anchor(use, 2)}|print(total([1, 2], tax=0.1))"))
check("both files", result.startswith("[Success] Edited 2 files"), result[:200])
check("every edit landed", read(lib) == "def total(items, tax=0):\n"
      "    return sum(items) * (1 + tax)\n"
      and read(use) == "from lib import total\nprint(total([1, 2], tax=0.1))\n",
      repr(read(lib)) + repr(read(use)))

asked = []
real_prompt = tools._approval_prompt
tools._approval_prompt = lambda label, details, rule="": asked.append((label, details)) or True
saved_auto, config.AUTO_ALLOW = config.AUTO_ALLOW, False
try:
    tools.handle_multi_edit(block(lib, "tax=0", "tax=0.0") + block(use, "tax=0.1", "tax=0.2"))
finally:
    tools._approval_prompt = real_prompt
    config.AUTO_ALLOW = saved_auto
check("asked once for the whole batch, not once per edit",
      len(asked) == 1 and asked[0][0] == "Edit Files", str([a[0] for a in asked]))
check("with every file and the diff in front of the person",
      asked and sum(1 for key, _ in asked[0][1] if key == "path") == 2
      and any(key == "diff" and "+def total(items, tax=0.0):" in value
              for key, value in asked[0][1]), str(asked[0][1] if asked else ""))

# ---------------------------------------------------------------------------
print("\n--- one that cannot be made, and nothing is written ---")
before_lib, before_use = read(lib), read(use)
result = tools.handle_multi_edit(block(lib, "tax=0.0", "tax=1")
                                 + block(use, "this text is not there", "x"))
check("refused, saying which edit and why",
      result.startswith("[Error] Nothing was written. Edit 2 of 2")
      and "use.py" in result and "not found" in result, result[:300])
check("and the edit that could have been made was not", read(lib) == before_lib
      and read(use) == before_use)
result = tools.handle_multi_edit(block(lib, "1:zzz", "x"))
check("a stale anchor is refused as edit_file refuses it", result.startswith("[Error]"))
result = tools.handle_multi_edit(block(os.path.join(HOME, "missing.py"), "a", "b"))
check("a file that is not there is write_file's job", "write_file" in result, result)
tools._approval_prompt = lambda *a, **k: False
saved_auto, config.AUTO_ALLOW = config.AUTO_ALLOW, False
try:
    result = tools.handle_multi_edit(block(lib, "tax=0.0", "tax=1"))
finally:
    tools._approval_prompt = real_prompt
    config.AUTO_ALLOW = saved_auto
check("declined, it is a refusal and nothing changes",
      result.startswith("[System]") and read(lib) == before_lib)
check("and nothing is left staged for the next call", tools._batch is None)

# ---------------------------------------------------------------------------
print("\n--- the gates see every file in it ---")
locked = write("test_calc.py", "assert True\n")
arguments = {"edits": block(lib, "tax=0.0", "tax=1") + block(locked, "True", "False")}
permissions.hold("deny", ["edit_file(*test_*.py)"], "multi-edit-test")
try:
    verdict, rule = tools._decide("multi_edit", arguments)
    result = tools.dispatch_tool("multi_edit", arguments)
finally:
    permissions.release("multi-edit-test")
check("a deny on one file is a deny for the batch - so /tdd's locks hold",
      verdict == "deny" and "test_" in rule, f"{verdict} {rule}")
check("and dispatch refuses it before anything is written",
      result.startswith("[System]") and read(lib) == before_lib
      and read(locked) == "assert True\n", result[:160])

real_holder = channel.holder
channel.holder = lambda path: ({"agent": "agent-2", "path": path}
                               if path.endswith("use.py") else {})
try:
    blocked, conflict = tools._claimed_by_another(
        "multi_edit", {"edits": block(lib, "a", "b") + block(use, "c", "d")})
finally:
    channel.holder = real_holder
check("another agent's claim on one of its files stops it", blocked == use and conflict)
check("what was written is every file, for the commit and the board",
      tools._paths_written("multi_edit", {"edits": block(lib, "a", "b") + block(use, "c", "d")},
                           "[Success] Edited 2 files") == [lib, use])
check("and none of them when it failed",
      tools._paths_written("multi_edit", {"edits": block(lib, "a", "b")}, "[Error] x") == [])
check("it is a tool that changes things, so a planning stage cannot use it",
      "multi_edit" in tools._CHANGES_THINGS)

# ---------------------------------------------------------------------------
print("\n--- over the text protocol, in an <edits> block ---")
reply = ('<tool_call>{"name": "multi_edit", "arguments": {}}\n<edits>\n'
         + block(lib, "tax=0.0", "tax=0.5") + "</edits>\n</tool_call>")
calls = llm_client.parse_tool_calls(reply, quiet=True)
check("the block arrives as the edits argument",
      calls and calls[0][0] == "multi_edit" and "<<<<<<< SEARCH" in calls[0][1].get("edits", ""),
      str(calls))
tool = next(t for t in toolspec.TOOLS if t.name == "multi_edit")
check("which the table says is a raw block", tool.blocks == ("edits",))
native = next(s for s in toolspec.native_schema() if s["name"] == "multi_edit")
check("and over native tools is a plain string",
      native["input_schema"]["properties"]["edits"]["type"] == "string"
      if "input_schema" in native else True, str(native)[:200])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("multi_edit checks passed")
