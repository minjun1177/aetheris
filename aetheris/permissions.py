"""Tool permission rules: what runs without asking, and what never runs.

Until now the only gate was the approval prompt, and `/automode on` turned it
off for everything at once - including `run_cmd` and `delete_file`. Rules give
the middle ground: wave through the calls you make twenty times a day, and put
a hard stop in front of the ones you never want.

Rules live in `.permissions.json` (project) and `~/.aetheris/permissions.json`
(personal); both are read and their rules combined.

    {
      "allow": ["read_file", "run_cmd(git status)", "mcp__github__*"],
      "deny":  ["delete_file", "run_cmd(rm *)", "write_file(*/.env)"]
    }

A rule is a tool name, optionally followed by a pattern in parentheses that is
matched against the call's main argument - the command for `run_cmd`, the path
for a file tool, the URL for a network tool. Both halves accept `*` and `?`
wildcards. A pattern with no wildcard also matches anything that starts with it
followed by a space, so `run_cmd(git status)` covers `git status --short`.

`deny` wins over `allow`, and anything unmatched falls through to the approval
prompt exactly as before - an empty rule set changes nothing.
"""

import json
import os
from fnmatch import fnmatch

from aetheris import atomic
from aetheris import paths


PROJECT_CONFIG_FILES = (".permissions.json", "permissions.json")
USER_CONFIG_FILE = paths.state("permissions.json")

VERDICTS = ("deny", "allow")

# The argument that identifies *what* a call touches, in the order tools use.
_TARGET_KEYS = ("command", "filepath", "dirpath", "src", "url", "uri", "query",
                "paths", "name", "id")

# Arguments a `deny` rule is also matched against, beyond the one target. See
# `_all_targets`. `dst` is the path `copy_file` *writes*, and until this it was
# the one thing about a call that no rule could reach.
_EXTRA_DENY_KEYS = ("dst",)

_rules: dict[str, list[tuple[str, str]]] = {}
_sources: list[str] = []
_loaded = False

errors: list[str] = []


def config_paths() -> list[tuple[str, str]]:
    """(source label, path) pairs, highest precedence first."""
    paths = [("project", os.path.abspath(name)) for name in PROJECT_CONFIG_FILES]
    paths.append(("user", USER_CONFIG_FILE))
    return paths


def load_rules(force: bool = False) -> dict[str, list[tuple[str, str]]]:
    """Read the rule files. Rules from every file apply; deny always wins."""
    global _loaded
    if _loaded and not force:
        return _rules

    _rules.clear()
    _rules.update({verdict: [] for verdict in VERDICTS})
    _sources.clear()
    errors.clear()

    seen_project = False
    for source, path in config_paths():
        if not os.path.isfile(path):
            continue
        if source == "project":
            if seen_project:
                continue        # .permissions.json wins over permissions.json
            seen_project = True
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            errors.append(f"{path}: invalid JSON ({e})")
            continue
        except Exception as e:
            errors.append(f"{path}: {e}")
            continue

        _sources.append(path)
        for verdict in VERDICTS:
            entries = data.get(verdict)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, str) or not entry.strip():
                    continue
                entry = entry.strip()
                problem = rule_problem(entry)
                if problem:
                    errors.append(f"{path}: {verdict} rule '{entry}' ignored - {problem}")
                    continue
                _rules[verdict].append((entry, path))

    _loaded = True
    return _rules


def rule_sources() -> list[str]:
    load_rules()
    return list(_sources)


def rules_for(verdict: str) -> list[tuple[str, str]]:
    return list(load_rules().get(verdict, [])) + _held.get(verdict, [])


# ---------------------------------------------------------------------------
# rules that live for part of a session
# ---------------------------------------------------------------------------
# A rule the user turned on for one request - `/tdd` locking the test files -
# is not a preference and does not belong in their `.permissions.json`. It
# lives here instead: same matching, same `deny`-wins precedence, gone when it
# is released. Nothing writes it to disk, so a crash cannot leave a project
# locked in a way its owner never asked for and cannot see.

_held: dict[str, list[tuple[str, str]]] = {}


def hold(verdict: str, rules, label: str) -> None:
    """Add rules for now. `label` is what `/perms` shows as their source."""
    if verdict not in VERDICTS:
        return
    _held.setdefault(verdict, []).extend(
        (rule, label) for rule in rules if isinstance(rule, str) and rule.strip())


def release(label: str = "") -> list:
    """Drop held rules - all of them, or just one label's. Returns what went."""
    dropped = []
    for verdict, entries in _held.items():
        keep = [entry for entry in entries if label and entry[1] != label]
        dropped += [entry for entry in entries if entry not in keep]
        _held[verdict] = keep
    return dropped


def held(label: str = "") -> list:
    """The rules being held, as (verdict, rule, label)."""
    return [(verdict, rule, source)
            for verdict, entries in _held.items() for rule, source in entries
            if not label or source == label]


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def target_for(arguments: dict) -> str:
    """The argument a rule pattern is matched against."""
    return _target_and_key(arguments)[0]


def _target_and_key(arguments: dict) -> tuple[str, str]:
    """The matched argument and the parameter it came from."""
    if not isinstance(arguments, dict):
        return "", ""
    for key in _TARGET_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key
    return "", ""


