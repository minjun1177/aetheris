"""The person's own slash commands: a markdown file in a commands directory is `/<name>`.

The body is the message, `$ARGUMENTS` and `$1`..`$9` take what was typed after
the name, a project's file wins over the person's, and no file can take over a
built-in command.
"""
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="commands-home-")
os.environ["AETHERIS_HOME"] = HOME
from aetheris import config
config.SAVE_CHAT_HISTORY = False

from aetheris import app, commands, tui   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


origin = os.getcwd()
work = tempfile.mkdtemp(prefix="commands-work-")
os.chdir(work)
USER = os.path.join(HOME, "commands")
PROJECT = os.path.join(work, ".aetheris", "commands")
reserved = tui.reserved_names()

write(os.path.join(USER, "review.md"),
      "---\ndescription: Review the staged diff\n---\nReview `git diff --cached` for $ARGUMENTS.\n")
write(os.path.join(USER, "notes.md"), "Write release notes from the git log.\n")
write(os.path.join(USER, "swap.md"), "Rename $1 to $2 everywhere.\n")
write(os.path.join(USER, "help.md"), "I would replace /help.\n")
write(os.path.join(USER, "Bad Name.md"), "x\n")
write(os.path.join(USER, "empty.md"), "---\ndescription: nothing\n---\n")

print("--- a file is a command ---")
found, problems = commands.discover(reserved)
check("each usable file is found", sorted(found) == ["notes", "review", "swap"], str(sorted(found)))
check("its description comes from the frontmatter",
      found["review"]["description"] == "Review the staged diff")
check("or, without one, from its first line",
      found["notes"]["description"] == "Write release notes from the git log.")
check("a built-in's name is refused, and said", "help" not in found
      and any("/help is a built-in" in p for p in problems), str(problems))
check("so is a name that is not one", any("Bad Name" in p for p in problems))
check("and a file with no prompt", any("empty.md" in p for p in problems))

print("\n--- what was typed goes in ---")
text, used = commands.expand("/review the security bits", reserved)
check("$ARGUMENTS is what follows the name",
      text == "Review `git diff --cached` for the security bits.", text)
check("and the command used is returned", used and used["name"] == "review")
text, _ = commands.expand("/swap old_name new_name", reserved)
check("$1 and $2 are its words", text == "Rename old_name to new_name everywhere.", text)
text, _ = commands.expand("/notes since v1.2", reserved)
check("a body without either has it added at the end",
      text == "Write release notes from the git log.\n\nsince v1.2", text)
text, _ = commands.expand("/REVIEW x", reserved)
check("the name is not case-sensitive", text.endswith("for x."), text)
check("a name with no file is not a command", commands.expand("/nosuch x", reserved) == ("", None))
check("nor is a built-in", commands.expand("/help", reserved) == ("", None))
check("nor a line that is not a command", commands.expand("review", reserved) == ("", None))

print("\n--- the project's own wins ---")
write(os.path.join(PROJECT, "review.md"), "Project review of $ARGUMENTS.\n")
found, _ = commands.discover(reserved)
check("the project's file is used", found["review"]["source"] == "project"
      and commands.expand("/review it", reserved)[0] == "Project review of it.")

print("\n--- the menu and /commands show them ---")
offered = {insert: meta for insert, _, meta in tui.complete_command("/re")}
check("typing / offers them, with what they do",
      offered.get("/review") == "Project review of $ARGUMENTS.", str(offered))
check("the built-ins are still offered first", "/rewind" in offered and "/remote" in offered)
check("the menu never offers a refused one",
      "/help" not in [i for i, _, m in tui.complete_command("/he") if "replace" in m])
out = io.StringIO()
with contextlib.redirect_stdout(out):
    app._show_commands()
check("/commands lists each with its source",
      "/review" in out.getvalue() and "(project)" in out.getvalue()
      and "/notes" in out.getvalue(), out.getvalue())
check("and the problems", "built-in" in out.getvalue())

print("\n--- a built-in matches its whole name, not a prefix of it ---")
import asyncio   # noqa: E402


class Chat:
    pass


chat = Chat()
chat.messages, chat.session_id, chat.mcp_servers = [{"role": "system", "content": "s"}], None, []
for line in ("/systemd keeps restarting nginx, why?", "/exporter config", "/loadtest x",
             "/skillset y"):
    config.CUSTOM_PERSONA = ""
    with contextlib.redirect_stdout(io.StringIO()):
        outcome = asyncio.run(app._slash_command(chat, line, line.lower()))
    check(f"{line.split()[0]} is not taken for a built-in", isinstance(outcome, app._Next)
          and config.CUSTOM_PERSONA == "" and not os.path.exists("er config"), repr(outcome))
write(os.path.join(USER, "skillset.md"), "Skill set for $ARGUMENTS.\n")
check("so a command of yours may begin like one",
      commands.expand("/skillset me", tui.reserved_names())[0] == "Skill set for me.")
config.CUSTOM_PERSONA = ""

os.chdir(origin)
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("command checks passed")
