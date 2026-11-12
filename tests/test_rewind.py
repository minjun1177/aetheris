"""`/rewind`: the conversation as it was before the last prompt, and that prompt back.

A bad turn costs the turns after it: a small model writes its next answer on
top of the failed calls and the abandoned plan. What is checked: the history
goes back to exactly what it was - channel notes kept, compaction undone - the
prompt returns to the box, the checklist goes back with it, the files do not
and the commits they are in are named, and a different conversation never
inherits the points of the last one.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="rewind-home-")
os.environ["AETHERIS_HOME"] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.SAVE_CHAT_HISTORY = False

import inspect                                              # noqa: E402
from aetheris import app, git_ops, providers, rewind, todos  # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def say(role, text):
    return {"role": role, "content": text}


print("--- the points ---")
rewind.forget()
messages = [say("system", "s")]
rewind.mark(messages, "first")
messages += [say("user", "first"), say("assistant", "one")]
config.TODOS[:] = todos.parse("[ ] a")
rewind.mark(messages, "second")
messages += [say("user", "second"), say("assistant", "<tool_call>x</tool_call>"),
             say("user", "[tool result]"), say("assistant", "two")]
config.TODOS[:] = todos.parse("[x] a\n[ ] b")
holder = messages
check("one point per prompt", rewind.available() == 2)

taken = rewind.rewind(messages, 1)
check("the history is what it was before the prompt",
      messages == [say("system", "s"), say("user", "first"), say("assistant", "one")],
      str(messages))
check("changed in place, so everything holding the list sees it", holder is messages)
check("the prompt comes back", taken["prompt"] == "second")
check("the checklist goes back too", [i["text"] for i in config.TODOS] == ["a"]
      and not config.TODOS[0]["done"], str(config.TODOS))
check("and that point is used up", rewind.available() == 1)
check("more than there are is refused, and nothing changes",
      rewind.rewind(messages, 2) is None and len(messages) == 3)
check("so is none", rewind.rewind(messages, 0) is None)

print("\n--- what the history looked like is kept, not where it was cut ---")
rewind.forget()
messages = [say("system", "s"), say("user", "old"), say("assistant", "old answer")]
rewind.mark(messages, "next")
messages += [say("user", "next"), say("assistant", "...")]
# Compaction rewrites the early part of the list in the middle of a turn.
messages[:] = [say("system", "s"), say("user", "[summary]"), say("assistant", "...")]
rewind.rewind(messages, 1)
check("a turn that was compacted comes back uncompacted",
      messages == [say("system", "s"), say("user", "old"), say("assistant", "old answer")],
      str(messages))

rewind.forget()
for n in range(rewind.KEEP + 5):
    rewind.mark([say("system", "s")], f"p{n}")
check(f"only the last {rewind.KEEP} are kept", rewind.available() == rewind.KEEP)
rewind.forget()
check("and forget drops them all", rewind.available() == 0)

print("\n--- the command ---")


def run(arg, messages, sid="sid"):
    """`app._rewind`, with what it printed."""
    import builtins
    said = []
    original = builtins.print
    builtins.print = lambda *a, **k: said.append(" ".join(str(x) for x in a))
    try:
        result = app._rewind(messages, arg, sid)
    finally:
        builtins.print = original
    return result, "\n".join(said)


messages = [say("system", "s")]
_, said = run("", messages)
check("with nothing to take back it says so", "Nothing to take back" in said, said)
rewind.mark(messages, "fix the parser")
messages += [say("user", "fix the parser"), say("assistant", "done")]
_, said = run("two", messages)
check("a count that is not a number is a usage error", "Usage: /rewind" in said, said)
_, said = run("3", messages)
check("one beyond the reach says how far it goes", "1 to 1 prompt" in said, said)
app._kept_typing = ""
_, said = run("", messages)
check("it takes the turn back", messages == [say("system", "s")], str(messages))
check("and the prompt waits in the box", app._kept_typing == "fix the parser")
app._kept_typing = ""


class Elsewhere(providers.Provider):
    name = "elsewhere"
    label = "Elsewhere"
    needs_key = False
    drives_turns = True

    def ready(self):
        return ""


saved, providers._active = providers._active, Elsewhere({"model": "x"})
try:
    rewind.mark(messages, "p")
    _, said = run("", messages)
finally:
    providers._active = saved
check("a provider that keeps its own context is not rewound behind its back",
      "Attacca keeps" in said and rewind.available() == 1, said)
rewind.forget()

print("\n--- the files stay, and are named ---")
repo = tempfile.mkdtemp(prefix="rewind-repo-")
cwd = os.getcwd()
os.chdir(repo)
try:
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    with open("a.txt", "w") as handle:
        handle.write("a\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "start")
    git_ops._repo_root_cache.clear()
    messages = [say("system", "s")]
    rewind.mark(messages, "change a")
    with open("a.txt", "w") as handle:
        handle.write("b\n")
    git_ops.auto_commit(["a.txt"], "edit_file")
    with open("mine.txt", "w") as handle:
        handle.write("mine\n")
    git("add", "mine.txt")
    git("commit", "-q", "-m", "my own")
    messages += [say("user", "change a"), say("assistant", "changed")]
    _, said = run("", messages)
    with open("a.txt") as handle:
        content = handle.read()
finally:
    os.chdir(cwd)
check("the file is left as the turn made it", content == "b\n", content)
check("the AI commit is named, with /undo", "ai(edit_file)" in said and "/undo" in said, said)
check("and a commit the person made is not", "my own" not in said, said)
check("no commits since is an empty list, not an error",
      git_ops.ai_commits_since("") == [])

print("\n--- the loop marks every prompt and forgets on a new conversation ---")
source = inspect.getsource(app.main)
check("a point is taken before the prompt is added",
      0 <= source.find("rewind.mark(messages, user_input)")
      < source.find('turn = {"role": "user", "content": user_input}'))
check("/clear forgets them", "rewind.forget()" in inspect.getsource(app._cmd_clear))
check("and so does opening a saved session",
      "rewind.forget()" in inspect.getsource(app._adopt_session))


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("rewind checks passed")
