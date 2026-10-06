"""State that is not the shape the code assumed, and what it used to cost.

Everything here is a file or a value that reaches the harness from somewhere it
does not control - a session written by another version, a `memory.json` edited
by hand, a number typed at `/set` - and every check is one that raised where it
should have carried on.

The one that matters most is the first. A message whose `content` is `null`
goes into the history and stays there, so the `startswith` it breaks is not one
failed turn: it is that turn and every later one, until the conversation is
cleared. `_raw_estimate` had always guarded against it and nothing else had.

The file-writing half is the other kind. `edit_file` and `write_file` opened
the target with `open(path, "w")`, which truncates first and writes second - the
exact thing `atomic.py` exists to prevent, applied everywhere in the harness
except to the user's own source files. The line endings came with it: a read
through universal newlines and a write through `os.linesep` turns a whole file
from Unix endings to Windows ones, so a one-line edit reads as a rewrite.
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="malformed-home-"))

from aetheris import config          # noqa: E402
from aetheris import context         # noqa: E402
from aetheris import session         # noqa: E402
from aetheris import tools           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


work = tempfile.mkdtemp(prefix="malformed-")
os.chdir(work)

config.AUTO_ALLOW = True            # no approval prompt: there is no terminal here
config.PERMISSIONS_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.CHANNEL_ENABLED = False
config.AUTO_VERIFY = False


# ---------------------------------------------------------------------------
print("--- a message with no content ---")
# A session file written with `"content": null`, or a provider that reported a
# tool call and no text. It sits in the history, so anything that raises on it
# raises on every turn from then on.
broken = [{"role": "system", "content": "system"},
          {"role": "user", "content": None},
          {"role": "assistant", "content": None},
          {"role": "user", "content": "[Tool Result for 'read_file']:\nx" * 4000}]

for label, call in (
        ("the token estimate", lambda: context._raw_estimate(broken)),
        ("trimming tool results", lambda: context._trim_tool_results(broken, 100)),
        ("splitting the conversation", lambda: context._get_conv_pairs(broken)),
        ("forgetting pruned skills", lambda: context._sync_loaded_skills(broken))):
    try:
        call()
        check(f"{label} survives it", True)
    except Exception as error:
        check(f"{label} survives it", False, f"{type(error).__name__}: {error}")

check("and the real messages are still read",
      len(context._get_conv_pairs(broken)) >= 1)


# ---------------------------------------------------------------------------
print("\n--- editing a file does not rewrite it ---")
# Both directions, because guessing one is how the other breaks: a file with
# Unix endings must not come back with Windows ones, and a file that genuinely
# uses Windows endings must not be flattened either. `git diff` is what pays
# for getting this wrong - every line of the file, for a one-line edit.
unix = os.path.join(work, "unix.py")
with open(unix, "wb") as handle:
    handle.write(b"a = 1\nb = 2\nc = 3\n")
check("a Unix file is read as one", tools._existing_newline(unix) == "\n")
tools.handle_edit_file(unix, "", f"2:{tools._line_hash('b = 2')}|b = 22")
with open(unix, "rb") as handle:
    after = handle.read()
check("the edit landed", after == b"a = 1\nb = 22\nc = 3\n", repr(after))

windows = os.path.join(work, "windows.py")
with open(windows, "wb") as handle:
    handle.write(b"a = 1\r\nb = 2\r\nc = 3\r\n")
check("a Windows file is read as one", tools._existing_newline(windows) == "\r\n")
tools.handle_edit_file(windows, "", f"2:{tools._line_hash('b = 2')}|b = 22")
with open(windows, "rb") as handle:
    after = handle.read()
check("its endings survive the edit",
      after == b"a = 1\r\nb = 22\r\nc = 3\r\n", repr(after))


# ---------------------------------------------------------------------------
print("\n--- a write leaves nothing half-finished ---")
kept = os.path.join(work, "kept.py")
tools.handle_write_file(kept, "print('first')\n")
check("it writes", open(kept, encoding="utf-8").read() == "print('first')\n")
tools.handle_write_file(kept, "print('second')\n")
check("it replaces", open(kept, encoding="utf-8").read() == "print('second')\n")
check("no temporary files left behind",
      [f for f in os.listdir(work) if f.startswith(".tmp-")] == [],
      str(sorted(os.listdir(work))))

# A directory the model guessed at is not built on the way past. `atomic` makes
# one for its own callers, which own their directories; this path came from a
# model, and `create_dir` is the tool that asks before making one.
invented = os.path.join(work, "src", "utils", "helper.py")
refused = tools.handle_write_file(invented, "print('x')\n")
check("a missing directory is refused", refused.startswith(config.TOOL_ERROR_PREFIX),
      refused[:80])
check("and is not created on the way past", not os.path.isdir(os.path.dirname(invented)))
check("the file is not there either", not os.path.exists(invented))


# ---------------------------------------------------------------------------
print("\n--- a memory file somebody edited by hand ---")
# `memory.json` is a plain file in the user's own directory, and the obvious
# thing to write in it by hand is `{"name": "Bob"}` rather than the record the
# harness stores. Three of the five memory tools subscripted it blind.
with open(config.MEMORY_FILE, "w", encoding="utf-8") as handle:
    json.dump({"nickname": "Bob"}, handle)

check("the list reads it", "nickname" in session.handle_get_memory_list())
check("reading it works", "Bob" in session.handle_read_memory("nickname"))
edited = session.handle_edit_memory("nickname", "Bobby")
check("editing it works", edited.startswith("[Success]"), edited)
check("and the edit stuck", "Bobby" in session.handle_read_memory("nickname"))

with open(config.MEMORY_FILE, "w", encoding="utf-8") as handle:
    json.dump(["not", "a", "table"], handle)
check("a file of the wrong type reads as empty", session.load_memory() == {})


# ---------------------------------------------------------------------------
print("\n--- a setting set to nought ---")
# `/set` accepts any non-negative number, and `chat_turn` divided by this one.
# Zero is the natural way to write "no ceiling"; it ended the turn in a
# ZeroDivisionError instead of an answer.
value, problem = config.parse_setting("MAX_TOOL_CALLS", "0")
check("nought is an accepted value", value == 0 and not problem, problem)

import inspect                             # noqa: E402
from aetheris import llm_client      # noqa: E402

# Read out of the source rather than by running a turn, which would want a
# provider and a terminal. What is asserted is the modulo itself: whatever it
# divides by must have been tested for zero on the same line.
divisions = [line.strip() for line in
             inspect.getsource(llm_client.chat_turn).splitlines()
             if "%" in line and "call_count" in line]
check("the ceiling is still what ends a runaway tool loop", len(divisions) == 1,
      str(divisions))
check("and it is not divided by unguarded",
      bool(divisions) and "limit > 0" in divisions[0], divisions[0] if divisions else "")


# ---------------------------------------------------------------------------
print("\n--- settings.json records only what differs ---")
# Writing back a value that has since become the default would pin it there,
# and a later version's better default would never reach anyone who had once
# changed that setting.
name = "SEARCH_MAX_RESULTS"
config._saved.clear()
config.set_setting(name, str((config.defaults()[name] or 0) + 4))
check("a changed setting is recorded", name in config.saved_settings())

was = config._DEFAULTS[name]
try:
    config._DEFAULTS[name] = config.saved_settings()[name]   # the default catches up
    config._saved.clear()
    config.load_saved_settings()
    check("one that now matches the default is not", name not in config.saved_settings(),
          str(config.saved_settings()))
finally:
    config._DEFAULTS[name] = was
    config.set_setting(name, "default")


# ---------------------------------------------------------------------------
print("\n--- a tool call whose arguments are the wrong shape ---")
# What small models actually send. `{"filepath": {"path": "x"}}` because they
# read the schema as nesting, `{"content": ["a", "b"]}` because they were
# thinking in lines. Both used to raise out of the handler, through
# `dispatch_tool`, and end the whole turn on one line of red - with no tool
# result, so the model was never told and could not correct itself.
config.AUTO_ALLOW = True            # the approval prompt has no terminal here

import contextlib                            # noqa: E402
import io                                    # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):
    nested = tools.dispatch_tool("write_file", {"filepath": {"path": "x"}, "content": "x"})
    lines = tools.dispatch_tool("write_file", {"filepath": "lines.txt",
                                               "content": ["first", "second"]})
    numeric = tools.dispatch_tool("read_file", {"filepath": 7})

check("a path that is not a string is refused",
      nested.startswith(config.TOOL_ERROR_PREFIX), nested[:80])
check("and the refusal says what shape was wanted", "plain string" in nested)
check("a body sent line by line is joined rather than refused",
      lines.startswith("[Success]"), lines[:60])
check("and it lands as those lines",
      open(os.path.join(work, "lines.txt"), encoding="utf-8").read() == "first\nsecond")
check("a number where a path goes is refused too",
      numeric.startswith(config.TOOL_ERROR_PREFIX), numeric[:60])

# The floor under all of it: whatever a handler does, dispatch hands the model
# something it can read.
handlers = tools._handlers()
was = handlers["git_status"]
handlers["git_status"] = lambda: 1 / 0
try:
    with contextlib.redirect_stdout(io.StringIO()):
        raised = tools.dispatch_tool("git_status", {})
finally:
    handlers["git_status"] = was
check("a handler that raises becomes a tool result",
      raised.startswith(config.TOOL_ERROR_PREFIX), raised[:60])
check("and it names what went wrong", "ZeroDivisionError" in raised, raised[:100])


# ---------------------------------------------------------------------------
print("\n--- the approval prompt is not what raises ---")
# It is the one place every tool's arguments are shown, so it sees whatever the
# model sent. The gate in front of `run_cmd` and `delete_file` must not be the
# thing that falls over.
from aetheris import tui               # noqa: E402

config.AUTO_ALLOW = False
problem = ""
try:
    with contextlib.redirect_stdout(io.StringIO()):
        tui._approval_prompt("Test", [("a", 5), ("b", {"x": 1}), ("c", None)], rule="test")
except Exception as error:
    problem = f"{type(error).__name__}: {error}"
finally:
    config.AUTO_ALLOW = True
check("it renders a value of any type", not problem, problem)


# ---------------------------------------------------------------------------
print("\n--- replaying a session written by something else ---")
# Failing to *replay* a conversation must not be what stops it being resumed.
from aetheris import app               # noqa: E402

problem = ""
try:
    with contextlib.redirect_stdout(io.StringIO()):
        app._replay_session([{"role": "user"},                     # no content
                             {"role": "assistant", "content": None},
                             "not a dict at all",
                             {"content": "no role"}])
except Exception as error:
    problem = f"{type(error).__name__}: {error}"
check("a file with holes in it still replays", not problem, problem)


# ---------------------------------------------------------------------------
print("\n--- a turn cut short hands back an answer ---")
# `messages[-2]` at that point in the loop is whatever tool result happens to
# sit there, so declining to continue used to return "[Tool Result for ...]"
# to the user as though the model had said it.
from aetheris import llm_client        # noqa: E402

cut_short = [{"role": "system", "content": "system"},
             {"role": "assistant",
              "content": "Reading it now.\n<tool_call>{\"name\": \"read_file\"}</tool_call>"},
             {"role": "user", "content": "[Tool Result for 'read_file']:\n1:abc|x = 1"}]
check("it returns what the model said", llm_client._last_said(cut_short) == "Reading it now.",
      llm_client._last_said(cut_short))
check("and not the tool result beside it",
      "[Tool Result" not in llm_client._last_said(cut_short))
check("with nothing said, it returns nothing to fall back from",
      llm_client._last_said([{"role": "user", "content": "hello"}]) == "")


# ---------------------------------------------------------------------------
print("\n--- a provider refusing a request says why ---")
# The API key is the thing that goes wrong most often, and it was the thing
# this reported worst. `list_models` - what `/connect` and `/models` call, so
# the first place a wrong key is met - used `raise_for_status`, which keeps the
# status line and throws the body away: "401 Client Error: Unauthorized", with
# no mention of a key. The streaming path kept the body and cut it at 400
# characters, and `/connect` cut whatever survived that to 160.
from aetheris import providers        # noqa: E402


class FakeResponse:
    """Just enough of a `requests` response for the error reader."""

    def __init__(self, status, text):
        self.status_code, self.text = status, text


anthropic = providers.AnthropicProvider({"api_key": "k", "model": "m"})
detail = providers._error_detail(FakeResponse(401, json.dumps(
    {"type": "error", "error": {"type": "authentication_error",
                                "message": "invalid x-api-key"}})))
check("Anthropic's message is read out of its body", "invalid x-api-key" in detail, detail)
check("and the kind of error comes with it", "authentication_error" in detail, detail)

detail = providers._error_detail(FakeResponse(400, json.dumps(
    {"error": {"code": 400, "status": "INVALID_ARGUMENT",
               "message": "API key not valid. Please pass a valid API key."}})))
check("so is Gemini's", "API key not valid" in detail, detail)

long_message = "Incorrect API key provided: sk-" + "x" * 600 + ". Find yours at example.com."
detail = providers._error_detail(FakeResponse(401, json.dumps(
    {"error": {"message": long_message, "code": "invalid_api_key"}})))
check("a long message is not cut at 400 characters", len(detail) > 400, str(len(detail)))
check("and it keeps the end, which is where the fix is written",
      "Find yours at example.com." in detail)

# The other direction: a proxy's error page is markup and repetition, and
# printing all of it would bury the request that failed.
page = ("<html><head><title>502 Bad Gateway</title></head><body>"
        + "<p>nginx failed. </p>" * 400 + "</body></html>")
detail = providers._error_detail(FakeResponse(502, page))
check("an HTML page leads with its title", detail.startswith("502 Bad Gateway"), detail[:60])
check("carries no markup", "<" not in detail and ">" not in detail)
check("and is held to a few lines",
      len(detail) <= providers.RAW_DETAIL_CHARS + 60, str(len(detail)))

# The whole message, as `_failed` assembles it for the person reading.
whole = anthropic._failed(FakeResponse(401, json.dumps(
    {"error": {"message": "invalid x-api-key"}})))
check("the assembled failure names the provider", "Anthropic" in whole)
check("names the status", "401" in whole)
check("says what the provider said", "invalid x-api-key" in whole)
check("and says what to do about it", "ANTHROPIC_API_KEY" in whole, whole)

rate = anthropic._failed(FakeResponse(429, json.dumps(
    {"error": {"message": "quota exceeded"}}))).lower()
check("a 429 is explained as rate limit or credit",
      "credit" in rate or "rate limited" in rate, rate[-90:])
missing = anthropic._failed(FakeResponse(404, "{}"))
check("a 404 points at /models", "/models" in missing, missing[-70:])

# `_get_json` is the call that discarded the body entirely. The comment above
# it names `raise_for_status` on purpose, so this looks for the call itself.
source = inspect.getsource(providers.Provider._get_json)
check("listing models no longer calls raise_for_status",
      not re.search(r"\.raise_for_status\s*\(", source))


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("malformed-state checks passed")
