"""Commands of the person's own, run before a tool, after one, and when a turn ends.

The permission rules answer one question about a call - may it run - and only
with yes, no or ask. What they cannot do is *look* at it with a program of the
person's choosing: refuse a `run_cmd` whose command fails a check, format a file
the moment it is written, post a note when a long turn finishes. A hook is that
program.

Two files, both optional, read in this order and both used:

* `~/.aetheris/hooks.json` - the person's own;
* `.hooks.json` in the working directory - the project's. A command in it runs
  on this machine as the person, so it is used only once trusted (`trust.py`),
  exactly as `.mcp.json` is.

The shape:

    {
      "pre_tool":  [{"match": "run_cmd(git push*)", "command": "./check-push"}],
      "post_tool": [{"match": "edit_file", "command": "ruff format \\"$AETHERIS_FILES\\""}],
      "turn_end":  [{"command": "notify-send aetheris done"}]
    }

`match` is a permission rule - `tool` or `tool(pattern)`, matched the way
`/perms` matches - and may be left out to mean every tool. A hook is given the
call as one JSON object on stdin and as `AETHERIS_EVENT`, `AETHERIS_TOOL` and
`AETHERIS_FILES` (the files the call names, one per line) in its environment.
The environment has no provider keys in it, as `run_cmd`'s has none.

A `pre_tool` hook that exits non-zero refuses the call, and what it printed is
the reason the model is given. A `post_tool` hook cannot take back what already
ran, so a non-zero exit adds what it printed to the result instead - a formatter
that found a syntax error is worth telling the model about. A `turn_end` hook is
told the turn's outcome and only ever prints.
"""

import json
import os
import subprocess

from aetheris import config
from aetheris import paths

EVENTS = ("pre_tool", "post_tool", "turn_end")
PROJECT_CONFIG_FILE = ".hooks.json"


def user_config_file() -> str:
    """Read at call time: the tests move the home."""
    return paths.state("hooks.json")


def config_paths() -> list:
    """(source, path) pairs, in the order their hooks run."""
    return [("user", user_config_file()),
            ("project", os.path.abspath(PROJECT_CONFIG_FILE))]


class Hook:
    __slots__ = ("event", "match", "command", "source")

    def __init__(self, event: str, match: str, command: str, source: str):
        self.event, self.match, self.command, self.source = event, match, command, source


_hooks: list = []
_loaded = False
errors: list = []
# Project files that are there but not trusted, so none of their hooks run.
untrusted: list = []


def load(force: bool = False) -> list:
    """Every hook from every file that may be used. Never raises."""
    global _loaded
    if _loaded and not force:
        return _hooks
    from aetheris import permissions, trust
    _hooks.clear()
    errors.clear()
    untrusted.clear()
    for source, path in config_paths():
        if not os.path.isfile(path):
            continue
        if source == "project" and not trust.is_trusted(path):
            untrusted.append(path)
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            errors.append(f"{path}: cannot be read ({error})")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path}: does not hold a JSON object")
            continue
        for event, entries in data.items():
            if event not in EVENTS:
                errors.append(f"{path}: '{event}' is not an event - "
                              f"use {', '.join(EVENTS)}")
                continue
            if not isinstance(entries, list):
                errors.append(f"{path}: {event} is not a list")
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("command"), str) \
                        or not entry["command"].strip():
                    errors.append(f"{path}: a {event} entry has no command")
                    continue
                match = entry.get("match", "*")
                if not isinstance(match, str) or not match.strip():
                    match = "*"
                match = match.strip()
                problem = permissions.rule_problem(match) if match != "*" else ""
                if problem:
                    errors.append(f"{path}: {event} match '{match}' ignored - {problem}")
                    continue
                _hooks.append(Hook(event, match, entry["command"].strip(), path))
    _loaded = True
    return _hooks


