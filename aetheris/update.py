"""Whether a newer Aetheris is out, and `/update` to install it.

Nothing is installed without being asked. Upgrading the program that is
running changes it under the session - a Windows launcher in use cannot even
be replaced - and an upgrade is a change to somebody's machine, which is theirs
to agree to. So the check only tells, once a day at most and once a session,
and `/update` shows the command it is about to run and asks first.

How it upgrades depends on how it was installed, and getting that wrong is
worse than doing nothing: `pip install --upgrade` into a git checkout's
interpreter installs a second copy beside the one being edited, and inside a
pipx environment there may be no pip at all.
"""
import json
import os
import subprocess
import sys
import threading
import time

from aetheris import __version__
from aetheris import atomic, config, paths
from aetheris.config import S

PACKAGE = "aetheris"
INDEX_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_EVERY = 24 * 3600         # seconds between checks at start-up
CHECK_TIMEOUT = 5
STATE_FILE = paths.state("update.json")

_found = ""                     # a newer version the background check saw
_announced = False


def _version(text: str) -> tuple:
    """`1.10.0` as (1, 10, 0) - compared as numbers, not as text. A suffix
    (`2.0.0rc1`) keeps its number and drops the rest."""
    parts = []
    for piece in str(text).split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits or 0))
    return tuple(parts)


def newer(candidate: str, than: str = __version__) -> bool:
    return bool(candidate) and _version(candidate) > _version(than)


def latest(timeout: float = CHECK_TIMEOUT) -> str:
    """The newest version on PyPI. Raises when it cannot be found out."""
    import requests
    response = requests.get(INDEX_URL, timeout=timeout)
    response.raise_for_status()
    return str(response.json()["info"]["version"])


def how_installed() -> tuple:
    """(kind, command): how this copy is upgraded.

    "git" for a checkout - the command is a suggestion, never run: pulling is a
    decision about a working tree. "pipx" and "pip" are run by `/update`.
    """
    package_dir = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(package_dir)
    if os.path.exists(os.path.join(root, ".git")):
        return "git", ["git", "-C", root, "pull"]
    prefix = os.path.normcase(sys.prefix)
    if "pipx" in prefix and "venvs" in prefix:
        return "pipx", ["pipx", "upgrade", PACKAGE]
    return "pip", [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]


# ---------------------------------------------------------------------------
# the check at start-up
# ---------------------------------------------------------------------------

def _read_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _check() -> None:
    global _found
    state = _read_state()
    seen = str(state.get("latest") or "")
    if time.time() - float(state.get("checked") or 0) >= CHECK_EVERY:
        try:
            seen = latest()
        except Exception:
            return              # offline, or PyPI is down: say nothing
        try:
            paths.ensure_home()
            atomic.write_json(STATE_FILE, {"checked": time.time(), "latest": seen})
        except Exception:
            pass
    if newer(seen):
        _found = seen


def start_check() -> None:
    """Look in the background. Nothing waits on it; `announce` reads it."""
    if not getattr(config, "UPDATE_CHECK", True):
        return
    threading.Thread(target=_check, name="update-check", daemon=True).start()


def announce() -> None:
    """Say once, between turns, that a newer version is out."""
    global _announced
    if _found and not _announced:
        _announced = True
        print(f"  {S.INFO}↑ Aetheris {_found} is out (this is {__version__}) - "
              f"/update installs it.{S.R}\n")


# ---------------------------------------------------------------------------
# /update
# ---------------------------------------------------------------------------

def run(ask) -> None:
    """`/update`: find the newest version, and install it if asked to.

    `ask` puts a question to whoever is driving and returns the answer.
    """
    print(f"  {S.GRAY}Asking PyPI for the newest {PACKAGE}...{S.R}")
    try:
        found = latest()
    except Exception as error:
        print(f"  {S.ERR}✗ Could not find out: {error}{S.R}\n")
        return
    if not newer(found):
        print(f"  {S.OK}✓ {__version__} is the newest.{S.R}\n")
        return
    kind, command = how_installed()
    print(f"  {S.INFO}↑ {found} is out; this is {__version__}.{S.R}")
    if kind == "git":
        print(f"  {S.GRAY}This copy runs from a git checkout, so it is updated "
              f"there:{S.R} {S.WHITE}{' '.join(command)}{S.R}\n")
        return
    print(f"  {S.GRAY}It would run:{S.R} {S.WHITE}{' '.join(command)}{S.R}")
    answer = (ask(f"  {S.INFO}Install it now? (y/N) ›{S.R} ") or "").strip().lower()
    if answer not in ("y", "yes"):
        print(f"  {S.GRAY}Left as it is.{S.R}\n")
        return
    try:
        finished = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"  {S.ERR}✗ It did not run: {error}{S.R}\n")
        return
    if finished.returncode != 0:
        tail = (finished.stderr or finished.stdout or "").strip().splitlines()[-6:]
        print(f"  {S.ERR}✗ It failed (exit {finished.returncode}):{S.R}")
        for line in tail:
            print(f"    {S.GRAY}{line}{S.R}")
        print()
        return
    print(f"  {S.OK}✓ Installed {found}. It takes effect the next time Aetheris "
          f"starts - this session goes on as {__version__}.{S.R}\n")


if __name__ == "__main__":
    print("This file can not run directly.")
