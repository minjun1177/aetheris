"""What the model learned about *this* project, kept as markdown.

Long-term memory (`session.py`) is about the person: their name, how they want
to be worked with, a standing rule that holds wherever they are. It is one flat
store shared by every project, and it is JSON, which is the right shape for a
fact in a sentence and the wrong shape for a paragraph with a list in it.

What it has no room for is the other half of what is worth keeping: why this
repository does the odd thing it does, the order its deploy runs in, the three
questions nobody has answered yet. That knowledge is per project - it is wrong
everywhere else - and it is prose, often with headings and code in it.

So notes are a second store with different rules:

* **Per project.** `channel.workspace()` decides what a project is - the git
  working tree, or the current directory when there is no repository - so a
  terminal opened in `src/` sees the same notes as one opened at the root.
  Two projects called `chat` do not share notes; `paths.workspace_slug` is
  what keeps them apart, and it is the same name the channel board uses.

* **One note is one file.** `~/.aetheris/notes/<project>/<id>.md`. The file
  *is* the record: its name is the id, its mtime is when it was last written,
  and its bytes are the whole content. There is no index beside it to fall out
  of step with what is on disk, and a person can open the directory in any
  editor and have it make sense.

* **Markdown, stored as written.** No frontmatter, no injected heading, no
  escaping on the way in or out. The model writes the body in a `<content>`
  raw block, which is the same route file bodies take and for the same reason:
  a 4B model asked to JSON-escape a fenced code block will get it wrong.

* **Under `~/.aetheris`, not in the project.** These are notes a model wrote
  about someone's repository, and a directory that appears inside it the first
  time the harness is used would be a rude surprise - the same reasoning that
  puts the channel board there (`paths.py`).

What reaches the system prompt is the list of titles and nothing else. The body
of every note would be unbounded prompt paid for on every turn; the titles are
what a fresh session is missing, because a session that does not know a note
exists has no reason to call `read_note`.
"""

import datetime
import os

from aetheris import atomic
from aetheris import channel
from aetheris import config
from aetheris import paths

SUFFIX = ".md"

# Long enough for a sentence-shaped id ("why-the-hashline-has-three-digits"),
# short enough to leave room on every filesystem that caps a path.
ID_MAX_LEN = 48


# ---------------------------------------------------------------------------
# where the notes are
# ---------------------------------------------------------------------------

def project_dir() -> str:
    """The directory holding this project's notes. Not created by reading."""
    return os.path.join(config.NOTES_DIR, paths.workspace_slug(channel.workspace()))


def note_id(raw: str) -> str:
    """The id a note is filed under, or "" if nothing usable was sent.

    Slugged rather than taken as typed, because this becomes a filename and the
    model picks the id. `../../.ssh/config` cannot survive the journey - every
    separator and every dot is gone before a path is built from it - and the
    check in `note_path` is there in case that is ever not true.
    """
    return paths.safe_name(raw, ID_MAX_LEN, "note")


def note_path(raw: str) -> str:
    """The file a note id names, or "" if the id cannot name one."""
    identifier = note_id(raw)
    if not identifier:
        return ""
    directory = project_dir()
    path = os.path.join(directory, identifier + SUFFIX)
    # Belt and braces. `note_id` strips every separator, so this cannot fire
    # today; it is here so that loosening the slug later cannot quietly turn
    # a note id into a way to write anywhere on the disk.
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(directory):
        return ""
    return path


def note_ids() -> list:
    """Every note this project has, sorted by id.

    Sorted by name and not by time: this list goes into the system prompt, and
    invariant 5.10 asks that the prompt be byte-identical between builds. An
    mtime order would reshuffle it every time a note was written and throw the
    provider's prefix cache away for no gain.
    """
    try:
        names = os.listdir(project_dir())
    except OSError:
        return []
    return sorted(name[:-len(SUFFIX)] for name in names
                  if name.endswith(SUFFIX) and len(name) > len(SUFFIX))


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _missing(identifier: str) -> str:
    """The refusal for a note that is not there, with what is."""
    existing = note_ids()
    if not existing:
        return (f"[Error] No note '{identifier}' - this project has no notes yet. "
                "`write_note` starts one.")
    return (f"[Error] No note '{identifier}'. This project has: "
            + ", ".join(existing))


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------

