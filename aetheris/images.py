"""Reading an image so a model can look at it.

Three of the four providers here accept images, and so do a good number of
local models - Ollama reports `vision` in a model's capabilities the same way
it reports `tools`, and the 4B model this harness is built around is one of the
ones that has it. So "show it the screenshot" stopped being a thing only the
hosted tools could do, and the only reason it did not work here was that
nothing turned a file into the shape an API wants.

**A message carries paths, not pixels.** An image rides on the message as a
sibling key, `message["images"]`, holding file paths; the bytes are read and
base64-encoded in `providers.py` on the way out and never stored. Two reasons,
and the second is the one that decided it:

* `content` is a string everywhere in this program - `merge_runs` concatenates
  it, `context` trims it, `session` replays it, and invariant 5.3 requires the
  history to be plain text so that one format reaches all of them. A parallel
  key leaves every one of those untouched.
* a session file is written after every turn. A 400KB screenshot is 550KB of
  base64, in every saved copy of a conversation it appears in, forever. The
  path is forty bytes. The cost of that choice is that deleting the file
  afterwards means a resumed session cannot show it again - which is said out
  loud when it happens, rather than being discovered as a strange reply.

**Big images are made smaller rather than refused.** A photograph off a phone
is 12MB and no provider will take it, but the person attaching it is not
thinking about that; they want the harness to deal with it. So it is resized to
`IMAGE_MAX_EDGE` on the long side, which is the size the hosted providers scale
to anyway, and re-encoded. Pillow does the work and is in `requirements.txt`;
without it, an image over the ceiling is refused with the size named, because
sending it would fail further away from the person who could fix it.
"""

import base64
import io
import os

from aetheris import config

try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:                     # resizing is off; the ceiling still holds
    Image = None
    PILLOW_AVAILABLE = False


# What every one of the four providers accepts. Deliberately not "whatever
# Pillow can open": a TIFF that opens locally and is rejected by the API is a
# failure that happens too far from the person who chose the file.
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Magic bytes for the same five, so a screenshot saved as `shot` with no
# extension at all is still recognised for what it is.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def media_type(path: str) -> str:
    """The mime type of an image file, or "" if it is not one we send.

    The extension decides, and the first bytes are the fallback - a file
    downloaded as `download` is still a PNG, and asking is cheap next to
    handing the model a wall of broken text.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension in MEDIA_TYPES:
        return MEDIA_TYPES[extension]
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return ""
    for signature, kind in _SIGNATURES:
        if head.startswith(signature):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def is_image(path: str) -> bool:
    """Whether this file is an image this harness would send."""
    return bool(media_type(path))


def _resized(raw: bytes, kind: str) -> tuple:
    """`raw` scaled to fit the long-edge cap, or unchanged.

    Returns (bytes, media type, note). The type comes back because re-encoding
    changes it: a resized GIF or WEBP leaves here as a PNG, and telling the API
    it is still what it was is how an image arrives as garbage.
    """
    if not PILLOW_AVAILABLE:
        return raw, kind, ""
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            width, height = picture.size
            longest = max(width, height)
            if longest <= config.IMAGE_MAX_EDGE and len(raw) <= config.IMAGE_MAX_BYTES:
                return raw, kind, ""
            scale = min(1.0, config.IMAGE_MAX_EDGE / float(longest or 1))
            size = (max(1, int(width * scale)), max(1, int(height * scale)))
            # A palette or 16-bit image has to become something JPEG or PNG can
            # hold before it is saved, and transparency has to survive: an icon
            # flattened onto black is a different picture.
            picture = picture.convert("RGBA" if "A" in picture.getbands() else "RGB")
            picture = picture.resize(size, Image.LANCZOS)
            buffer = io.BytesIO()
            if kind == "image/jpeg" and picture.mode == "RGB":
                picture.save(buffer, format="JPEG", quality=85, optimize=True)
                became = "image/jpeg"
            else:
                picture.save(buffer, format="PNG", optimize=True)
                became = "image/png"
            smaller = buffer.getvalue()
    except Exception:
        return raw, kind, ""            # unreadable to Pillow; the ceiling decides
    if len(smaller) >= len(raw):
        return raw, kind, ""            # re-encoding made it worse; keep the original
    return smaller, became, (f"{width}x{height} -> {size[0]}x{size[1]}, "
                             f"{_size(len(raw))} -> {_size(len(smaller))}")


def _size(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MB"
    if count >= 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count} bytes"


def encode(path: str) -> tuple:
    """An image as (base64 data, media type, note), or ("", "", why not).

    The note is what happened on the way - a resize, or nothing. The failure
    is a sentence a person can act on, because every caller shows it to one.
    """
    kind = media_type(path)
    if not kind:
        return "", "", f"{os.path.basename(path)} is not an image this harness can send"
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as error:
        return "", "", f"could not read {os.path.basename(path)}: {error}"
    if not raw:
        return "", "", f"{os.path.basename(path)} is empty"

    raw, kind, note = _resized(raw, kind)
    if len(raw) > config.IMAGE_MAX_BYTES:
        why = (f"{os.path.basename(path)} is {_size(len(raw))} and the ceiling "
               f"is {_size(config.IMAGE_MAX_BYTES)}")
        if not PILLOW_AVAILABLE:
            why += " - install Pillow and it would be resized instead"
        return "", "", why
    return base64.b64encode(raw).decode("ascii"), kind, note


def describe(path: str) -> str:
    """One line about an image file, for the line printed when it is attached."""
    kind = media_type(path) or "unknown"
    try:
        size = _size(os.path.getsize(path))
    except OSError:
        size = "unreadable"
    return f"{kind.removeprefix('image/')}, {size}"


# ---------------------------------------------------------------------------
# what the model asked to look at
# ---------------------------------------------------------------------------
#
# `view_image` cannot hand an image back the way a tool hands back text: a tool
# result is a string. So the handler leaves the path here and `llm_client`
# attaches it to the tool-result message it is about to append - the same
# arrangement `verify` uses for a check it wants run after the turn.

_pending: list = []


def want(path: str) -> None:
    """Ask for `path` to ride along on the next tool result."""
    if path not in _pending:
        _pending.append(path)


def take_pending() -> list:
    """Every path asked for since the last call, and forget them."""
    taken = list(_pending)
    _pending.clear()
    return taken


def forget_pending() -> None:
    _pending.clear()
