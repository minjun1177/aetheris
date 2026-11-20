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

A project file is somebody's repository talking, so its `allow` rules count
only once the person has trusted that file (`trust.py`). Its `deny` rules
always count: a rule that can only refuse more is safe to take from anyone.
"""

import json
import os
from fnmatch import fnmatch

from aetheris import atomic
from aetheris import paths
from aetheris import trust


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

# Project files that are there but not trusted: only their `deny` rules apply.
untrusted: list[str] = []


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
    untrusted.clear()

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

        if not isinstance(data, dict):
            errors.append(f"{path}: does not hold a JSON object")
            continue
        verdicts = VERDICTS
        if source == "project" and not trust.is_trusted(path):
            # Its denies only: a repository nobody has read does not get to
            # wave its own calls through.
            untrusted.append(path)
            verdicts = ("deny",)
        _sources.append(path)
        for verdict in verdicts:
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


# Arguments that name a file. A rule about a path is about the file, not about
# how the model happened to spell it: `.env`, `./.env`, `/abs/proj/.env`,
# `docs/../.env` and a symlink to it are all the same file, and a rule matched
# against the text alone was answered differently for each.
_PATH_KEYS = ("filepath", "dirpath", "src", "dst", "file_path")


def _forms(path: str, base: str = "") -> list:
    """`path` as a rule may name it: relative to `base` (here) when inside, and absolute."""
    absolute = os.path.abspath(path)
    forms = []
    try:
        relative = os.path.relpath(absolute, base or os.getcwd())
    except ValueError:              # another drive, on Windows
        relative = ""
    if relative and relative != os.pardir and not relative.startswith(os.pardir + os.sep):
        forms.append(relative)
    forms.append(absolute)
    return forms


def _spellings(path: str) -> tuple:
    """(as written, resolved): the forms of the path, and of the file it reaches."""
    written = _forms(os.path.normpath(path))
    # The resolved path is measured from the resolved working directory. From
    # the working directory as spelled, a file in `src` was not in `src` on
    # Windows, where the temp directory and many home directories are an 8.3
    # name (`RUNNER~1`) that `realpath` spells out in full: the relative form
    # began with `..`, and `edit_file(src/*)` allowed nothing at all.
    # Absolute first: POSIX `realpath` takes the working directory it is handed
    # for a relative path as already resolved, so both ends must be resolved
    # from the same spelling.
    resolved = _forms(os.path.realpath(os.path.abspath(path)),
                      os.path.realpath(os.getcwd()))
    return written, resolved


def _path_arguments(arguments: dict) -> list:
    if not isinstance(arguments, dict):
        return []
    return [arguments[key].strip() for key in _PATH_KEYS
            if isinstance(arguments.get(key), str) and arguments[key].strip()]


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


# git can be told to run another program from its own command line, and the
# rule that people write first - the one the docs used as the example - is
# `run_cmd(git *)`. Under it `git -c core.pager='sh -c ...' log`, `git config
# alias.x '!...'`, `git rebase --exec ...` and `git submodule foreach ...` were
# all allowed outright. Each of these, unless the rule names it itself, falls
# through to the approval prompt.
_GIT_RUNS_OTHERS = ("-c", "--config-env", "--exec-path", "--upload-pack", "--receive-pack",
                    "--exec", "-x", "--extcmd", "--ext-diff", "--output", "-o",
                    "--open-files-in-pager", "-O", "--textconv", "--tool", "-t")
_GIT_SUBCOMMANDS_THAT_RUN = ("config", "submodule", "bisect", "filter-branch", "filter-repo",
                             "difftool", "mergetool", "daemon", "credential", "hook",
                             "instaweb", "web--browse", "send-email", "archimport", "p4")


def git_runs_another_program(pattern: str, target: str) -> bool:
    """True when a `git` command could run something the rule never mentioned."""
    import shlex
    try:
        words = shlex.split(target, posix=True)
    except ValueError:
        return True                    # unbalanced quotes: nothing here can be trusted
    if not words or os.path.basename(words[0]).lower() not in ("git", "git.exe"):
        return False
    named = set(pattern.split())
    subcommand_seen = False
    for word in words[1:]:
        option = word.split("=", 1)[0]
        if option in _GIT_RUNS_OTHERS and option not in named:
            return True
        if word.startswith("-") and len(word) > 2 and not word.startswith("--") \
                and word[1] in "cxoOt" and word[:2] not in named:
            return True               # `-cfoo=bar`, `-xcmd` - the short form glued on
        if not subcommand_seen and not word.startswith("-"):
            subcommand_seen = True
            if word in _GIT_SUBCOMMANDS_THAT_RUN and word not in named:
                return True
    return False


def matches(rule: str, tool: str, target: str) -> bool:
    name, pattern = _split_rule(rule)
    if not fnmatch(_normalise(tool), _normalise(name)):
        return False
    if not pattern:
        return True
    if not target:
        return False
    subject, wanted = _normalise(target), _normalise(pattern)
    if wanted.startswith("./") and len(wanted) > 2:
        wanted = wanted[2:]
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
    if not config.PERMISSIONS_ENABLED:
        return "ask", ""

    target, key = _target_and_key(arguments)
    paths_given = _path_arguments(arguments)

    # A deny is asked about every argument the call has and every spelling of
    # every path in it: any one matching refuses the call.
    subjects = list(_all_targets(arguments))
    for path in paths_given:
        written, resolved = _spellings(path)
        subjects += [form for form in written + resolved if form not in subjects]
    for rule, _source in rules_for("deny"):
        if any(matches(rule, tool, subject) for subject in subjects or [""]):
            return "deny", rule

    allowed = rules_for("allow")
    if paths_given and key in _PATH_KEYS:
        # Every path the call touches has to be allowed - `copy_file` writes
        # `dst`, which an allow on `src` never looked at - and each by what it
        # *is*: the spelling normalised (so `src/../../etc/x` is not "in src/"),
        # and the file a symlink leads to as well as the link.
        decided = ""
        for path in paths_given:
            covering = ""
            for spelling in _spellings(path):
                rule = next((rule for rule, _ in allowed
                             if any(matches(rule, tool, form) for form in spelling)), "")
                if not rule:
                    covering = ""
                    break
                covering = covering or rule
            if not covering:
                return "ask", ""
            decided = decided or covering
        return "allow", decided

    for rule, _source in allowed:
        if not matches(rule, tool, target):
            continue
        if key == "command" and chains_a_second_command(_split_rule(rule)[1], target):
            # The rule covers the command it names; it does not cover
            # whatever the shell was told to run afterwards. Fall through
            # to the prompt rather than waving the whole line through.
            continue
        if key == "command" and git_runs_another_program(_split_rule(rule)[1], target):
            continue
        return "allow", rule
    return "ask", ""


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
    existed = os.path.isfile(path)
    if existed and not trust.is_trusted(path):
        # Saving would mean trusting the whole file afterwards - including
        # whatever `allow` rules the repository put there - on the strength of
        # one rule the person wrote. They decide about the file first.
        return False, (f"{path} is not trusted, so nothing is added to it. "
                       f"Restart and trust it, or add the rule by hand.")
    data = {}
    if existed:
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

    # The person wrote this file's new contents themselves, through this
    # harness: the new fingerprint is the trusted one.
    trust.remember(path, True)
    load_rules(force=True)
    return True, path
