"""The terms are shown before the harness can act, and asked only once.

Apache-2.0 governs whether or not anyone reads it. What this gate is for is
that somebody who just ran `pip install aetheris` is told, before the
first turn, that the thing they installed runs shell commands on their computer
at a language model's suggestion.

So the checks are: it asks when it has not been answered, it does not ask twice,
a refusal starts nothing and records nothing, and with no terminal to ask it
refuses rather than hanging or assuming.
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import paths

HOME = tempfile.mkdtemp(prefix="terms-")
os.environ[paths.ENV_VAR] = HOME
os.environ.pop("AETHERIS_ACCEPT_TERMS", None)

from aetheris import terms          # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class FakeStdin:
    """A terminal that answers with `reply`, or one that is not a terminal."""

    def __init__(self, reply, tty=True):
        self.reply, self._tty = reply, tty

    def isatty(self):
        return self._tty

    def readline(self):
        return self.reply


def ask(reply, tty=True):
    """Run `require()` against a fake terminal. Returns (verdict, what it said)."""
    said, real_out, real_in = io.StringIO(), sys.stdout, sys.stdin
    sys.stdin = FakeStdin(reply, tty)
    try:
        sys.stdout = said
        verdict = terms.require(stream=said)
    finally:
        sys.stdout, sys.stdin = real_out, real_in
    return verdict, said.getvalue()


record = os.path.join(HOME, "accepted-terms.json")

# ---------------------------------------------------------------------------
print("--- what it says ---")
check("it says the model runs commands as you",
      "run as you, with your permissions" in terms.TEXT)
check("it says files are written where you started it",
      "creates, edits and deletes files" in terms.TEXT)
check("it says a hosted provider is sent your files",
      "sends" in terms.TEXT and "provider" in terms.TEXT)
check("it says which switches remove the approval prompt",
      "/automode" in terms.TEXT and "allow" in terms.TEXT)
check("it disclaims warranty in the licence's own words",
      'AS IS' in terms.TEXT and "not liable" in terms.TEXT)
check("and points at the licence rather than replacing it",
      "Apache License 2.0" in terms.TEXT and "LICENSE" in terms.TEXT)

print("\n--- refusing starts nothing ---")
verdict, said = ask("n\n")
check("no is no", verdict is False)
check("the terms were shown first", "You are responsible" in said)
check("it says nothing was started", "nothing was started" in said)
check("and no answer is recorded", not os.path.exists(record))

verdict, _ = ask("\n")
check("a bare Enter is not agreement", verdict is False)
check("neither is anything else", ask("maybe\n")[0] is False)
check("still nothing recorded", not os.path.exists(record))

print("\n--- agreeing is remembered ---")
verdict, _ = ask("y\n")
check("yes is yes", verdict is True)
check("the answer is on file", os.path.exists(record))
check("and it is not asked again", terms.accepted() is True)
verdict, said = ask("")            # nothing to read from - must not be needed
check("a later run does not ask", verdict is True)
check("and does not show the terms again", "You are responsible" not in said, said[:60])

print("\n--- a newer version of the terms asks again ---")
kept = terms.TERMS_VERSION
terms.TERMS_VERSION = kept + 1
try:
    check("a bumped version is not covered by the old answer", terms.accepted() is False)
finally:
    terms.TERMS_VERSION = kept
check("and the old answer still stands for the old version", terms.accepted() is True)

print("\n--- with no terminal it refuses rather than guessing ---")
os.remove(record)
verdict, said = ask("y\n", tty=False)
check("a pipe cannot agree by accident", verdict is False)
check("it says how to agree instead", terms.ACCEPT_ENV in said)
check("and nothing was recorded", not os.path.exists(record))

os.environ[terms.ACCEPT_ENV] = "1"
try:
    check("the environment variable does agree", terms.accepted() is True)
    check("and require() passes on it", ask("", tty=False)[0] is True)
finally:
    os.environ.pop(terms.ACCEPT_ENV, None)

print("\n--- a broken record is treated as unanswered, not as yes ---")
with open(record, "w", encoding="utf-8") as f:
    f.write("{ not json")
check("unreadable means ask again", terms.accepted() is False)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("terms checks passed")
