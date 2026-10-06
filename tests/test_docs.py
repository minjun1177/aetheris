"""The documentation has to describe the program that exists.

The tool table stopped drifting from the prompt once one generated the other.
Prose cannot be generated, but it can be checked: this fails when README.md or
ARCHITECTURE.md names something that is gone, or misses something that is new.

It caught, on the day it was written, a tool count three behind, an install
command with the filename misspelled, and a paragraph describing behaviour that
had been replaced two features ago.
"""
import inspect
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from aetheris import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import toolspec
from aetheris import tools

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


PKG = os.path.join(ROOT, "aetheris")


def read(name):
    """A file by the name the docs use, found wherever it actually lives."""
    for base in (ROOT, PKG, os.path.join(ROOT, "tests")):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    raise FileNotFoundError(name)


def exists(name):
    return any(os.path.isfile(os.path.join(base, name))
               for base in (ROOT, PKG, os.path.join(ROOT, "tests")))


README = read("README.md")
ARCH = read("ARCHITECTURE.md")
# Prose wraps, so a sentence to match against must be joined back up first.
ARCH_FLAT = " ".join(ARCH.split())
APP = read("app.py")
TUI = read("tui.py")

print("--- every tool is documented ---")
undocumented = [t.name for t in toolspec.TOOLS if f"`{t.name}`" not in README]
check("README names every tool", not undocumented, str(undocumented))
claimed = re.search(r"equips the model with (\d+) tools", README)
check("and says how many there are", claimed is not None)
if claimed:
    check("with the right number", int(claimed.group(1)) == len(toolspec.TOOLS),
          f"README says {claimed.group(1)}, there are {len(toolspec.TOOLS)}")

print("\n--- every slash command is documented, completable and in /help ---")
commands = sorted(set(re.findall(r'cmd == "(/[a-z]+)"', APP))
                  | set(re.findall(r'cmd\.startswith\("(/[a-z]+)', APP)))
check("app.py defines commands", len(commands) > 15, f"{len(commands)} found")
check("README documents every one",
      not [c for c in commands if f"`{c}" not in README],
      str([c for c in commands if f"`{c}" not in README]))
# The menu and `/help` are now rendered from one table, so the question is no
# longer "do these two hand-written lists agree" but "does that table cover
# every command app.py answers". `command_names()` is what the menu is built
# from, so asking it is asking the menu.
from aetheris import tui                                      # noqa: E402

completable = {insert for insert, _, _ in tui.complete_command("/")}
check("the tab-completion menu is built from the command table",
      "SlashCommandCompleter(complete_command)" in APP and len(completable) > 15,
      f"{len(completable)} names")
check("and every suggestion says what it does",
      not [i for i, _, meta in tui.complete_command("/") if not meta.strip()],
      str([i for i, _, meta in tui.complete_command("/") if not meta.strip()]))
check("every command is tab-completable",
      not [c for c in commands if c not in completable],
      str([c for c in commands if c not in completable]))
helped = set(name.split()[0] for name, _ in tui.COMMANDS)
check("every command is in /help", not [c for c in commands if c not in helped],
      str([c for c in commands if c not in helped]))
check("and every row in the table describes something",
      not [name for name, desc in tui.COMMANDS if not desc.strip()],
      str([name for name, desc in tui.COMMANDS if not desc.strip()]))
# The table is the help, so a row naming a command app.py does not answer is a
# promise nothing keeps.
check("no row promises a command that does not exist",
      not [c for c in helped if c not in commands and c not in ("/exit",)],
      str([c for c in helped if c not in commands and c not in ("/exit",)]))

# Every on/off command reads the same way, or none of them does. Six answered a
# bare `/x` with "✗ Usage:" and no word about what the switch was even for,
# while three explained themselves - and the six also skipped the blank line
# the rest of the loop ends on. `/mcp` and `/agents` take `on`/`off` too but
# are not only switches: a bare one shows a screen of its own, so they have
# more than one row for good reason and are checked apart from the rest.
NOT_ONLY_SWITCHES = ("/mcp", "/agents")
switches = [name.split()[0] for name, _ in tui.COMMANDS if "[on/off]" in name]
check("every switch is spelled the same way in /help", len(switches) >= 8, str(switches))

pure = [s for s in switches if s not in NOT_ONLY_SWITCHES]
doubled = [s for s in pure if sum(1 for n, _ in tui.COMMANDS if n.split()[0] == s) > 1]
check("a switch is one row, not one to read and one to set", not doubled, str(doubled))

undelegated = [s for s in switches
               if re.search(rf'_switch\(\s*\n?\s*"{re.escape(s)}"', APP) is None]
check("and each goes through the one shared switch", not undelegated,
      str(undelegated))

print("\n--- the settings README documents still exist ---")
documented_settings = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", README, re.M))
check("README has a settings table", len(documented_settings) > 10,
      f"{len(documented_settings)} settings")
gone = [s for s in documented_settings if not hasattr(config, s)]
check("all of them are real", not gone, str(gone))

print("\n--- the install command works ---")
install = re.search(r"pip install -r (\S+)", README)
check("README gives an install command", install is not None)
if install:
    check("naming a file that exists", os.path.exists(os.path.join(ROOT, install.group(1))),
          install.group(1))

