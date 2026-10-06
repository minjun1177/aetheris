"""`@path` in a typed message, and what it puts in front of the model.

Other harnesses spell this `@file`, and it earns its place here for a reason
particular to small local models: the alternative is asking the model to call
`read_file`, which is a whole extra round trip - a reply, a parse, a dispatch,
another reply - to fetch something the person already knew they wanted. On a
model running on your own GPU that round trip is seconds, and it is the one
place where the user knows the answer better than the model does.

The mention stays in the sentence where it was typed, because that is where it
reads: "explain @app.py" is a sentence, and cutting the token out of it leaves
the model a dangling "explain". The contents arrive underneath, once each,
however many times the file was named.

A miss is never fatal. A path that does not exist is reported and the message is
sent as written - the model can still answer, and stopping a turn because of a
typo in one token would cost more than the typo does.

Stdlib, `config` and `images` only, so this can be tested without a terminal.
"""

import os
import re

from aetheris import config
from aetheris import images

# `@` starts a mention only at the start of the line or after whitespace, so an
# email address and a decorator in pasted code are left alone. The token runs to
# the next space; quoting is handled below for paths that contain one.
_MENTION = re.compile(r'(?:^|(?<=\s))@("[^"]+"|\'[^\']+\'|[^\s]+)')

# Punctuation that ended a sentence rather than a filename. Only stripped when
# what is left names something real and the original did not.
_SENTENCE_TAIL = ".,;:!?)]}\"'"


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _exists(path: str) -> bool:
    """`os.path.exists`, minus the lie Windows tells about trailing punctuation.

    Win32 strips trailing dots and spaces off a name before looking it up, so
    `config.txt.` reports as existing when only `config.txt` does. That is the
    exact shape a full stop at the end of a sentence makes, and taking it at
    face value means the file is attached - and then reported back to the user -
    under a name that is not on disk.
    """
    if os.name == "nt":
        name = os.path.basename(path.replace("\\", "/").rstrip("/"))
        if name not in (".", "..") and name[-1:] in (".", " "):
            return False
    return os.path.exists(path)


def resolve(token: str) -> str:
    """The path a mention names, allowing for punctuation that closed a sentence."""
    path = _unquote(token)
    if _exists(path):
        return path
    trimmed = path.rstrip(_SENTENCE_TAIL)
    if trimmed and trimmed != path and _exists(trimmed):
        return trimmed
    return path          # report the miss against what was actually typed


def find(text: str) -> list[str]:
    """Every path mentioned with `@`, in order, without repeats."""
    seen, paths = set(), []
    for match in _MENTION.finditer(text or ""):
        path = resolve(match.group(1))
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _read(path: str) -> tuple[bool, str]:
    """One attachment's body. Returns (attached, text).

    `read_file` and `list_dir` do the work, so a mention shows exactly what the
    model would have seen had it fetched the thing itself - the same hashlines,
    so it can edit what it was shown, and the same directory format.
    """
    from aetheris import tools

    if os.path.isdir(path):
        body = tools.handle_list_dir(path)
    elif os.path.exists(path):
        body = tools.handle_read_file(path)
    else:
        return False, "no such file or directory"

    if body.startswith(config.TOOL_ERROR_PREFIX):
        return False, body[len(config.TOOL_ERROR_PREFIX):].strip()

    # `@.env` reaches `read_file` directly rather than through `dispatch_tool`,
    # so the redaction that happens there would be missed - and an attachment
    # goes to the provider and into the session file exactly like a tool result
    # does. Typing `@.env` is the person's own choice; sending them their keys
    # back is not what they were choosing.
    from aetheris import vault
    body = vault.redact(body)

    cap = config.MENTION_MAX_CHARS
    if cap and len(body) > cap:
        # A mention is one keystroke and can name a file of any size. The
        # context is the scarce thing here, so the cut happens now, with a line
        # saying so, rather than as a silent eviction three turns later.
        body = body[:cap] + (f"\n...[{path} is {len(body)} characters; "
                             f"the first {cap} are shown. Use read_file for the rest.]")
    return True, body


def expand(text: str) -> tuple[str, list[tuple[str, bool, str]], list[str]]:
    """The message to send, one (path, attached, note) per mention, and images.

    The caller displays the notes; nothing here prints, so the expansion can be
    tested and so a sub-agent could use it without writing to the terminal.

    An image does not become text. `@shot.png` read as a file is a screenful of
    broken bytes that tells the model nothing and costs a thousand tokens to
    say it, so the path comes back separately and rides on the message itself
    (`images.py`) - the mention still stays in the sentence where it was typed,
    because "what is wrong with @shot.png" is a sentence either way.
    """
    paths = find(text)
    if not paths:
        return text, [], []

    notes, blocks, pictures = [], [], []
    for path in paths:
        if not os.path.isdir(path) and images.is_image(path):
            pictures.append(path)
            notes.append((path, True, images.describe(path)))
            continue
        attached, body = _read(path)
        if attached:
            kind = "directory listing" if os.path.isdir(path) else "file"
            blocks.append(f"[Attached {kind}: {path}]\n{body}")
            notes.append((path, True, f"{len(body)} chars"))
        else:
            notes.append((path, False, body))

    if not blocks:
        return text, notes, pictures
    return text.rstrip() + "\n\n" + "\n\n".join(blocks), notes, pictures