def _all_targets(arguments: dict) -> list:
    """Every path-ish argument of a call, not just the first one that answers.

    `copy_file` takes `src` and `dst`, and only `src` is in `_TARGET_KEYS` - so
    `deny write_file(*/.env)` stopped a write to that path and `copy_file` put
    a file there anyway. `tools._WRITES_FILES` has always said `dst` is the one
    `copy_file` writes, which is what auto-commit and the agent channel act on;
    the rules were the only part that disagreed.

    Used for `deny` only. Denying on any argument can refuse more than before
    and never allows more, so no rule anybody has written becomes broader.
    Allow keeps the single target, where "the rule covers exactly the call it
    names" is the whole point (5.6a).
    """
    if not isinstance(arguments, dict):
        return []
    seen, targets = set(), []
    for key in _TARGET_KEYS + _EXTRA_DENY_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in seen:
            seen.add(value.strip())
            targets.append(value.strip())
    return targets


def _normalise(text: str) -> str:
    # Rules are written with forward slashes; Windows paths arrive with both.
    return text.replace("\\", "/").casefold() if os.name == "nt" else text.replace("\\", "/")


def _split_rule(rule: str) -> tuple[str, str]:
    name, sep, pattern = rule.partition("(")
    if not sep:
        return rule.strip(), ""
    return name.strip(), pattern[:-1].strip() if pattern.endswith(")") else pattern.strip()


def rule_problem(rule: str) -> str:
    """Why a rule cannot be used, or "" when it is fine.

    `write_file()` is the one worth catching. It reads to a person as "calls
    with no arguments", and it used to mean the opposite: an empty pattern
    matched every call, so one stray pair of brackets quietly granted
    write_file over every path on the machine. A rule that means "every call"
    has to be written as the bare tool name, where it looks like what it is.
    """
    name, sep, rest = rule.partition("(")
    if not name.strip():
        return "it names no tool"
    if not sep:
        return ""
    if not rest.endswith(")"):
        return "its bracket is never closed"
    if not rest[:-1].strip():
        return ("an empty pattern would match every call - write it as "
                f"'{name.strip()}' if that is what you meant")
    return ""


# Everything the shell reads as "and then do this too". A rule pattern is
# matched against the command as text, so `run_cmd(git status)` also matches
# `git status && rm -rf ~`: the pattern covers the prefix and the shell runs
# whatever follows. Nobody writing that rule meant to allow the second command.
_SHELL_OPERATORS = (";", "&", "|", "`", "$(", "${", ">", "<", "\n", "\r")


def chains_a_second_command(pattern: str, target: str) -> bool:
    """True when the command does more than the rule's pattern accounts for.

    Only an operator the pattern does not itself contain counts, so a rule
    written deliberately as `run_cmd(* | grep *)` still works. This downgrades
    an `allow` to the approval prompt; it never turns an `ask` into a `deny`,
    and it leaves `deny` alone - a deny that matches should keep matching.
    """
    return any(op in target and op not in pattern for op in _SHELL_OPERATORS)


def matches(rule: str, tool: str, target: str) -> bool:
    name, pattern = _split_rule(rule)
    if not fnmatch(_normalise(tool), _normalise(name)):
        return False
    if not pattern:
        return True
    if not target:
        return False
    subject, wanted = _normalise(target), _normalise(pattern)
    if fnmatch(subject, wanted):
        return True
    # A pattern with no wildcard also covers "<pattern> <anything>", so a rule
    # for `git status` does not have to be rewritten to allow `git status -s`.
    if not any(c in wanted for c in "*?[") and fnmatch(subject, wanted + " *"):
        return True
    return False


def decide(tool: str, arguments: dict) -> tuple[str, str]:
    """Return ("deny"|"allow"|"ask", the rule that decided it)."""
    from aetheris import config
    if not getattr(config, "PERMISSIONS_ENABLED", True):
        return "ask", ""

    target, key = _target_and_key(arguments)
    for verdict in VERDICTS:               # deny is checked first and wins
        # A deny is asked about every path the call touches; an allow only
        # about the one target. See `_all_targets`.
        subjects = _all_targets(arguments) if verdict == "deny" else [target]
        for rule, _source in rules_for(verdict):
            if not any(matches(rule, tool, subject) for subject in subjects or [""]):
                continue
            if (verdict == "allow" and key == "command"
                    and chains_a_second_command(_split_rule(rule)[1], target)):
                # The rule covers the command it names; it does not cover
                # whatever the shell was told to run afterwards. Fall through
                # to the prompt rather than waving the whole line through.
                continue
            return verdict, rule
    return "ask", ""


def suggest_rule(tool: str, arguments: dict) -> str:
    """The rule that would allow exactly this call - offered at the prompt."""
    target = target_for(arguments)
    return f"{tool}({target})" if target else tool


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------

def add_rule(rule: str, verdict: str = "allow") -> tuple[bool, str]:
    """Append a rule to the project file. Returns (ok, message)."""
    rule = (rule or "").strip()
    if not rule:
        return False, "an empty rule cannot be saved"
    problem = rule_problem(rule)
    if problem:
        return False, f"'{rule}' cannot be saved - {problem}"
    if verdict not in VERDICTS:
        return False, f"unknown verdict '{verdict}'"

    path = os.path.abspath(PROJECT_CONFIG_FILES[0])
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            return False, f"{path} could not be read ({e})"
    if not isinstance(data, dict):
        return False, f"{path} does not hold a JSON object"

    entries = data.get(verdict)
    if not isinstance(entries, list):
        entries = []
    if rule in entries:
        return True, f"already listed in {path}"
    entries.append(rule)
    data[verdict] = entries

    try:
        atomic.write_json(path, data)
    except Exception as e:
        return False, f"{path} could not be written ({e})"

    load_rules(force=True)
    return True, path


if __name__ == "__main__":
    print("This file can not run directly.")
