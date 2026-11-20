"""Hooks: the person's own commands around a tool call and at the end of a turn.

A `pre_tool` hook that fails refuses the call before its handler or its approval
prompt is reached, and says why in a `[System]` result. A `post_tool` hook that
fails adds what it printed to the result. A project's `.hooks.json` runs nothing
until it is trusted, and no hook is handed this harness's API keys.
"""
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="hooks-home-")
os.environ["AETHERIS_HOME"] = HOME
from aetheris import config
config.SAVE_CHAT_HISTORY = False
config.PERMISSIONS_ENABLED = True
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = False
config.AUTO_VERIFY = False
config.CHANNEL_ENABLED = False

from aetheris import app, hooks, tools, trust, tui   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def quiet(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = function(*args, **kwargs)
    return result, out.getvalue()


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


PY = f'"{sys.executable}"'
origin = os.getcwd()
work = tempfile.mkdtemp(prefix="hooks-work-")
os.chdir(work)
seen = os.path.join(work, "seen.json")
after = os.path.join(work, "after.txt")

# A pre_tool hook that refuses `write_file` into `blocked/`, and records what it was given.
pre = os.path.join(work, "pre.py")
with open(pre, "w", encoding="utf-8") as handle:
    handle.write(
        "import json, os, sys\n"
        "data = json.load(sys.stdin)\n"
        "json.dump({'stdin': data, 'tool': os.environ.get('AETHERIS_TOOL'),\n"
        "           'files': os.environ.get('AETHERIS_FILES'),\n"
        "           'leaked': os.environ.get('OPENAI_API_KEY', '')},\n"
        f"          open({seen!r}, 'w'))\n"
        "if data['arguments'].get('filepath', '').startswith('blocked/'):\n"
        "    print('nothing goes in blocked/')\n"
        "    sys.exit(1)\n")
post = os.path.join(work, "post.py")
with open(post, "w", encoding="utf-8") as handle:
    handle.write(
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        f"open({after!r}, 'a').write(data['tool'] + '|' + data['result'][:9] + '\\n')\n"
        "if 'bad' in data['arguments'].get('filepath', ''):\n"
        "    print('lint: line 1 is wrong', file=sys.stderr)\n"
        "    sys.exit(3)\n")

write_json(hooks.user_config_file(), {
    "pre_tool": [{"match": "write_file", "command": f"{PY} pre.py"}],
    "post_tool": [{"match": "write_file", "command": f"{PY} post.py"}],
})
os.environ["OPENAI_API_KEY"] = "sk-test-not-for-hooks-1234567890"

print("--- the user's own hooks file ---")
loaded = hooks.load(force=True)
check("both hooks are read", len(loaded) == 2, str([(h.event, h.match) for h in loaded]))
check("with nothing wrong", hooks.errors == [], str(hooks.errors))

print("\n--- a pre_tool hook decides before the handler runs ---")
result, _ = quiet(tools.dispatch_tool, "write_file",
                  {"filepath": "blocked/x.txt", "content": "hi"})
check("a failing hook refuses the call", isinstance(result, str)
      and result.startswith(config.TOOL_REFUSAL_PREFIX), str(result)[:120])
check("and the model is told why, in the hook's words", "nothing goes in blocked/" in result)
check("nothing was written", not os.path.exists(os.path.join(work, "blocked", "x.txt")))
given = json.load(open(seen, encoding="utf-8"))
check("the hook got the call as JSON on stdin",
      given["stdin"]["tool"] == "write_file"
      and given["stdin"]["arguments"]["filepath"] == "blocked/x.txt", str(given["stdin"]))
check("and in its environment", given["tool"] == "write_file"
      and given["files"] == "blocked/x.txt", str(given))
check("without the provider keys", given["leaked"] == "", given["leaked"])
check("a refused call never reached the post_tool hook", not os.path.exists(after))

result, _ = quiet(tools.dispatch_tool, "write_file", {"filepath": "ok.txt", "content": "hi"})
check("a passing hook lets the call run", str(result).startswith("[Success"), str(result)[:120])
check("the file is there", os.path.isfile(os.path.join(work, "ok.txt")))
check("and the post_tool hook saw the result",
      open(after, encoding="utf-8").read().strip() == "write_file|[Success]",
      open(after, encoding="utf-8").read())

print("\n--- a post_tool hook cannot take a call back, so it adds to the result ---")
result, _ = quiet(tools.dispatch_tool, "write_file", {"filepath": "bad.py", "content": "x"})
check("the call still succeeded", str(result).startswith("[Success"), str(result)[:80])
check("and the hook's complaint travels with it",
      "[Hook]" in result and "exited 3" in result and "line 1 is wrong" in result, result)

print("\n--- match is a permission rule ---")
result, _ = quiet(tools.dispatch_tool, "list_dir", {"dirpath": "."})
check("a tool the hooks do not match is left alone", "[Hook]" not in str(result)
      and not str(result).startswith(config.TOOL_REFUSAL_PREFIX), str(result)[:80])
check("a pattern narrows it to the target",
      len(hooks.hooks_for("pre_tool", "run_cmd", {"command": "ls"})) == 0)
write_json(hooks.user_config_file(), {"pre_tool": [
    {"match": "run_cmd(git push*)", "command": "exit 1"}, {"command": "exit 0"}]})
hooks.load(force=True)
check("run_cmd(git push*) matches a push",
      len(hooks.hooks_for("pre_tool", "run_cmd", {"command": "git push origin"})) == 2)
check("and not a status",
      len(hooks.hooks_for("pre_tool", "run_cmd", {"command": "git status"})) == 1)
config.HOOKS_ENABLED = False
check("HOOKS_ENABLED off runs none of them",
      hooks.hooks_for("pre_tool", "run_cmd", {"command": "git push"}) == [])
config.HOOKS_ENABLED = True

print("\n--- a broken file says so and runs nothing ---")
write_json(hooks.user_config_file(), {"before": [{"command": "x"}],
                                      "pre_tool": [{"match": "write_file"}]})
check("an unknown event and an entry with no command are both errors",
      hooks.load(force=True) == [] and len(hooks.errors) == 2, str(hooks.errors))
with open(hooks.user_config_file(), "w", encoding="utf-8") as handle:
    handle.write("{not json")
check("invalid JSON is an error, not a crash", hooks.load(force=True) == []
      and hooks.errors and "cannot be read" in hooks.errors[0], str(hooks.errors))
os.remove(hooks.user_config_file())

print("\n--- turn_end ---")
write_json(hooks.user_config_file(), {"turn_end": [
    {"command": f"{PY} -c \"import json,sys; print(json.load(sys.stdin)['outcome'])\""},
    {"command": "exit 5"}]})
hooks.load(force=True)
reports = hooks.turn_ended("done")
check("every turn_end hook runs", len(reports) == 2, str(reports))
check("and is told how the turn ended", reports[0][1] == 0 and reports[0][2] == "done",
      str(reports[0]))
_, printed = quiet(app._run_turn_end_hooks, "done")
check("a failing one is reported on screen", "exit 5" in printed and "exited 5" in printed,
      printed)
os.remove(hooks.user_config_file())

print("\n--- a project's .hooks.json runs nothing until trusted ---")
PROJECT = os.path.join(work, ".hooks.json")
write_json(PROJECT, {"pre_tool": [{"command": "exit 1"}]})
hooks.load(force=True)
check("untrusted: none of its hooks", hooks.load() == [], str(hooks.load()))
check("and /hooks can say why", PROJECT in hooks.untrusted)
check("it is one of the files the trust prompt asks about",
      ("hooks", PROJECT) in app._project_config_files(), str(app._project_config_files()))
real_ask = tui.ask_the_driver
tui.ask_the_driver = lambda *a, **k: "y"
_, printed = quiet(app._settle_trust)
tui.ask_the_driver = real_ask
check("what it would run is shown before the question", "exit 1" in printed, printed[:300])
check("trusted: its hooks are used", len(hooks.load()) == 1 and trust.is_trusted(PROJECT))
result, _ = quiet(tools.dispatch_tool, "list_dir", {"dirpath": "."})
check("and they apply", str(result).startswith(config.TOOL_REFUSAL_PREFIX), str(result)[:80])
os.remove(PROJECT)
hooks.load(force=True)

os.chdir(origin)
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("hook checks passed")
