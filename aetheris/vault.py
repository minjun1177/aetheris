"""The values in `.env`, kept out of the model and pasted back in by the harness.

A `.env` file is the one file in a project whose *contents* are the secret. The
model has every reason to want it - it is where the database URL and the API
keys are - and no reason at all to want the values. It needs to know that
`STRIPE_KEY` exists and to be able to *use* it; the sixty characters after the
`=` are for the machine.

Which matters here more than in most places, because a value the model reads
does not stay read:

* it goes to the provider, so a hosted model means the key is now Anthropic's
  or OpenAI's problem too;
* it is written into `~/.aetheris/sessions/*.json` and stays there;
* the summariser may copy it into a `<SUMMARY>` that survives compression.

One `read_file` and the key is in three places it was never meant to be, none
of which the person watching would think to check.

So the harness reads the file and the model does not. What it is handed is

    STRIPE_KEY={{env:STRIPE_KEY}}

and when it sends that placeholder back - in a command, a URL, a header - the
harness puts the real value in on the way to the tool. The model can use a
secret it has never been told.

**What is filled in, and what is not.** `FILLED_IN` names it per tool, and the
rule behind the list is that a placeholder is expanded into what *runs* and
never into what is *saved*. `run_cmd` gets the real key because curl needs it;
`write_file` does not, because the alternative is the model being able to copy a
secret into a file by writing the placeholder and asking for it back. A
placeholder written to a file stays a placeholder, which is also exactly what
`.env.example` should contain.

**What is not covered.** A secret that is not in one of these files - typed
into the chat, invented by a command, pasted by the user - is not known here
and is not redacted. This narrows the biggest hole; it is not a guarantee, and
nothing here should be described to anybody as one.

Stdlib plus `paths`, so it can be imported from `systemprompt` - which `config`
imports at import time - without a cycle.
"""

import os
import re

from aetheris import paths

# `{{env:NAME}}`. Braces because no shell, JSON encoder or markdown renderer
# does anything surprising with them, and a doubled pair does not occur in
# ordinary prose or code by accident.
PLACEHOLDER = "{{env:%s}}"
_PLACEHOLDER_PATTERN = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}")

# Files whose values are treated as secret. `.env.example` and friends are the
# deliberate exception: they exist to be read, and their "values" are the
# placeholders somebody wrote by hand.
FILENAMES = (".env", ".env.local", ".env.development", ".env.production",
             ".env.test", ".envrc")
NEVER = ("example", "sample", "template", "dist", "default")

# A value shorter than this is not worth hiding and is dangerous to hide: a
# `.env` with `ENV=dev` would otherwise turn every "dev" in every tool result
# into a placeholder.
MIN_LENGTH = 8

# ...and neither is a value that is plainly not a secret, however long.
_NOT_SECRET = frozenset((
    "true", "false", "yes", "no", "on", "off", "none", "null", "nil",
    "development", "production", "localhost", "staging", "debug", "info",
    "warning", "error", "verbose", "disabled", "enabled",
))

