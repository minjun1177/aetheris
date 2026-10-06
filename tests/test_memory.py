"""A memory marked important is in the prompt before the first message.

Long-term memory only pays off if the model goes looking for it, and a fresh
session has no reason to look: nothing in it says there is anything to find.
`write_memory` with `important` set is the answer to that - the memory is
written into the system prompt when the prompt is built, which is the start of
every session, so the model has it before the user has typed anything.

What the checks below are actually protecting:

* the mark survives being saved over. A model that rewrites "User name" a month
  later, without thinking about `important`, must not silently demote the one
  fact the user asked to be remembered - but an explicit `false` must still
  take the mark away, or there would be no way back.
* the block is bounded. It is in the prompt, and the prompt is paid for on
  every turn: forty memories of a thousand characters each would quietly eat
  the context window that the conversation needs.
* it is byte-identical between builds. The prompt is a cache prefix (invariant
  in `providers.py`), and a set iterated into text, or a clock, would cost a
  cache miss on every request instead of none.
* resuming re-reads it. A session picked up tomorrow is a session start too,
  and the block saved into its file is yesterday's.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="memory-home-")
os.environ["AETHERIS_HOME"] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.MEMORY_FILE = os.path.join(HOME, "memory.json")

from aetheris import app             # noqa: E402
from aetheris import session         # noqa: E402
from aetheris import toolspec        # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


def wipe():
    session.save_memory({})


HEADING = "### MEMORY - what you were told to remember:"

try:
    print("--- the flag is written, read back, and advertised ---")
    wipe()
    session.handle_write_memory("User name", "Minjun", "true")
    session.handle_write_memory("Today's task", "fix the parser")
    check("an important memory says so when it is saved",
          "important" in session.handle_write_memory("Rule", "never push to main", True))
    check("and reads back as important",
          "Important: yes" in session.handle_read_memory("User name"))
    check("an ordinary one does not",
          "Important: no" in session.handle_read_memory("Today's task"))
    listing = session.handle_get_memory_list()
    check("the list marks the important ones", "User name [important]" in listing)
    check("and leaves the rest alone", "Today's task (" in listing)
    check("`important` is a parameter of write_memory",
          "important" in toolspec.get("write_memory").schema()["parameters"])
    check("and is optional, so an old-shaped call still binds",
          toolspec.get("write_memory").bind({"id": "a", "content": "b"}) == ["a", "b", ""])

    print("\n--- the mark belongs to the memory, not to the call that rewrote it ---")
    session.handle_write_memory("User name", "Minjun Lee")
    check("saving over it without saying anything keeps the mark",
          "Important: yes" in session.handle_read_memory("User name"))
    session.handle_write_memory("User name", "Minjun Lee", "false")
    check("and an explicit false takes it away",
          "Important: no" in session.handle_read_memory("User name"))
    session.handle_write_memory("User name", "Minjun Lee", "true")
    session.handle_edit_memory("User name", "Minjun L.")
    check("edit_memory changes the text and not the mark",
          "Important: yes" in session.handle_read_memory("User name"))

    print("\n--- what a session opens with ---")
    prompt = app._compose_system_prompt()
    check("the block is in the system prompt a new session starts on",
          HEADING in prompt)
    check("an important memory is written out in full", "never push to main" in prompt)
    check("an ordinary one is not", "fix the parser" not in prompt)
    check("it is built the same way twice",
          session.memory_prompt_section() == session.memory_prompt_section())

    persona, summary = config.CUSTOM_PERSONA, "\n\n<SUMMARY>earlier work</SUMMARY>"
    config.CUSTOM_PERSONA = "You are terse."
    composed = app._compose_system_prompt(summary)
    check("the persona still comes first", composed.startswith("You are terse."))
    check("and the summary still comes last", composed.rstrip().endswith("</SUMMARY>"))
    check("so compression can still find it",
          app._extract_summary(composed) == summary)
    config.CUSTOM_PERSONA = persona

    print("\n--- with nothing marked, nothing is added ---")
    wipe()
    check("no memories, no block", session.memory_prompt_section() == "")
    session.handle_write_memory("Today's task", "fix the parser")
    check("none of them important, no block", session.memory_prompt_section() == "")
    check("and the prompt is untouched", HEADING not in app._compose_system_prompt())

    print("\n--- the block is bounded, and says when it cut something ---")
    wipe()
    for i in range(config.MEMORY_IMPORTANT_MAX + 5):
        session.handle_write_memory(f"m{i}", "x" * (config.MEMORY_IMPORTANT_CHARS + 400), True)
    section = session.memory_prompt_section()
    check("no more entries than the cap allows",
          section.count("\n- ") == config.MEMORY_IMPORTANT_MAX,
          f"{section.count(chr(10) + '- ')} entries")
    check("and it says how many it left out", "5 more are marked important" in section)
    check("a long memory is cut", "(cut - `read_memory`" in section)
    check("and cut to the length the setting names",
          len(max(section.splitlines(), key=len)) < config.MEMORY_IMPORTANT_CHARS + 200)
    check("both caps are settings, so they can be changed",
          {"MEMORY_IMPORTANT_MAX", "MEMORY_IMPORTANT_CHARS"} <= set(config.settable()))

    print("\n--- a hand-edited memory.json cannot break the prompt ---")
    for broken in ("[]", "not json at all", '{"a": "a bare string from version 1"}',
                   '{"b": {"content": "no flag here"}}', '{"c": {"important": true}}'):
        with open(config.MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write(broken)
        try:
            session.memory_prompt_section()
            app._compose_system_prompt()
            ok, why = True, ""
        except Exception as error:
            ok, why = False, f"{type(error).__name__}: {error}"
        check(f"survives {broken[:34]!r}", ok, why)

    print("\n--- resuming re-reads it ---")
    wipe()
    saved = [{"role": "system", "content": "an old prompt with no memory in it"},
             {"role": "user", "content": "hello"}]
    session.handle_write_memory("Rule", "never push to main", True)
    messages = app._adopt_session({"version": session.SESSION_FORMAT, "title": "t",
                                   "model": config.MODEL, "persona": "",
                                   "messages": saved, "token_history": []})
    check("a session saved before the memory existed picks it up",
          HEADING in messages[0]["content"])
    check("and the conversation itself is untouched",
          [m["role"] for m in messages] == ["system", "user"])

    print("\n--- and so does setting a persona ---")
    # `/system` wrote the system message by hand - `persona + SYSTEM_PROMPT +
    # summary` - which is everything `_compose_system_prompt` builds except the
    # two blocks it adds: the memories marked important, and this project's note
    # titles. So asking for a persona silently threw away the whole of 5.15
    # until something else happened to rebuild the prompt. It also read its
    # argument off the lowercased line the commands are matched against, so the
    # persona itself came out in lower case.
    wipe()
    session.handle_write_memory("Rule", "never push to main", True)
    config.SYSTEM_PROMPT = app._build_system_prompt()
    messages = [{"role": "system",
                 "content": app._compose_system_prompt("\n\n<SUMMARY>kept</SUMMARY>")}]
    persona = "You are a Korean tutor named Minji."
    with contextlib.redirect_stdout(io.StringIO()):
        app._system_command(" " + persona, messages)
    prompt = messages[0]["content"]
    check("the persona is stored exactly as it was typed", prompt.startswith(persona),
          prompt[:60])
    check("the important memory is still in the prompt", HEADING in prompt)
    check("and so is the summary", "<SUMMARY>kept</SUMMARY>" in prompt)

    with contextlib.redirect_stdout(io.StringIO()):
        app._system_command(" reset", messages)
    prompt = messages[0]["content"]
    check("reset takes the persona back out", "Minji" not in prompt)
    check("and leaves the memory where it was", HEADING in prompt)
    check("and the summary too", "<SUMMARY>kept</SUMMARY>" in prompt)
    check("config forgets it as well", config.CUSTOM_PERSONA == "")

    with contextlib.redirect_stdout(io.StringIO()) as said:
        app._system_command("   ", messages)
    check("a bare /system says how to use it", "Usage:" in said.getvalue(),
          said.getvalue().strip()[:60])

finally:
    shutil.rmtree(HOME, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("memory checks passed")
