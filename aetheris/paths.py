"""Where the harness keeps what outlives a session.

Everything personal - the conversations, the long-term memory, the input
history, the saved API keys - lives under one directory, `~/.aetheris`.

It used to be split. Keys went to the home directory, and `sessions/`,
`memory.json` and `.chat_history` were written into whatever directory the
harness happened to start in. That was survivable while it was run as
`python app.py` from its own checkout, and stopped being survivable the moment
it became a command you can run anywhere: starting it in a home directory left
files there, starting it in two projects gave you two unrelated memories, and
`/sessions` only ever listed the ones belonging to wherever you were standing.

What stays per-directory is the part that is genuinely about a project rather
than about you: `.permissions.json`, `.mcp.json` and `skills/`, each of which
is read from the working directory first and from here second.

`AETHERIS_HOME` overrides the location - useful for keeping two profiles
apart, and how the tests get a directory of their own instead of the real one.

Both names were `localchat` until 1.0.0, and a rebrand is no reason for
somebody to lose their sessions. So the old names still work and the old
directory still wins where it exists: if `~/.localchat` is already there and
`~/.aetheris` is not, that is the home, untouched and unmoved. A person who
wants the new name moves the directory themselves, once, and nothing else has
to know. `LOCALCHAT_HOME` is read when `AETHERIS_HOME` is unset, so a shell
profile or a CI job that sets the old one keeps pointing where it pointed.

Stdlib only, and it imports nothing local. `config` builds the system prompt at
import time (ARCHITECTURE 5.2), so the modules below it cannot import `config`;
they can import this.
"""

import hashlib
import os
import re

ENV_VAR = "AETHERIS_HOME"
DIR_NAME = ".aetheris"

# The names before 1.0.0. Read, never written: a fresh install gets the new
# directory, an existing one is left exactly where the person already has it.
LEGACY_ENV_VAR = "LOCALCHAT_HOME"
LEGACY_DIR_NAME = ".localchat"

# Written into the working directory by versions before this one.
LEGACY_IN_CWD = ("memory.json", "sessions", ".chat_history")


def home() -> str:
    """The directory holding everything that outlives a session.

    `AETHERIS_HOME` first, then the pre-1.0.0 `LOCALCHAT_HOME`, then
    `~/.aetheris` - except where `~/.localchat` exists and `~/.aetheris` does
    not, which is every install that predates the rename and keeps working
    without being asked to move anything.
    """
    override = (os.environ.get(ENV_VAR, "").strip()
                or os.environ.get(LEGACY_ENV_VAR, "").strip())
    if override:
        return os.path.abspath(os.path.expanduser(override))
    user = os.path.expanduser("~")
    current = os.path.join(user, DIR_NAME)
    legacy = os.path.join(user, LEGACY_DIR_NAME)
    if not os.path.isdir(current) and os.path.isdir(legacy):
        return legacy
    return current


def state(*parts: str) -> str:
    """A path inside `home()`. Nothing is created; the writer does that."""
    return os.path.join(home(), *parts)


def ensure_home() -> str:
    """`home()`, created if it is not there yet. Returns it either way."""
    directory = home()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass          # the writer will report it with the file it was writing
    return directory


# ---------------------------------------------------------------------------
# naming a file after something that was not chosen to be a filename
# ---------------------------------------------------------------------------

# Illegal on Windows, and `/` would silently make a name into a path.
_FS_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Names Windows will not give a file whatever the extension. `CON.md` is not a
# file there, and the failure comes back as a permission error from `open`.
_WINDOWS_RESERVED = ({"CON", "PRN", "AUX", "NUL", "CLOCK$"}
                     | {f"COM{i}" for i in range(1, 10)}
                     | {f"LPT{i}" for i in range(1, 10)})


def safe_name(text: str, limit: int = 48, reserved_suffix: str = "x") -> str:
    """A filename for `text`, or "" when nothing usable is left of it.

    Session titles and note ids are both written by a model, in any script, and
    both end up as a filename. Letters and digits survive in whatever language
    they are in - a Korean note id stays readable rather than becoming a hash -
    and everything the filesystem would object to does not.

    `reserved_suffix` is appended when the result is a name Windows reserves,
    which is the one case where a legal-looking name still cannot be created.
    """
    slug = _FS_UNSAFE.sub(" ", text or "")
    slug = re.sub(r'[^\w\s-]', '', slug, flags=re.UNICODE)
    slug = re.sub(r'\s+', '-', slug.strip())
    slug = re.sub(r'-{2,}', '-', slug).strip('-._')
    slug = slug[:limit].strip('-._').lower()
    if not slug:
        return ""
    if slug.split('.')[0].upper() in _WINDOWS_RESERVED:
        slug = f"{slug}-{reserved_suffix}"
    return slug


def workspace_slug(place: str) -> str:
    """A directory's own name, plus a digest of the path it sits at.

    The readable half is so a person looking in `~/.aetheris` can tell which
    project something belongs to; the digest is because two different projects
    are routinely both called `chat`. Used for the channel board and for a
    project's notes, which must agree on what counts as one project.
    """
    digest = hashlib.sha1(os.path.normcase(place).encode("utf-8", "replace"))
    readable = re.sub(r"[^\w.-]", "-", os.path.basename(place.rstrip(os.sep)))
    return f"{readable[:32] or 'workspace'}-{digest.hexdigest()[:10]}"


def strays_in_cwd() -> list:
    """Names in the working directory left by an older version, if any.

    Only reported, never touched. `sessions` is an ordinary enough directory
    name that moving one on sight would eventually destroy somebody's actual
    work - so this says what it found and lets the person decide.
    """
    return [name for name in LEGACY_IN_CWD if os.path.exists(name)]