# `KEY=value`, `export KEY=value`, `KEY: value`. Quotes are stripped, and a
# line without a name is not a setting.
_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$""")

# Which arguments of which tool a placeholder is put back into. Read the way
# `tools._WRITES_FILES` is: the list *is* the rule, and a tool absent from it
# receives the placeholder unchanged.
#
# Everything here is consumed and thrown away. Nothing here writes a file.
FILLED_IN = {
    "run_cmd": ("command", "stdin"),
    "send_input": ("stdin",),
    "run_python": ("content", "stdin"),
    "call_api": ("url", "headers", "payload"),
    "get_url": ("url",),
}

_cache: dict = {}           # path -> (mtime, size, {name: value})


def _cfg(name, default):
    from aetheris import config
    return getattr(config, name, default)


def enabled() -> bool:
    return bool(_cfg("SECRET_REDACT", True))


# ---------------------------------------------------------------------------
# reading the files
# ---------------------------------------------------------------------------

def _worth_hiding(value: str) -> bool:
    """Whether a value is a secret rather than a word that happens to be here."""
    value = value.strip()
    if len(value) < max(1, int(_cfg("SECRET_MIN_LENGTH", MIN_LENGTH))):
        return False
    if value.lower() in _NOT_SECRET:
        return False
    try:
        float(value)
        return False            # a number is a port or a timeout, not a key
    except ValueError:
        return True


def _unquote(value: str) -> str:
    value = value.split(" #", 1)[0].strip() if not value.startswith(("'", '"')) else value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _read(path: str) -> dict:
    """One env file as {name: value}. Cached against its size and mtime."""
    try:
        stamp = os.stat(path)
        key = (stamp.st_mtime_ns, stamp.st_size)
    except OSError:
        _cache.pop(path, None)
        return {}
    cached = _cache.get(path)
    if cached and cached[0] == key:
        return cached[1]

    found = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                matched = _LINE.match(line)
                if not matched:
                    continue
                value = _unquote(matched.group(2))
                if _worth_hiding(value):
                    found[matched.group(1)] = value
    except OSError:
        found = {}
    _cache[path] = (key, found)
    return found


def files() -> list:
    """The env files this workspace has, nearest first.

    The working directory and the git working tree above it, which is the same
    pair `channel.workspace()` calls one place - a terminal opened in `src/`
    and one at the top are working on the same project and the same `.env`.
    """
    here = os.path.abspath(os.getcwd())
    roots = [here]
    try:
        from aetheris import git_ops
        root = git_ops.repo_root(here)
        if root and os.path.abspath(root) != here:
            roots.append(os.path.abspath(root))
    except Exception:
        pass

    names = list(FILENAMES) + [n for n in _cfg("SECRET_FILES", []) if isinstance(n, str)]
    found = []
    for directory in roots:
        for name in names:
            if any(word in name.lower() for word in NEVER):
                continue
            path = os.path.join(directory, name)
            if os.path.isfile(path) and path not in found:
                found.append(path)
    return found


def known() -> dict:
    """Every secret this workspace declares, as {name: value}. Nearest wins."""
    if not enabled():
        return {}
    merged = {}
    for path in files():
        for name, value in _read(path).items():
            merged.setdefault(name, value)
    return merged


def names() -> list:
    return sorted(known())


# ---------------------------------------------------------------------------
# out to the model, and back
# ---------------------------------------------------------------------------

def redact(text):
    """Replace every known secret in `text` with the placeholder for its name.

    Longest first, so a value that contains another - a URL holding a password -
    is replaced whole rather than left with a placeholder embedded in it.
    """
    if not isinstance(text, str) or not text:
        return text
    secrets = known()
    if not secrets:
        return text
    for name, value in sorted(secrets.items(), key=lambda item: -len(item[1])):
        if value and value in text:
            text = text.replace(value, PLACEHOLDER % name)
    return text


def restore(text):
    """Put the real values back where the model wrote a placeholder."""
    if not isinstance(text, str) or "{{env:" not in text:
        return text
    secrets = known()

    def swap(match):
        name = match.group(1)
        return secrets.get(name, match.group(0))

    return _PLACEHOLDER_PATTERN.sub(swap, text)


def _walk(value, change):
    """Apply `change` to every string inside a JSON-shaped value."""
    if isinstance(value, str):
        return change(value)
    if isinstance(value, list):
        return [_walk(item, change) for item in value]
    if isinstance(value, dict):
        return {key: _walk(item, change) for key, item in value.items()}
    return value


def fill_in(tool: str, arguments: dict) -> dict:
    """The arguments a tool actually runs with, placeholders expanded.

    A copy: the caller's dict is what is displayed, committed and written into
    the conversation, and it must keep saying `{{env:NAME}}`. Only the copy
    handed to the handler carries the value.
    """
    keys = FILLED_IN.get(tool)
    if not keys or not enabled() or not isinstance(arguments, dict):
        return arguments
    filled = dict(arguments)
    for key in keys:
        if key in filled:
            filled[key] = _walk(filled[key], restore)
    return filled


def used_in(arguments: dict) -> list:
    """The placeholder names a call carries, for the approval prompt to name."""
    if not isinstance(arguments, dict):
        return []
    found = []

    def note(text):
        for match in _PLACEHOLDER_PATTERN.finditer(text):
            if match.group(1) not in found:
                found.append(match.group(1))
        return text

    _walk(arguments, note)
    return found


# ---------------------------------------------------------------------------
# what the model is told
# ---------------------------------------------------------------------------

def prompt_section() -> str:
    """The paragraph that stops the model trying to "fix" a placeholder.

    Without it a model reads `KEY={{env:KEY}}`, decides the file is broken, and
    helpfully writes what it imagines the real value to be over the top.
    """
    if not enabled():
        return ""
    listed = names()
    if not listed:
        return ""
    return (
        "\n### SECRETS:\n"
        "This project has a .env file. You are shown the names in it and never "
        "the values: every value reads as `{{env:NAME}}`. That is not a broken "
        "file and not a placeholder somebody forgot to fill in - it is the real "
        "value, hidden from you and from the conversation.\n"
        f"Available: {', '.join(listed)}\n"
        "To use one, write `{{env:NAME}}` where the value would go in a "
        "`run_cmd`, `run_python`, `get_url` or `call_api` call - the harness "
        "puts the real value in as it runs, so the command works and you still "
        "never see it. It is NOT filled in when writing a file, so a "
        "placeholder written to a file stays a placeholder.\n"
        "Never guess a value, never ask the user to paste one, and never try to "
        "read one out of a file another way.\n"
    )


if __name__ == "__main__":
    print("This file can not run directly.")
