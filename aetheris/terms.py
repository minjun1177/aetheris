"""What the harness does to the machine it runs on, shown once before it does it.

This is not a second licence. Apache-2.0 already disclaims warranty (section 7)
and limits liability (section 8), and it governs either way - agreeing here adds
nothing to it and refusing takes nothing away. What this adds is that the
disclaimer is *read*: a licence file nobody opens is a poor way to tell someone
that the program they just installed is about to run shell commands on their
computer at a language model's suggestion.

So it says plainly what the harness can do, points at the licence for the legal
part, and asks once. The answer is kept in `~/.aetheris` and never asked again
unless the terms change.

Stdlib plus `atomic` and `paths`, both of which import nothing local.
"""

import json
import os
import sys

from aetheris import atomic
from aetheris import paths

# Raise this only when the terms themselves change, which asks everyone again.
TERMS_VERSION = 1

ACCEPT_ENV = "AETHERIS_ACCEPT_TERMS"
# The name before 1.0.0. An unattended job that already sets it keeps working;
# only the new name is ever printed as the way to do it.
LEGACY_ACCEPT_ENV = "SIMPLE_HARNESS_ACCEPT_TERMS"
_RECORD = "accepted-terms.json"


TEXT = """\
Aetheris gives a language model tools that act on this computer.

  * It runs shell commands, and they run as you, with your permissions.
  * It creates, edits and deletes files in whatever directory you start it in.
  * It makes network requests, and if you connect a hosted provider it sends
    your prompts - including file contents it has read - to that provider.

A language model can be wrong about any of it. The approval prompt is what
stands between a suggestion and it happening, and `/automode on`, an `allow`
rule and a trusted MCP server each remove that prompt for what they cover.

Use version control, or a copy you can afford to lose. `/undo` takes back the
last file change the harness committed; it cannot take back a shell command.

This software is licensed under Apache License 2.0 and is provided "AS IS",
without warranties or conditions of any kind. Its authors and contributors are
not liable for any damage, data loss or other harm arising from its use. The
full text is in the LICENSE file and at apache.org/licenses/LICENSE-2.0 -
sections 7 and 8 are the ones that say this properly.

You are responsible for what you allow it to do."""


def _record_path() -> str:
    return paths.state(_RECORD)


def accepted() -> bool:
    """Has this machine already agreed to the current terms?"""
    for name in (ACCEPT_ENV, LEGACY_ACCEPT_ENV):
        if os.environ.get(name, "").strip().lower() in ("1", "true", "yes"):
            return True
    try:
        with open(_record_path(), "r", encoding="utf-8") as f:
            return int(json.load(f).get("version", 0)) >= TERMS_VERSION
    except Exception:
        return False        # missing, unreadable or not ours - ask again


def record() -> None:
    """Remember the answer. A failure here costs a second prompt, nothing more."""
    try:
        paths.ensure_home()
        atomic.write_json(_record_path(), {"version": TERMS_VERSION,
                                           "accepted": True})
    except Exception:
        pass


def require(stream=None) -> bool:
    """Show the terms and ask. True to carry on, False to stop.

    Nothing is written and nothing is asked once the answer is on file. With no
    terminal to ask - a pipe, a cron job, a container - it refuses rather than
    guessing, and names the environment variable that answers for it. Silence
    is not agreement, but it must not be a hang either.
    """
    if accepted():
        return True

    out = stream or sys.stdout
    print("\n" + TEXT + "\n", file=out)

    # `sys.stdin` is None under pythonw and in some embedded launchers, where
    # asking `isatty()` is itself the crash - before the program has printed
    # anything a person could act on.
    if sys.stdin is None or not sys.stdin.isatty():
        print(f"Agree once in a terminal, or set {ACCEPT_ENV}=1 to agree here.\n",
              file=out)
        return False

    try:
        answer = input("Do you agree? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        return False

    if answer not in ("y", "yes"):
        print("Not agreed - nothing was started.\n", file=out)
        return False

    record()
    return True