print("\n--- ARCHITECTURE.md points at things that exist ---")
named_files = sorted(set(re.findall(r"`([a-z_]+\.py)`", ARCH)))
missing = [f for f in named_files if not exists(f)]
check("every file it names exists", not missing, str(missing))
check("and it names most of them", len(named_files) > 15, f"{len(named_files)} files")

# A file the docs point you at has to be a file you can open. TODO.md pointed
# at a `roadmap.md` that was never committed, and went on doing so: the check
# above reads Python filenames only, so a dead markdown link had nothing
# looking at it.
named_docs = set()
for text in (README, ARCH, read("TODO.md"), read("CHANGELOG.md")):
    # Prose only. A fenced block is as likely to be an illustration of a layout
    # (`quick-skill.md` in a sample tree) as a pointer at a real file, and an
    # illustration is not a broken link.
    prose, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            prose.append(line)
    prose = "\n".join(prose)
    named_docs |= set(re.findall(r"([A-Za-z0-9_-]+\.md)", prose))
# By basename and anywhere in the tree: a skill's `SKILL.md` is named without
# the folder it sits in, and the point here is "is there such a file at all",
# not where it lives.
on_disk_docs = set()
for base, dirs, names in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")
               and "venv" not in d]
    on_disk_docs |= {n for n in names if n.endswith(".md")}
absent = sorted(d for d in named_docs if d not in on_disk_docs)
check("every markdown file the docs name exists", not absent, str(absent))

# The size in ARCHITECTURE's opening line is prose, so nothing regenerates it -
# and prose that states a number goes stale silently. It is rounded on purpose,
# so this asks only that the rounding still be honest.
stated = re.search(r"~([\d,]+) lines of Python", ARCH_FLAT)
check("ARCHITECTURE states the size of the codebase", stated is not None)
if stated:
    real = sum(len(read(f).splitlines())
               for f in sorted(os.listdir(PKG)) if f.endswith(".py"))
    claimed_lines = int(stated.group(1).replace(",", ""))
    check("and the number is within a tenth of the real one",
          abs(claimed_lines - real) <= real * 0.10,
          f"doc says {claimed_lines:,}, the package is {real:,}")

# Anything written as `module.function` should resolve.
from aetheris import (atomic, context, deepthink, git_ops, llm_client,
                            providers, session, subagent)
MODULES = {m.__name__: m for m in (atomic, config, context, deepthink, git_ops,
                                   llm_client, providers, session, subagent,
                                   toolspec, tools)}
# `config.MODEL`, `deepthink.run(messages)`, `atomic.write_json(path, private=True)`
# - the argument list has to be allowed for, or a renamed function slips past.
# Not `config.py`, which is a filename and is checked above.
referenced = {(m, a) for m, a in
              re.findall(r"`([a-z_]+)\.([A-Za-z_]+)(?:\([^`]*\))?`", ARCH)
              if a != "py"}
unresolved = [f"{m}.{a}" for m, a in referenced
              if m in MODULES and not hasattr(MODULES[m], a)]
check("every module.attribute it names resolves", not unresolved, str(unresolved))
check("and it names a useful number of them", len(referenced) > 10,
      f"{len(referenced)} references")

print("\n--- the invariants it states are the ones enforced ---")
check("the refusal prefix it quotes is the real one",
      f'`{llm_client.REFUSAL_PREFIX}` prefixes a refusal' in ARCH
      or f"`{llm_client.REFUSAL_PREFIX}`" in ARCH)
said = re.search(r"ends the turn after (\w+)", ARCH_FLAT)
check("the doc says when a refused model is stopped", said is not None)
check("and the number matches the code",
      said and {"three": 3, "two": 2, "four": 4, "five": 5}.get(
          said.group(1), -1) == llm_client.MAX_REFUSALS_IN_A_ROW,
      f"doc says {said.group(1) if said else '?'}, "
      f"code says {llm_client.MAX_REFUSALS_IN_A_ROW}")
check("the stage table matches deepthink.STAGES",
      all(stage.key in ARCH for stage in deepthink.STAGES))
check("it describes the read-only stages correctly",
      [s.edits for s in deepthink.STAGES] == [False, False, True, False, True, True])
check("toolspec really imports nothing local",
      not re.search(r"^(?:import|from) (?:aetheris\b|"
                    r"(?:config|tools|providers|app|systemprompt)\b)",
                    read("toolspec.py"), re.M))
check("config really imports systemprompt at module level",
      "from aetheris.systemprompt import" in read("config.py"))

print("\n--- every test file the docs list exists ---")
listed = set(re.findall(r"`(test_[a-z_]+\.py)`", ARCH)) | set(
    re.findall(r"`tests/(test_[a-z_]+\.py)`", README))
on_disk = {f for f in os.listdir(os.path.join(ROOT, "tests")) if f.startswith("test_")}
check("the docs list only real test files", not (listed - on_disk), str(listed - on_disk))
check("and every test file is listed somewhere",
      not (on_disk - listed - {"test_docs.py"}), str(on_disk - listed - {"test_docs.py"}))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("documentation checks passed")
