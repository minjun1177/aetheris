"""Three things a person could not change without editing source.

`config.py` was the only way to change `NUM_CTX`, an API key could be typed in
and never taken back out, and the banner assumed a terminal tall enough to
print it into. Each of those is somebody having to work around the program
rather than use it.

What is checked here is the part that would break quietly: that the settings
file only ever records what was actually changed, that a broken one still
lets the harness start, that deleting a key deletes the key and nothing else,
and that the welcome screen fits the terminal it is printed into.
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".settings-test-home")
shutil.rmtree(HOME, ignore_errors=True)
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import connect, providers, tui      # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def saved_file():
    try:
        with open(config.SETTINGS_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError:
        return None


# ---------------------------------------------------------------------------
print("--- what counts as a setting is derived, not listed ---")
settable = config.settable()
check("the real settings are in it",
      {"NUM_CTX", "CMD_TIMEOUT", "VM_TIMEOUT", "SHOW_THINKING"} <= set(settable),
      str(sorted(set(("NUM_CTX", "CMD_TIMEOUT", "VM_TIMEOUT")) - set(settable))))
# A tool result marker is a protocol anchor that three modules test with
# `startswith` (5.9); a session title is live state. Neither is a preference,
# and offering to change them would be offering to break something.
check("but live state and protocol anchors are not",
      not ({"SYSTEM_PROMPT", "SESSION_TITLE", "MODEL", "TOOL_ERROR_PREFIX",
            "TOOL_REFUSAL_PREFIX", "SETTINGS_FILE", "CURRENT_OS"} & set(settable)),
      str(sorted({"SYSTEM_PROMPT", "SESSION_TITLE", "MODEL", "TOOL_ERROR_PREFIX",
                  "SETTINGS_FILE", "CURRENT_OS"} & set(settable))))
check("and nothing unserialisable is",
      all(isinstance(v, (bool, int, float, str, list)) for v in settable.values()))

print("\n--- what was typed becomes the type the setting already has ---")
cases = [
    ("NUM_CTX", "32768", 32768),
    ("SHOW_THINKING", "on", True),
    ("SHOW_THINKING", "false", False),
    ("SHOW_THINKING", "1", True),
    ("CMD_IDLE_TIMEOUT", "1.5", 1.5),
    ("MCP_TRUSTED_SERVERS", "github, filesystem", ["github", "filesystem"]),
    ("SEARXNG_URL", "http://localhost:8888", "http://localhost:8888"),
]
for name, raw, expected in cases:
    value, problem = config.parse_setting(name, raw)
    check(f"{name} = {raw!r}", value == expected and not problem, f"got {value!r} {problem}")

for name, raw, why in [
    ("NUM_CTX", "lots", "not a number"),
    ("SHOW_THINKING", "maybe", "not on or off"),
    ("CMD_TIMEOUT", "-5", "negative"),
]:
    value, problem = config.parse_setting(name, raw)
    check(f"{name} = {raw!r} is refused ({why})", value is None and bool(problem), problem)

print("\n--- the file records the deviations and nothing else ---")
ok, where = config.set_setting("num_ctx", "32768")
check("a setting can be changed by name in any case", ok and config.NUM_CTX == 32768,
      f"{where} {config.NUM_CTX}")
check("and it is written down", saved_file() == {"NUM_CTX": 32768}, str(saved_file()))
config.set_setting("VM_TIMEOUT", "60")
check("along with the next one",
      saved_file() == {"NUM_CTX": 32768, "VM_TIMEOUT": 60}, str(saved_file()))

# Writing every setting out would freeze this version's values forever: a
# default improved in a later release would never reach anyone who had once
# used /set. Only what was actually changed is recorded.
check("but the other fifty are not",
      len(saved_file()) == 2 and len(config.settable()) > 40,
      f"{len(saved_file())} recorded, {len(config.settable())} settings")

ok, _ = config.set_setting("NUM_CTX", "default")
check("'default' puts it back", ok and config.NUM_CTX == config.defaults()["NUM_CTX"],
      str(config.NUM_CTX))
check("and stops recording it", saved_file() == {"VM_TIMEOUT": 60}, str(saved_file()))

ok, message = config.set_setting("NO_SUCH_SETTING", "1")
check("an unknown name is refused, not created", not ok and "not a setting" in message,
      message)
check("and nothing was written for it", "NO_SUCH_SETTING" not in (saved_file() or {}))

print("\n--- and it is read back at import, in a new process ---")


def in_subprocess(code: str) -> str:
    environment = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    environment[paths.ENV_VAR] = HOME
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=environment, cwd=ROOT).stdout.strip()

check("a saved setting survives a restart",
      in_subprocess("from aetheris import config; print(config.VM_TIMEOUT)") == "60")
check("and the one put back to its default does not",
      in_subprocess("from aetheris import config; print(config.NUM_CTX)")
      == str(config.defaults()["NUM_CTX"]))

# The harness has to start when its settings file is broken. A file that will
# not parse is one bad afternoon; a harness that will not open is a worse one.
with open(config.SETTINGS_FILE, "w", encoding="utf-8") as handle:
    handle.write("{ this is not json")
check("a settings file that will not parse leaves every default alone",
      in_subprocess("from aetheris import config; "
                    "print(config.VM_TIMEOUT, config.SETTINGS_APPLIED)") == "20 []")

with open(config.SETTINGS_FILE, "w", encoding="utf-8") as handle:
    json.dump({"VM_TIMEOUT": 45, "GONE_IN_THIS_VERSION": 1, "NUM_CTX": "lots"}, handle)
check("an entry this version cannot use is skipped, and the rest applied",
      in_subprocess("from aetheris import config; "
                    "print(config.VM_TIMEOUT, config.NUM_CTX)")
      == f"45 {config.defaults()['NUM_CTX']}")

os.remove(config.SETTINGS_FILE)

# ---------------------------------------------------------------------------
print("\n--- an API key can be taken back out ---")
providers.connect("anthropic", model="claude-x", api_key="sk-test-key")
check("a key can be saved", providers.settings_for("anthropic").get("api_key")
      == "sk-test-key")
removed, where = providers.forget_key("anthropic")
check("and deleted", removed and where == providers.CONFIG_PATH, str(where))
check("it is gone from the live settings",
      "api_key" not in providers.settings_for("anthropic"))
with open(providers.CONFIG_PATH, encoding="utf-8") as handle:
    on_disk = handle.read()
check("and gone from the file", "sk-test-key" not in on_disk, on_disk)
# The model beside it is not a secret, and keeping it is what makes connecting
# again one step instead of three.
check("but the model it was connected to is kept",
      providers.settings_for("anthropic").get("model") == "claude-x")
check("the provider now says it has no key",
      "no API key" in (providers.build("anthropic").ready() or ""),
      providers.build("anthropic").ready())
check("deleting it twice is not an error",
      providers.forget_key("anthropic") == (False, ""))
check("and an unknown provider says so",
      "unknown provider" in providers.forget_key("nope")[1])

# ---------------------------------------------------------------------------
print("\n--- the banner fits the terminal it is printed into ---")
import io                                                          # noqa: E402
import re                                                          # noqa: E402


def welcome_rows(columns, lines):
    os.environ["COLUMNS"], os.environ["LINES"] = str(columns), str(lines)
    buffer, real = io.StringIO(), sys.stdout
    sys.stdout = buffer
    try:
        tui._welcome()
    finally:
        sys.stdout = real
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buffer.getvalue())
    return len(text.splitlines()), text


for columns, lines in ((100, 40), (100, 30), (100, 24), (100, 20), (50, 24)):
    rows, text = welcome_rows(columns, lines)
    # +1 for the prompt the person then types at. The art is at the top, so
    # anything that does not fit takes the art with it.
    check(f"{columns}x{lines}: {rows} rows + the prompt", rows + 1 <= lines,
          f"needs {rows + 1}")

rows, text = welcome_rows(100, 40)
check("the wide face is used when there is room for it", "███" in text)
rows, text = welcome_rows(100, 20)
check("and the short one when there is not", "███" not in text and "╔═╗" in text)
check("with exactly one blank line under the rule",
      text.rstrip("\n").endswith("─") and text.endswith("\n\n"),
      repr(text[-30:]))

shutil.rmtree(HOME, ignore_errors=True)

# ---------------------------------------------------------------------------
print("\n--- /set groups by subject, and says when it has stopped ---")
# The leftovers used to print with no heading of their own, straight under the
# last group's rows - which put a dozen settings that have nothing to do with
# the scratch VM under `vm`, with the heading above them the only thing saying
# otherwise. And a bare name counted as a loner even when its own family was
# right beside it, so `DEEPTHINK` and `DEEPTHINK_MAX_PASSES` were two groups.
import contextlib                                                   # noqa: E402
import io                                                           # noqa: E402
import re                                                           # noqa: E402
from aetheris import tui                                      # noqa: E402

buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    tui._show_settings()
plain = re.sub(r"\x1b\[[0-9;]*m", "", buffer.getvalue())

group, under = "", {}
for line in plain.splitlines():
    heading = re.match(r"^  ([a-z][a-z ]*)$", line)
    row = re.match(r"^  [ \u2022] ([A-Z][A-Z0-9_]*)\s", line)
    if heading:
        group = heading.group(1).strip()
    elif row:
        under[row.group(1)] = group

check("every setting is listed", len(under) > 40, f"{len(under)} rows")
in_vm = sorted(name for name, where in under.items() if where == "vm")
check("the vm group holds only vm settings",
      bool(in_vm) and all(name.startswith("VM_") for name in in_vm), str(in_vm))
homeless = sorted(name for name, where in under.items() if not where)
check("nothing is left under no heading at all", not homeless, str(homeless))
check("the leftovers are named rather than left looking like the group above",
      "everything else" in under.values())
check("a bare name groups with its own family",
      under.get("DEEPTHINK") == "deepthink"
      and under.get("DEEPTHINK_MAX_PASSES") == "deepthink",
      f"{under.get('DEEPTHINK')} / {under.get('DEEPTHINK_MAX_PASSES')}")
check("and where the Ollama daemon lives is a setting like any other",
      "OLLAMA_HOST" in under)


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("settings checks passed")