def handle_write_note(note: str, content: str = "") -> str:
    """Create a note, or replace one whole."""
    identifier = note_id(note)
    if not identifier:
        return ("[Error] A note id is required, and must have a letter or a "
                "digit in it - it becomes the name of a file.")
    body = str(content or "")
    if len(body) > config.NOTE_MAX_CHARS:
        return (f"[Error] That note is {len(body):,} characters and the ceiling "
                f"is {config.NOTE_MAX_CHARS:,}. Notes are read back into the "
                "conversation whole, so one that does not fit is split into "
                "notes that do.")
    path = note_path(note)
    if not path:
        return f"[Error] '{note}' cannot be used as a note id."
    existed = os.path.exists(path)
    # Markdown ends in a newline the way every other text file does, so a note
    # read by `cat` and one read by the harness look the same.
    if body and not body.endswith("\n"):
        body += "\n"
    try:
        atomic.write_text(path, body)
    except OSError as error:
        return f"[Error] Could not write note '{identifier}': {error}"
    what = "replaced" if existed else "saved"
    return (f"[Success] Note {what}: '{identifier}' ({len(body):,} characters). "
            "Its title is in your prompt from the next session on; "
            f"`read_note` with id '{identifier}' brings the body back.")


def handle_read_note(note: str) -> str:
    identifier = note_id(note)
    if not identifier:
        return "[Error] A note id is required."
    path = note_path(note)
    if not path or not os.path.exists(path):
        return _missing(identifier)
    try:
        body = _read(path)
        written = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError as error:
        return f"[Error] Could not read note '{identifier}': {error}"
    return (f"[Note: {identifier}] (last written {written.isoformat(' ', 'seconds')})\n"
            f"{body}")


def handle_list_notes() -> str:
    listed = note_ids()
    if not listed:
        return ("[Notes] This project has no notes yet. `write_note` saves what "
                "is worth knowing next time - why something is built the way it "
                "is, the order a thing has to be done in, what is still open.")
    lines = [f"[Notes] {len(listed)} for this project "
             f"({os.path.basename(channel.workspace()) or channel.workspace()}):"]
    for i, identifier in enumerate(listed, 1):
        path = os.path.join(project_dir(), identifier + SUFFIX)
        try:
            size = os.path.getsize(path)
            written = datetime.datetime.fromtimestamp(
                os.path.getmtime(path)).strftime("%Y-%m-%d")
        except OSError:
            size, written = 0, "unknown"
        # Bytes, and said so: the ceiling on a note is in characters, and a
        # note written in Korean is three bytes to the character. One number
        # labelled as the other would read as a note four times its real size.
        lines.append(f"{i}. {identifier} ({size:,} bytes, {written})")
    return "\n".join(lines)


def handle_edit_note(note: str, new_content: str = "") -> str:
    """Replace the body of a note that already exists.

    The same split memory has: `write_note` will happily create, this one will
    not. A model that edits a note it only thinks it wrote should be told so
    rather than quietly starting a second one under a name it half-remembered.
    """
    identifier = note_id(note)
    if not identifier:
        return "[Error] A note id is required."
    path = note_path(note)
    if not path or not os.path.exists(path):
        return _missing(identifier) + " - `write_note` creates one."
    return handle_write_note(note, new_content)


def handle_delete_note(note: str) -> str:
    identifier = note_id(note)
    if not identifier:
        return "[Error] A note id is required."
    path = note_path(note)
    if not path or not os.path.exists(path):
        return _missing(identifier)
    try:
        os.remove(path)
    except OSError as error:
        return f"[Error] Could not delete note '{identifier}': {error}"
    return f"[Success] Note deleted: '{identifier}'"


# ---------------------------------------------------------------------------
# what the prompt is told
# ---------------------------------------------------------------------------

def notes_prompt_section() -> str:
    """The titles of this project's notes, for the system prompt.

    Titles only. A note is a paragraph or a page, and putting the bodies here
    would be unbounded context paid for on every turn of every session; what a
    fresh session actually lacks is the knowledge that there is anything to
    read. One line per note buys that, and `read_note` buys the rest.
    """
    listed = note_ids()
    if not listed:
        return ""
    shown = listed[:max(0, config.NOTES_TITLES_MAX)]
    if not shown:
        return ""
    where = os.path.basename(channel.workspace()) or channel.workspace()
    lines = [
        f"\n### PROJECT NOTES - what you wrote down about {where}:",
        "These are markdown notes about this project specifically, not about "
        "the user. Only the titles are here; call `read_note` with one of these "
        "ids before working on anything it sounds like it covers, and "
        "`write_note` when this session learns something the next one would "
        "have to work out again.",
    ]
    lines.extend(f"- {identifier}" for identifier in shown)
    if len(listed) > len(shown):
        lines.append(f"({len(listed) - len(shown)} more - `list_notes` has them.)")
    return "\n".join(lines) + "\n"
