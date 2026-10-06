"""Markdown notes, filed per project, and the titles that reach the prompt.

Long-term memory is about the person and follows them everywhere. Notes are the
other half: what is true about *one* repository - why it does the odd thing it
does, the order its deploy runs in, what is still open - and they are markdown,
because that knowledge is a paragraph with a list in it rather than a fact in a
sentence.

What the checks below are actually protecting:

* two projects do not share notes, and one project is the same project from any
  directory inside it. `channel.workspace()` answers both, and a note written
  from `src/` has to be the note read from the root.
* the file is the record. Its name is the id and its bytes are the content -
  there is no index beside it, so nothing can disagree with what is on disk,
  and a person can open the directory in an editor.
* a note id becomes a filename, and the model picks it. `../../.ssh/config`
  must not be a way to write outside the notes directory, in either of the two
  places that is stopped.
* markdown survives the round trip exactly. Headings, fences and blank lines
  come back as they went in; the body travels as a raw block precisely so it
  never has to be JSON-escaped.
* the prompt gets titles and nothing else, capped, and byte-identical between
  builds - it is a cache prefix, and an mtime ordering would reshuffle it on
  every write.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="notes-home-")
os.environ["AETHERIS_HOME"] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.NOTES_DIR = os.path.join(HOME, "notes")

from aetheris import app             # noqa: E402
from aetheris import channel         # noqa: E402
from aetheris import git_ops         # noqa: E402
from aetheris import llm_client      # noqa: E402
from aetheris import notes           # noqa: E402
from aetheris import toolspec        # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def work_in(directory):
    """Stand in `directory` and forget which project the last one was."""
    os.chdir(directory)
    channel.reset()
    git_ops._repo_root_cache.clear()


HEADING = "### PROJECT NOTES"
MARKDOWN = ("# Deploy order\n"
            "\n"
            "1. `pytest` has to pass\n"
            "2. tag, *then* push - never the other way round\n"
            "\n"
            "```bash\n"
            "git tag -a v0.7.0 && git push --tags\n"
            "```\n")

started_in = os.getcwd()
project_a = tempfile.mkdtemp(prefix="notes-a-")
project_b = tempfile.mkdtemp(prefix="notes-b-")
# A real repository, because "the same project from any directory inside it" is
# a promise about a git working tree - that is what `channel.workspace()` asks.
subprocess.run(("git", "init", "-q"), cwd=project_a, capture_output=True)

try:
    # ----------------------------------------------------------------------
    print("--- a note is a markdown file, and the file is the record ---")
    work_in(project_a)
    saved = notes.handle_write_note("Deploy order", MARKDOWN)
    check("saving says which id it filed the note under", "'deploy-order'" in saved, saved[:60])

    on_disk = os.path.join(notes.project_dir(), "deploy-order.md")
    check("the note is one .md file named after the id", os.path.isfile(on_disk))
    with open(on_disk, encoding="utf-8") as f:
        written = f.read()
    check("holding the markdown and nothing else - no frontmatter, no heading added",
          written == MARKDOWN, repr(written[:40]))

    read_back = notes.handle_read_note("deploy-order")
    check("reading gives the body back exactly", MARKDOWN in read_back)
    check("fences and blank lines survive the round trip",
          "```bash\ngit tag -a v0.7.0 && git push --tags\n```" in read_back)
    check("and it says when the note was last written", "last written" in read_back)

    # ----------------------------------------------------------------------
    print("\n--- the id is slugged, because the model picks it ---")
    notes.handle_write_note("Why the hashline?", "three characters, not two\n")
    check("punctuation and case do not reach the filesystem",
          os.path.isfile(os.path.join(notes.project_dir(), "why-the-hashline.md")))
    check("and the note answers to the slug",
          "three characters" in notes.handle_read_note("why-the-hashline"))
    check("as well as to what was typed",
          "three characters" in notes.handle_read_note("Why the hashline?"))

    korean = notes.handle_write_note("빌드 순서", "1. 테스트\n")
    check("a Korean id stays readable rather than becoming a hash",
          os.path.isfile(os.path.join(notes.project_dir(), "빌드-순서.md")), korean[:50])

    check("an id with nothing usable in it is refused, not silently renamed",
          notes.handle_write_note("***", "x").startswith(config.TOOL_ERROR_PREFIX))
    check("and the refusal says what an id has to have",
          "letter" in notes.handle_write_note("", "x"))

    # ----------------------------------------------------------------------
    print("\n--- a note id is not a way out of the notes directory ---")
    escape = notes.handle_write_note("../../../pwned", "should not be here\n")
    outside = os.path.abspath(os.path.join(notes.project_dir(), "..", "..", "..", "pwned.md"))
    check("a traversing id writes nothing outside the notes directory",
          not os.path.exists(outside), escape[:60])
    check("it lands inside it under a flattened name",
          os.path.isfile(os.path.join(notes.project_dir(), "pwned.md")))
    check("and the path helper refuses anything that would leave the directory",
          all(os.path.dirname(os.path.abspath(notes.note_path(bad) or
                                              os.path.join(notes.project_dir(), "x")))
              == os.path.abspath(notes.project_dir())
              for bad in ("../x", "..\\x", "/etc/passwd", "a/b/c")))
    notes.handle_delete_note("pwned")

    # ----------------------------------------------------------------------
    print("\n--- one project is one project, from anywhere inside it ---")
    nested = os.path.join(project_a, "src", "deep")
    os.makedirs(nested, exist_ok=True)
    work_in(nested)
    check("a terminal in a subdirectory reads the same notes",
          "deploy-order" in notes.note_ids(), str(notes.note_ids()))

    work_in(project_b)
    check("another project starts with none of its own", notes.note_ids() == [],
          str(notes.note_ids()))
    check("and says so rather than listing somebody else's",
          "no notes yet" in notes.handle_list_notes())
    notes.handle_write_note("its-own", "b only\n")
    check("what it writes stays there", notes.note_ids() == ["its-own"])

    work_in(project_a)
    check("and the first project never saw it", "its-own" not in notes.note_ids(),
          str(notes.note_ids()))

    # ----------------------------------------------------------------------
    print("\n--- write creates, edit does not ---")
    missing = notes.handle_edit_note("never-written", "x")
    check("editing a note that does not exist is refused",
          missing.startswith(config.TOOL_ERROR_PREFIX), missing[:60])
    check("the refusal names what this project does have", "deploy-order" in missing)
    check("and says which tool would have worked", "write_note" in missing)
    check("nothing was created by the attempt",
          not os.path.exists(os.path.join(notes.project_dir(), "never-written.md")))

    check("editing one that exists replaces it whole",
          "replaced" in notes.handle_edit_note("deploy-order", "# Shorter\n"))
    check("and the old body is gone rather than appended to",
          notes.handle_read_note("deploy-order").count("pytest") == 0)

    # ----------------------------------------------------------------------
    print("\n--- listing, deleting, and what a refusal offers ---")
    listing = notes.handle_list_notes()
    check("the listing counts this project's notes", "3 for this project" in listing, listing[:60])
    check("and names the project it is counting for",
          os.path.basename(project_a) in listing)
    check("a size is labelled as bytes, not characters", "bytes," in listing)

    check("deleting says which note went",
          "'why-the-hashline'" in notes.handle_delete_note("why-the-hashline"))
    check("and it is gone from disk",
          not os.path.exists(os.path.join(notes.project_dir(), "why-the-hashline.md")))
    gone = notes.handle_read_note("why-the-hashline")
    check("reading it now refuses", gone.startswith(config.TOOL_ERROR_PREFIX))
    check("and the refusal lists what is left", "deploy-order" in gone)

    # ----------------------------------------------------------------------
    print("\n--- a note is bounded, because it comes back whole ---")
    too_big = notes.handle_write_note("huge", "x" * (config.NOTE_MAX_CHARS + 1))
    check("a note past the ceiling is refused",
          too_big.startswith(config.TOOL_ERROR_PREFIX), too_big[:70])
    check("the refusal gives both numbers", f"{config.NOTE_MAX_CHARS:,}" in too_big)
    check("and nothing was written",
          not os.path.exists(os.path.join(notes.project_dir(), "huge.md")))
    check("one exactly at the ceiling is fine",
          "Success" in notes.handle_write_note("big", "x" * config.NOTE_MAX_CHARS))
    notes.handle_delete_note("big")

    # ----------------------------------------------------------------------
    print("\n--- the prompt gets the titles, and only the titles ---")
    section = notes.notes_prompt_section()
    check("the block is there once there are notes", HEADING in section)
    check("every note's id is in it",
          all(f"- {i}" in section for i in notes.note_ids()), str(notes.note_ids()))
    check("no note's body is", "Shorter" not in section and "테스트" not in section)
    check("it names the tool that fetches one", "read_note" in section)

    prompt = app._compose_system_prompt()
    check("and it reaches the real system prompt", HEADING in prompt)
    check("beside the memory block rather than instead of it",
          prompt.index(HEADING) > 0)

    work_in(project_b)
    check("a project with different notes gets a different block",
          "its-own" in notes.notes_prompt_section())
    check("and does not carry the other project's", "deploy-order" not in
          notes.notes_prompt_section())
    work_in(project_a)

    empty = tempfile.mkdtemp(prefix="notes-empty-")
    work_in(empty)
    check("a project with no notes adds no block", notes.notes_prompt_section() == "")
    check("and no heading reaches the prompt either",
          HEADING not in app._compose_system_prompt())
    work_in(project_a)

    # ----------------------------------------------------------------------
    print("\n--- the block is capped, and identical between builds ---")
    config.NOTES_TITLES_MAX = 1
    capped = notes.notes_prompt_section()
    check("only as many titles as the cap allows",
          sum(1 for line in capped.splitlines() if line.startswith("- ")) == 1, capped)
    check("and it says how many it did not show", "more" in capped)
    check("naming the tool that has the rest", "list_notes" in capped)
    config.NOTES_TITLES_MAX = 40

    check("building the block twice gives the same bytes",
          notes.notes_prompt_section() == notes.notes_prompt_section())
    # A clock in the prompt is a cache miss on every turn, so the block must not
    # carry one - but it is matched as the shapes a timestamp actually takes
    # rather than as "contains 20". The heading interpolates the project's name,
    # and this suite's project is a `mkdtemp` directory whose random suffix holds
    # `20` about one run in two hundred; that failed the check on a block with no
    # clock in it at all, on one platform of six, for a reason nothing said.
    block = notes.notes_prompt_section()
    named = block.replace(os.path.basename(channel.workspace()), "")
    CLOCKS = (r"\d{4}-\d{2}-\d{2}",     # a date
              r"\d{1,2}:\d{2}",         # a time
              r"\b\d{9,}\b")            # an epoch
    ticking = [shape for shape in CLOCKS if re.search(shape, named)]
    check("with no clock in it", not ticking, str(ticking))
    ordered = [line for line in notes.notes_prompt_section().splitlines()
               if line.startswith("- ")]
    check("the titles are in a fixed order, not the order they were written",
          ordered == sorted(ordered), str(ordered))

    # ----------------------------------------------------------------------
    print("\n--- the table, the prompt and the dispatcher agree ---")
    named = {t.name for t in toolspec.TOOLS}
    check("all five note tools are in the table",
          {"write_note", "read_note", "list_notes", "edit_note",
           "delete_note"} <= named, str(sorted(named & {"write_note", "read_note"})))
    check("the bodies travel as raw blocks, not as JSON strings",
          toolspec.get("write_note").blocks == ("content",)
          and toolspec.get("edit_note").blocks == ("new_content",))
    check("a note id sent under any of its names is understood",
          all(toolspec.get("read_note").bind({key: "deploy-order"})[0] == "deploy-order"
              for key in ("id", "note", "note_id", "title")))
    check("write_note takes the id and the body, in that order",
          toolspec.get("write_note").bind({"id": "x", "content": "y"}) == ["x", "y"])

    # The whole reason the body is a raw block: a note is the text most likely
    # to contain a fenced code block, and that is what a small model cannot
    # JSON-escape. It has to arrive byte for byte.
    fenced = llm_client.parse_tool_calls(
        '<tool_call>\n'
        '{"name": "write_note", "arguments": {"id": "why hashline"}}\n'
        "<content>\n# Why\n\n```\n50:1fa|print(answer)\n```\n</content>\n"
        "</tool_call>", quiet=True)
    check("a note sent over the text protocol parses as one call",
          len(fenced) == 1 and fenced[0][0] == "write_note", str(fenced)[:60])
    body = fenced[0][1].get("content", "") if fenced else ""
    check("its fenced code block arrives unescaped",
          "```\n50:1fa|print(answer)\n```" in body and "\\n" not in body, repr(body[:40]))

    # ----------------------------------------------------------------------
    print("\n--- a hand-edited notes directory cannot break the prompt ---")
    with open(os.path.join(notes.project_dir(), "not-a-note.txt"), "w",
              encoding="utf-8") as f:
        f.write("somebody put this here\n")
    check("a file that is not markdown is not a note",
          "not-a-note" not in notes.note_ids(), str(notes.note_ids()))
    os.makedirs(os.path.join(notes.project_dir(), "adir.md"), exist_ok=True)
    check("and a directory named like one does not raise",
          isinstance(notes.notes_prompt_section(), str))
    shutil.rmtree(os.path.join(notes.project_dir(), "adir.md"))

    shutil.rmtree(notes.project_dir())
    check("a notes directory that has gone reads as no notes", notes.note_ids() == [])
    check("and the prompt simply has no block", notes.notes_prompt_section() == "")

finally:
    os.chdir(started_in)
    channel.reset()
    for directory in (HOME, project_a, project_b):
        shutil.rmtree(directory, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("notes checks passed")
