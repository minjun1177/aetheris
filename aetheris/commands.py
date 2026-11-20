"""Slash commands of the person's own: a markdown file is a prompt with a name.

A request typed often - "review the staged diff against our checklist", "write
the release notes from the log" - was retyped every time, or pasted from
somewhere. A file `review.md` in a commands directory makes it `/review`.

    ~/.aetheris/commands/<name>.md     the person's own, in every project
    .aetheris/commands/<name>.md       the project's; it wins over the person's

The file's body is the message sent to the model. `$ARGUMENTS` in it becomes
whatever was typed after the command, and `$1` to `$9` the words of it; a body
with neither has what was typed added at the end. An optional frontmatter
`description:` is what the menu shows beside it.

These are prompts, not programs: nothing in one runs on this machine until the
model asks for a tool and the ordinary approval and rules decide. So a
project's commands need no trust prompt - typing one is the person's choice.
A file named like a built-in command is ignored, and `/commands` says so.
"""

import os
import re

from aetheris import paths

PROJECT_DIR = os.path.join(".aetheris", "commands")
BODY_MAX_CHARS = 20000
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def directories() -> list:
    """(source, directory), highest precedence first. Read at call time."""
    return [("project", os.path.abspath(PROJECT_DIR)),
            ("user", paths.state("commands"))]


def _read(path: str) -> tuple:
    """(description, body) of one command file."""
    from aetheris.skills import _parse_frontmatter
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        text = handle.read(BODY_MAX_CHARS + 1)
    meta, body = _parse_frontmatter(text)
    body = body.strip()
    description = meta.get("description") if isinstance(meta.get("description"), str) else ""
    if not description:
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        description = first[:80]
    return description, body


def discover(reserved=()) -> tuple:
    """({name: command}, [problems]). Never raises.

    `reserved` holds the built-in command names, without the slash. A file
    named like one of them is left out and reported, rather than quietly
    changing what a documented command does.
    """
    found, problems = {}, []
    for source, directory in directories():
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for filename in names:
            stem, extension = os.path.splitext(filename)
            if extension.lower() != ".md":
                continue
            name = stem.lower()
            path = os.path.join(directory, filename)
            if not os.path.isfile(path):
                continue
            if not _NAME.match(name):
                problems.append(f"{path}: '{stem}' is not a command name - "
                                f"use letters, digits, - and _")
                continue
            if name in reserved:
                problems.append(f"{path}: /{name} is a built-in command, so this "
                                f"file is not used")
                continue
            if name in found:
                continue            # the project's wins
            try:
                description, body = _read(path)
            except OSError as error:
                problems.append(f"{path}: cannot be read ({error})")
                continue
            if not body:
                problems.append(f"{path}: has no prompt in it")
                continue
            found[name] = {"name": name, "description": description, "body": body,
                           "path": path, "source": source}
    return found, problems


def render(body: str, arguments: str) -> str:
    """The body with what was typed after the command put in."""
    arguments = arguments.strip()
    words = arguments.split()
    used = "$ARGUMENTS" in body or re.search(r"\$[1-9]", body) is not None
    text = body.replace("$ARGUMENTS", arguments)
    text = re.sub(r"\$([1-9])",
                  lambda m: words[int(m.group(1)) - 1] if int(m.group(1)) <= len(words) else "",
                  text)
    if arguments and not used:
        text = f"{text}\n\n{arguments}"
    return text


def expand(line: str, reserved=()) -> tuple:
    """(the message `line` stands for, the command used) - or ("", None) if it names none."""
    if not line.startswith("/"):
        return "", None
    head, _, rest = line[1:].partition(" ")
    name = head.lower()
    if not name or name in reserved:
        return "", None
    found, _ = discover(reserved)
    command = found.get(name)
    if command is None:
        return "", None
    return render(command["body"], rest), command