def hooks_for(event: str, tool: str = "", arguments: dict | None = None) -> list:
    """The hooks that apply to this event - and, for a tool event, this call."""
    if not config.HOOKS_ENABLED:
        return []
    from aetheris import permissions
    chosen = []
    for hook in load():
        if hook.event != event:
            continue
        if event != "turn_end" and hook.match != "*":
            target = permissions.target_for(arguments or {})
            if not permissions.matches(hook.match, tool, target):
                continue
        chosen.append(hook)
    return chosen


def _files_named(tool: str, arguments: dict) -> list:
    from aetheris import tools
    try:
        return tools._target_paths(tool, arguments)
    except Exception:
        return []


def _trim(text: str) -> str:
    limit = max(200, int(config.HOOK_OUTPUT_CHARS))
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n...[hook output trimmed]"


def _run(hook: Hook, payload: dict, files: list) -> tuple:
    """(exit status, what it printed). A hook that cannot start is a failure."""
    from aetheris import providers
    environment = providers.child_environment()
    environment.update({"AETHERIS_EVENT": payload.get("event", ""),
                        "AETHERIS_TOOL": payload.get("tool", ""),
                        "AETHERIS_FILES": "\n".join(files)})
    try:
        done = subprocess.run(
            hook.command, shell=True, input=json.dumps(payload, ensure_ascii=False,
                                                       default=str),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(1, int(config.HOOK_TIMEOUT)), env=environment,
            cwd=os.getcwd())
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {config.HOOK_TIMEOUT}s"
    except OSError as error:
        return 127, f"could not start: {error}"
    printed = "\n".join(part for part in (done.stdout, done.stderr) if part and part.strip())
    return done.returncode, _trim(printed)


def before_tool(tool: str, arguments: dict) -> str:
    """The refusal a `pre_tool` hook gave this call, or "" to let it run."""
    hooks = hooks_for("pre_tool", tool, arguments)
    if not hooks:
        return ""
    files = _files_named(tool, arguments)
    payload = {"event": "pre_tool", "tool": tool, "arguments": arguments,
               "files": files, "cwd": os.getcwd()}
    for hook in hooks:
        status, printed = _run(hook, payload, files)
        if status != 0:
            reason = printed or f"it exited with status {status}"
            return (f"{config.TOOL_REFUSAL_PREFIX} '{tool}' was stopped by a pre_tool hook "
                    f"({hook.command}): {reason}\nDo not retry the same call; change it "
                    "to satisfy the hook, or tell the user it was stopped.")
    return ""


def after_tool(tool: str, arguments: dict, result) -> str:
    """What the `post_tool` hooks that failed had to say, or ""."""
    hooks = hooks_for("post_tool", tool, arguments)
    if not hooks:
        return ""
    files = _files_named(tool, arguments)
    payload = {"event": "post_tool", "tool": tool, "arguments": arguments,
               "files": files, "result": result if isinstance(result, str) else "",
               "cwd": os.getcwd()}
    said = []
    for hook in hooks:
        status, printed = _run(hook, payload, files)
        if status != 0:
            said.append(f"[Hook] {hook.command} exited {status}"
                        + (f":\n{printed}" if printed else ""))
    return "\n".join(said)


def turn_ended(outcome: str) -> list:
    """Run the `turn_end` hooks. Returns (command, status, printed) for each."""
    reports = []
    payload = {"event": "turn_end", "outcome": outcome, "cwd": os.getcwd()}
    for hook in hooks_for("turn_end"):
        status, printed = _run(hook, payload, [])
        reports.append((hook.command, status, printed))
    return reports


def summary(path: str) -> list:
    """What trusting a hooks file would let it run, as approval-box rows."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception as error:
        return [("file", path), ("problem", f"cannot be read ({error})")]
    rows = [("file", path)]
    if isinstance(data, dict):
        for event, entries in data.items():
            for entry in entries if isinstance(entries, list) else ():
                if isinstance(entry, dict) and entry.get("command"):
                    match = entry.get("match") or "*"
                    rows.append((f"runs on {event} {match}", str(entry["command"])))
    return rows
