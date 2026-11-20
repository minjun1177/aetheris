"""Which project config files the person has said they trust.

`.mcp.json` and `.permissions.json` are read from the working directory, and the
working directory is very often somebody else's repository - just cloned, never
read. Both files are instructions to this harness, not data:

* `.mcp.json` names commands that are started as MCP servers the moment the
  harness starts, with `${VAR}` filled in from the person's own environment -
  so a repository could run anything, or send an API key to any URL;
* `.permissions.json` can `allow` a tool outright - `"allow": ["run_cmd"]` and
  every command the model sends runs without a question.

So neither is used until the person has said, once, that this file is theirs to
trust. The answer is kept in `~/.aetheris/trusted-configs.json` (or the legacy
home, wherever `paths.home()` is) against the file's path *and* its contents: a
`git pull` that changes the file changes what was trusted, and it is asked about
again.

"No" is kept for the session only. A repository whose file was refused is asked
about again next time, so changing one's mind needs no command and no editing of
a state file.

Stdlib only, plus `atomic` and `paths`, so both config readers can import it.
"""

import hashlib
import os

from aetheris import atomic
from aetheris import paths

TRUST_FILE_NAME = "trusted-configs.json"

# Decisions made in this process, by `_key(path)`: True or False. Checked before
# the file on disk, so a "no" holds for the session without being saved.
_session: dict = {}


def trust_file() -> str:
    """Where the trusted fingerprints are kept. Read at call time: tests move HOME."""
    return paths.state(TRUST_FILE_NAME)


def _key(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def fingerprint(path: str) -> str:
    """sha256 of the file's bytes, or "" when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return ""


def _load() -> dict:
    import json
    try:
        with open(trust_file(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def decision(path: str):
    """True (trusted), False (refused this session) or None (never asked).

    A trust saved for different contents than the file has now is None: the
    file that was trusted is not the file that is there.
    """
    key = _key(path)
    if key in _session:
        return _session[key]
    saved = _load().get(key)
    if isinstance(saved, dict) and saved.get("sha256"):
        if saved["sha256"] == fingerprint(path):
            return True
    return None


def is_trusted(path: str) -> bool:
    return decision(path) is True


def remember(path: str, trusted: bool) -> str:
    """Record the person's answer. "" on success, otherwise what went wrong.

    A yes is saved with the file's current fingerprint; a no is held for this
    session only, and drops any older yes for the file from disk.
    """
    key = _key(path)
    _session[key] = bool(trusted)
    files = _load()
    if trusted:
        digest = fingerprint(path)
        if not digest:
            return f"{path} could not be read"
        files[key] = {"sha256": digest}
    elif key in files:
        files.pop(key)
    else:
        return ""
    try:
        paths.ensure_home()
        atomic.write_json(trust_file(), {"files": files}, private=True)
    except Exception as error:          # the answer still holds for the session
        return f"{trust_file()} could not be written ({error})"
    return ""


def forget_session() -> None:
    """Drop this session's answers. For tests, and for nothing else."""
    _session.clear()
