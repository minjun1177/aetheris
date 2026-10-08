"""API keys in the operating system's keyring, when it has one.

`providers.json` is owner-only, which keeps other accounts on the machine out
of it. It does not keep out what runs as the owner: a plain JSON file in a
well-known place is the first thing any script, any other assistant and any
model with a shell will find. The keyring - macOS Keychain, Windows Credential
Manager, the Secret Service on a Linux desktop - takes the key out of that file
and leaves a marker in its place:

    "anthropic": {"model": "...", "key_store": "keyring"}

**Not a wall.** A process running as the same user can still ask the keyring
for the key through its API, and on most desktops it is handed over without a
prompt. What this removes is the plaintext sitting in a file - which is what
gets read by accident, copied into a backup, or pasted into a bug report.
Keeping the *model* from reading the key is done elsewhere, by the vault and
`tools.dispatch_tool`, and does not depend on this.

**Optional, and never required.** `pip install "aetheris[keyring]"`. Without
the package, with no usable backend (WSL, a server, a container), or with
`AETHERIS_KEYRING=off`, keys stay in `providers.json` at 0600 exactly as
before. A key the keyring refuses to store is written to the file rather than
lost.

One entry per provider and per Aetheris home, so a second home set with
`AETHERIS_HOME` - a test run, a portable install - never reads or overwrites
the first one's keys.
"""

import os

SERVICE = "aetheris"
ENV_VAR = "AETHERIS_KEYRING"
INSTALL_HINT = 'pip install "aetheris[keyring]"'

_OFF = ("off", "0", "no", "false")

# The module once it has been found usable, False once it has been found not
# to be, None before anybody asked. Asking is not free: the Secret Service is a
# D-Bus round trip, and a missing one can take a moment to say so.
_usable = None

# What was last read from or written to the keyring, by entry. Lets a save
# that changed nothing about a key skip the keyring altogether - `save_state`
# runs on every /set and every model switch.
_known: dict = {}


def switched_off() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() in _OFF


def _keyring():
    """The `keyring` module, when it is installed and has a real store behind it."""
    global _usable
    if switched_off():
        return None
    if _usable is None:
        try:
            import keyring
            backend = keyring.get_keyring()
            # Priority below 1 is the failure backend (nothing found) or one of
            # the plaintext-file backends from `keyrings.alt` - neither is
            # better than the 0600 file this would be replacing.
            _usable = keyring if float(getattr(backend, "priority", 0) or 0) >= 1 else False
        except Exception:
            _usable = False
    return _usable or None


def available() -> bool:
    return _keyring() is not None


def backend_name() -> str:
    """What the keys are kept in, for `/connect` to say. "" when not the keyring."""
    module = _keyring()
    if module is None:
        return ""
    try:
        return str(getattr(module.get_keyring(), "name", "") or "").strip() or "system keyring"
    except Exception:
        return "system keyring"


def _entry(provider: str, where: str) -> str:
    return f"{provider} @ {os.path.dirname(os.path.abspath(where))}"


def get(provider: str, where: str) -> str:
    """The stored key, or "" when there is none or the keyring cannot be asked."""
    module = _keyring()
    if module is None:
        return ""
    entry = _entry(provider, where)
    try:
        value = module.get_password(SERVICE, entry) or ""
    except Exception:
        return ""
    if value:
        _known[entry] = value
    return value


def put(provider: str, where: str, key: str) -> bool:
    """Store `key`. False when it could not be, and the caller keeps the file."""
    module = _keyring()
    if module is None or not key:
        return False
    entry = _entry(provider, where)
    if _known.get(entry) == key:
        return True
    try:
        module.set_password(SERVICE, entry, key)
    except Exception:
        return False
    _known[entry] = key
    return True


def delete(provider: str, where: str) -> bool:
    module = _keyring()
    entry = _entry(provider, where)
    _known.pop(entry, None)
    if module is None:
        return False
    try:
        module.delete_password(SERVICE, entry)
        return True
    except Exception:
        return False        # nothing stored there is the usual reason


if __name__ == "__main__":
    print("This file can not run directly.")
