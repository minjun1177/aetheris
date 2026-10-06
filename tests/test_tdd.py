"""`/tdd`: the test files are locked for one request, and then they are not.

"Make this test pass" is a request models answer by editing the test - loosening
the assertion until it is true, or deleting it. Asking them not to does not
work, for the same reason it does not work in a planning stage: a small model
that is stuck takes the opening it is given. So the opening is closed at the
dispatcher, through the permission rules that already sit there.

What is checked here is the part that would be quietly wrong: that the patterns
reach a test file however its path is written and leave the code under test
alone, that the refusal really comes from `dispatch_tool` rather than from the
handler, and - the one that matters most - that the lock *lifts*. A lock nobody
remembers turning on is worse than no lock, so it is armed for one turn and
released in a `finally`, including when that turn raised.
"""
import inspect
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from aetheris import paths

HOME = tempfile.mkdtemp(prefix="tdd-home-")
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = False
config.CHANNEL_ENABLED = False
config.AUTO_VERIFY = False

from aetheris import app, llm_client, permissions, tools, verify   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


WORK = tempfile.mkdtemp(prefix="tdd-work-")


def verdict(tool, path):
    return permissions.decide(tool, {"filepath": path})[0]


# ---------------------------------------------------------------------------
print("--- armed, the patterns reach a test file however it is written ---")
app._arm_tdd()

LOCKED = ("test_parser.py", "tests/test_parser.py", "a/b/tests/test_parser.py",
          "/home/me/proj/test_parser.py", "parser_test.py", "conftest.py",
          "parser_test.go", "widget.test.ts", "widget.spec.js",
          "src/components/Widget.test.tsx")
missed = [p for p in LOCKED if verdict("edit_file", p) != "deny"]
check("every shape of test path is denied", not missed, str(missed))

OPEN = ("parser.py", "src/parser.py", "/home/me/proj/src/parser.py",
        "README.md", "main.go", "widget.ts", "package.json")
caught = [p for p in OPEN if verdict("edit_file", p) != "ask"]
check("and nothing else is", not caught, str(caught))

check("write_file is locked too", verdict("write_file", "test_parser.py") == "deny")
check("so are delete_file and copy_file",
      permissions.decide("delete_file", {"filepath": "test_parser.py"})[0] == "deny"
      and permissions.decide("copy_file", {"dst": "test_parser.py"})[0] == "deny")

# ---------------------------------------------------------------------------
print("\n--- the refusal happens in dispatch, before any handler runs ---")
test_file = os.path.join(WORK, "test_thing.py")
with open(test_file, "w", encoding="utf-8") as f:
    f.write("def test_thing():\n    assert add(1, 2) == 3\n")
before = open(test_file, encoding="utf-8").read()

result = tools.dispatch_tool("write_file", {"filepath": test_file, "content": "pass\n"})
check("writing a test file is refused", result.startswith(config.TOOL_REFUSAL_PREFIX),
      result[:60])
check("the file is untouched", open(test_file, encoding="utf-8").read() == before)
check("and the refusal names the rule", "test_" in result, result[:90])

code_file = os.path.join(WORK, "thing.py")
made = tools.dispatch_tool("write_file", {"filepath": code_file,
                                          "content": "def add(a, b):\n    return a + b\n"})
check("the code under test is still writable", made.startswith("[Success"), made)

# ---------------------------------------------------------------------------
print("\n--- it holds nothing on disk ---")
# A lock that outlived the process, or that appeared in somebody's
# .permissions.json, would be a surprise nobody could trace back to a command
# they typed once.
check("the rules are held in memory", len(permissions.held("/tdd")) > 0,
      f"{len(permissions.held('/tdd'))} held")
written = [path for _, path in permissions.config_paths()
           if os.path.exists(path) and "test_" in open(path, encoding="utf-8").read()]
check("and none of them reached a rules file on disk", not written, str(written))

# ---------------------------------------------------------------------------
print("\n--- and it lifts ---")
dropped = permissions.release("/tdd")
config.TDD_LOCK = False
check("releasing drops every rule it added", len(dropped) > 0
      and not permissions.held("/tdd"))
check("the test file is writable again", verdict("edit_file", "test_parser.py") == "ask")
check("through dispatch too",
      tools.dispatch_tool("write_file", {"filepath": test_file,
                                         "content": before}).startswith("[Success"))

# The turn loop releases in a `finally`, so a turn that raised or was
# interrupted does not leave the project locked.
loop = inspect.getsource(app.chat_loop) if hasattr(app, "chat_loop") else \
    inspect.getsource(sys.modules[app.__name__])
check("the release sits in a finally, not on the happy path",
      "finally:" in loop and "permissions.release(_TDD_LABEL)" in loop)

print("\n--- arming twice does not stack ---")
app._arm_tdd()
once = len(permissions.held("/tdd"))
app._arm_tdd()
check("the second arm replaces the first", len(permissions.held("/tdd")) == once,
      f"{once} then {len(permissions.held('/tdd'))}")
permissions.release("/tdd")
config.TDD_LOCK = False

print("\n--- the loop gets a longer budget while it is armed ---")
check("and it is longer than the ordinary one",
      config.TDD_VERIFY_FAILURES > llm_client.MAX_VERIFY_FAILURES,
      f"{config.TDD_VERIFY_FAILURES} vs {llm_client.MAX_VERIFY_FAILURES}")
turn = inspect.getsource(llm_client.chat_turn)
check("the turn loop reads it", "TDD_VERIFY_FAILURES" in turn)
check("TDD_LOCK is live state, not a setting somebody can save",
      "TDD_LOCK" not in config.settable())

shutil.rmtree(HOME, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("tdd checks passed")
