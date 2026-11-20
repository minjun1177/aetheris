"""The permission modes, and Shift+Tab moving through them.

A mode is the answer to "what will it do if I send this?": manual asks before
every guarded tool, plan has a plan approved first, accept-edits lets file edits
inside the project through, deepthink runs the pipeline, auto asks nothing.
What is checked: the order Shift+Tab goes in, that a mode is one combination of
the settings and never two at once, that accept-edits lets through exactly the
edits inside the project - not a command, not a delete, not a path outside it -
that the line under the prompt says which mode is on, and that `/mode` answers.
"""
import asyncio
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="modes-home-")

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_VERIFY = False

from aetheris import modes, tools, tui   # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def flags():
    return (config.AUTO_ALLOW, config.DEEPTHINK, config.ACCEPT_EDITS, config.PLANMODE)


print("--- the order, and one mode at a time ---")
modes.set_mode("manual")
check("manual is every switch off", flags() == (False, False, False, False))
seen = [modes.current()]
for _ in range(len(modes.MODES)):
    seen.append(modes.cycle())
check("Shift+Tab goes manual, plan, accept-edits, deepthink, auto, and round again",
      seen == ["manual", "plan", "accept-edits", "deepthink", "auto", "manual"], str(seen))
for name, expected in [("plan", (False, False, False, True)),
                       ("accept-edits", (False, False, True, False)),
                       ("deepthink", (False, True, False, False)),
                       ("auto", (True, False, False, False))]:
    modes.set_mode(name)
    check(f"{name} is its own switch and no other", flags() == expected, str(flags()))
modes.set_mode("Accept Edits")
check("a name is read loosely", modes.current() == "accept-edits")
try:
    modes.set_mode("yolo")
    refused = False
except ValueError:
    refused = True
check("a mode that does not exist is refused", refused)
check("and changes nothing", modes.current() == "accept-edits")
config.PLANMODE = True
config.AUTO_ALLOW = True
check("switches set by hand read as the most permissive mode", modes.current() == "auto")
modes.set_mode("manual")

print("\n--- the line under the prompt ---")
for name, words in [("manual", "manual"), ("plan", "plan mode on"),
                    ("accept-edits", "accept edits on"), ("deepthink", "deepthink on"),
                    ("auto", "auto mode on")]:
    modes.set_mode(name)
    line = config._ESCAPE.sub("", modes.status_line())
    check(f"{name} says so, and how to change it",
          words in line and "shift+tab" in line, line)
modes.set_mode("manual")

print("\n--- accept-edits lets through edits inside the project, and no more ---")
work = os.path.realpath(tempfile.mkdtemp(prefix="modes-work-"))
outside = os.path.realpath(tempfile.mkdtemp(prefix="modes-outside-"))
origin = os.getcwd()
os.chdir(work)
with open("a.txt", "w", encoding="utf-8") as handle:
    handle.write("one\n")

asked = []
real_ask = tui._ask_approval
tui._ask_approval = lambda label, details, rule="": asked.append(label) or False


def run(name, **arguments):
    asked.clear()
    out, real = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        return tools.dispatch_tool(name, arguments) or ""
    finally:
        sys.stdout = real


try:
    modes.set_mode("manual")
    result = run("write_file", filepath="b.txt", content="two\n")
    check("in manual a write is asked about", asked == ["Write File"], str(asked))
    check("and, refused, does not happen", not os.path.exists("b.txt"), result)

    modes.set_mode("accept-edits")
    result = run("write_file", filepath="b.txt", content="two\n")
    check("in accept-edits a write inside the project is not asked about",
          asked == [], str(asked))
    check("and happens", os.path.exists("b.txt"), result)
    run("edit_file", filepath="a.txt", old_text="one", new_text="uno")
    check("an edit inside it is not asked about either", asked == [], str(asked))
    run("create_dir", dirpath="sub")
    check("nor a new directory", asked == [] and os.path.isdir("sub"), str(asked))
    run("copy_file", src="a.txt", dst="sub/a.txt")
    check("nor a copy", asked == [] and os.path.exists("sub/a.txt"), str(asked))

    target = os.path.join(outside, "c.txt")
    run("write_file", filepath=target, content="three\n")
    check("a write outside the project is still asked about",
          asked == ["Write File"] and not os.path.exists(target), str(asked))
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("from elsewhere\n")
    run("copy_file", src=target, dst="d.txt")
    check("and so is a copy of something from outside it",
          bool(asked) and not os.path.exists("d.txt"), str(asked))
    run("delete_file", filepath="b.txt")
    check("a delete is still asked about", asked == ["Delete File"]
          and os.path.exists("b.txt"), str(asked))
    run("run_cmd", command="echo hi")
    check("and a command", asked == ["Run Command"], str(asked))
    check("nothing is left allowed once the call is over",
          config.POLICY_AUTO_ALLOW is False)

    modes.set_mode("auto")
    run("delete_file", filepath="b.txt")
    check("in auto even a delete is not asked about",
          asked == [] and not os.path.exists("b.txt"), str(asked))
finally:
    tui._ask_approval = real_ask
    modes.set_mode("manual")
    os.chdir(origin)

print("\n--- the key ---")
if config.PROMPT_TOOLKIT_AVAILABLE:
    from aetheris import promptkeys
    binding = next(b for b in promptkeys.bindings().bindings
                   if getattr(b.keys[0], "value", b.keys[0]) == "s-tab")

    class Buffer:
        complete_state = None
        stepped_back = False

        def complete_previous(self):
            Buffer.stepped_back = True

    class App:
        drawn = 0

        def invalidate(self):
            App.drawn += 1

    class Event:
        current_buffer = Buffer()
        app = App()

    binding.handler(Event)
    check("Shift+Tab moves to the next mode", modes.current() == "plan")
    check("and redraws the line that says so", App.drawn == 1)
    Buffer.complete_state = object()
    binding.handler(Event)
    check("with the menu open it steps back through the menu instead",
          Buffer.stepped_back and modes.current() == "plan")
    modes.set_mode("manual")
else:
    print("  (prompt_toolkit is not installed - the key is not bound)")

print("\n--- /mode ---")
from aetheris import app   # noqa: E402


def command(line):
    out, real = io.StringIO(), sys.stdout
    sys.stdout = out
    try:
        asyncio.run(app._cmd_mode(None, line, line.split()[0]))
    finally:
        sys.stdout = real
    return config._ESCAPE.sub("", out.getvalue())


shown = command("/mode")
check("alone it lists every mode", all(name in shown for name in modes.MODES), shown)
check("and says which is on", "Mode is manual" in shown, shown)
command("/mode accept-edits")
check("with a name it switches", modes.current() == "accept-edits")
shown = command("/mode nonsense")
check("a wrong name is refused and says what is right",
      "not a mode" in shown and modes.current() == "accept-edits", shown)
check("it is for this session: nothing is saved", config.saved_settings() == {},
      str(config.saved_settings()))
modes.set_mode("manual")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("mode checks passed")
