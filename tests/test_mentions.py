"""`@path` attachments and the `!` shell escape - the two things that act on the
line the user typed rather than on a slash command.

The checks that matter here are the ones about *not* firing: an email address is
not a mention, a decorator pasted into a question is not a mention, and a path
that does not exist must leave the message alone rather than stopping the turn.
The completer is checked against a real directory, because the whole point of it
is that the path comes off disk instead of out of memory.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import paths

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


HOME = tempfile.mkdtemp(prefix="mentions-home-")
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
from aetheris import mentions        # noqa: E402

WORK = tempfile.mkdtemp(prefix="mentions-work-")
origin = os.getcwd()
os.makedirs(os.path.join(WORK, "src", "utils"))
open(os.path.join(WORK, "config.txt"), "w").write("port=8080\nhost=local\n")
open(os.path.join(WORK, "src", "main.py"), "w").write('print("hi")\n')
open(os.path.join(WORK, ".hidden"), "w").write("x\n")
open(os.path.join(WORK, "with space.txt"), "w").write("spaced\n")
os.chdir(WORK)

try:
    print("--- what counts as a mention ---")
    cases = {
        "explain @config.txt please": ["config.txt"],
        "compare @config.txt and @src/main.py": ["config.txt", "src/main.py"],
        "@config.txt @config.txt twice": ["config.txt"],      # named once, sent once
        "look at @config.txt.": ["config.txt"],               # the full stop is prose
        "@src is a folder": ["src"],
        "nothing here at all": [],
        "": [],
    }
    for text, expected in cases.items():
        check(f"{text[:34]!r}", mentions.find(text) == expected, str(mentions.find(text)))

    print("\n--- what must not count ---")
    for text in ("mail me at foo@bar.com",
                 "the decorator is @property",
                 "prices in yen@100",
                 "an email inside a sentence: a@b.co and more"):
        found = mentions.find(text)
        real = [p for p in found if os.path.exists(p)]
        check(f"{text[:38]!r} attaches nothing", not real, str(real))

    print("\n--- a file arrives under the sentence that named it ---")
    out, notes, _pictures = mentions.expand("explain @config.txt please")
    check("the sentence is kept intact", out.startswith("explain @config.txt please"))
    check("the file is named in the attachment", "[Attached file: config.txt]" in out)
    check("its contents come with it", "port=8080" in out)
    check("and it is reported as attached", notes == [("config.txt", True, notes[0][2])],
          str(notes))

    print("\n--- a directory arrives as its listing ---")
    out, notes, _pictures = mentions.expand("what is in @src ?")
    check("the listing is labelled", "[Attached directory listing: src]" in out)
    check("and lists what is there", "main.py" in out and "utils" in out)

    print("\n--- a miss is reported, never fatal ---")
    text = "read @nope.txt for me"
    out, notes, _pictures = mentions.expand(text)
    check("the message goes as written", out == text, repr(out[:60]))
    check("and the miss is reported", notes and notes[0][1] is False, str(notes))

    print("\n--- one mention cannot swallow the context ---")
    big = os.path.join(WORK, "big.txt")
    open(big, "w").write("y" * (config.MENTION_MAX_CHARS * 2))
    out, notes, _pictures = mentions.expand("@big.txt")
    check("the attachment is capped", len(out) < config.MENTION_MAX_CHARS * 1.2, str(len(out)))
    check("and says it was cut", "characters" in out and "read_file" in out)
    os.remove(big)

    print("\n--- a path with a space, quoted ---")
    found = mentions.find('read @"with space.txt" now')
    check("quotes hold the path together", found == ["with space.txt"], str(found))

    if config.PROMPT_TOOLKIT_AVAILABLE:
        from prompt_toolkit.document import Document
        completer = config.PathMentionCompleter()

        def offer(text):
            doc = Document(text, len(text))
            return [c.text for c in completer.get_completions(doc, None)]

        print("\n--- the menu lists what is actually there ---")
        check("@ offers this directory",
              offer("@") == ["config.txt", "src/", "with space.txt"], str(offer("@")))
        check("a directory keeps its separator", "src/" in offer("@"))
        check("typing narrows it", offer("@co") == ["config.txt"], str(offer("@co")))
        check("a separator descends into it",
              offer("@src/") == ["src/main.py", "src/utils/"], str(offer("@src/")))
        check("and keeps narrowing there",
              offer("@src/m") == ["src/main.py"], str(offer("@src/m")))
        check("it works mid-sentence", offer("explain @co") == ["config.txt"])

        print("\n--- and stays out of the way otherwise ---")
        check("dotfiles are hidden until asked for", ".hidden" not in offer("@"))
        check("typing the dot reveals them", offer("@.h") == [".hidden"], str(offer("@.h")))
        check("no @, no menu", offer("just typing") == [])
        check("a slash command is not a path", offer("/help") == [])
        check("an email is not a path", offer("foo@bar") == [], str(offer("foo@bar")))
        check("a directory that does not exist offers nothing", offer("@nope/") == [])

        print("\n--- the slash completer is untouched ---")
        # It takes a function now rather than a list of names, because the menu
        # says what each command does and what may follow it. A fixed one here
        # keeps this test about the two completers coexisting.
        def suggest(text):
            return [(name, name, f"does {name[1:]}")
                    for name in ("/help", "/clear", "/connect")
                    if name.startswith(text)]

        slash = config.SlashCommandCompleter(suggest)
        got = [c.text for c in slash.get_completions(Document("/c", 2), None)]
        check("it still completes commands", got == ["/clear", "/connect"], str(got))
        described = list(slash.get_completions(Document("/c", 2), None))
        check("and says what each one does",
              all(str(c.display_meta) for c in described))
        # A menu is a convenience; nothing it does may stop a line being typed.
        exploding = config.SlashCommandCompleter(lambda text: 1 / 0)
        check("a suggester that raises yields nothing rather than raising",
              list(exploding.get_completions(Document("/c", 2), None)) == [])

        print("\n--- and the menu previews what comes next ---")
        # The menu used to offer forty bare words with no hint of what any did,
        # and it stopped at the first space - so `/mcp ` and `/set `, where
        # what you cannot remember is exactly what comes next, offered nothing.
        from aetheris import tui                              # noqa: E402

        def shown(text):
            return {display for _, display, _ in tui.complete_command(text)}

        def inserted(text):
            return {insert for insert, _, _ in tui.complete_command(text)}

        top = tui.complete_command("/")
        check("typing / offers every command", len(top) > 25, str(len(top)))
        check("each with what it does", all(meta.strip() for _, _, meta in top))
        check("including the second spelling of /exit", "/quit" in inserted("/"))

        check("a space asks the other question", "/vm reset" in inserted("/vm "))
        check("and narrows as you type", inserted("/vm r") == {"/vm reset"},
              str(inserted("/vm r")))
        check("alternatives are offered one by one, not as one row",
              {"/mcp tools", "/mcp prompts", "/mcp all"} <= inserted("/mcp "),
              str(sorted(inserted("/mcp "))))
        check("and each shows its own spelling",
              "/mcp tools [name]" in shown("/mcp ") and
              "/mcp tools|prompts|all [name]" not in shown("/mcp "),
              str(sorted(shown("/mcp "))))
        check("a switch offers on and off",
              {"/deepthink on", "/deepthink off"} == inserted("/deepthink "),
              str(inserted("/deepthink ")))
        check("a placeholder is shown but never completed",
              "/perms allow" in inserted("/perms ")
              and "/perms allow <rule>" in shown("/perms "),
              str(sorted(shown("/perms "))))

        # The names nobody remembers are the ones worth completing.
        check("/set offers the settings", "/set NUM_CTX" in inserted("/set "))
        check("with their current value", any(
            "65,536" in meta for insert, _, meta in tui.complete_command("/set NUM_CTX")))
        check("/connect offers the providers",
              "/connect anthropic" in inserted("/connect "))
        check("and says whether each can be used", any(
            "API key" in meta for _, _, meta in tui.complete_command("/connect anth")))

        check("free text is never completed over", tui.complete_command("/tdd fix it") == [])
        check("nor is an ordinary message", tui.complete_command("hello there") == [])

        print("\n--- and a `!` line says it is a shell command before Enter ---")
        # One box takes a message for the model and, behind a `!`, a command
        # for this machine. They looked identical while being typed, so the
        # first sign that a line had run as a shell command was it running.
        from aetheris import app                               # noqa: E402

        lexer = config.ShellLineLexer(app.SHELL_STYLE)

        def styled(text):
            return lexer.lex_document(Document(text, len(text)))(0)[0][0]

        check("an ordinary message is not coloured", styled("hello there") == "")
        check("a slash command is not either", styled("/help") == "")
        check("a ! command is", styled("!ls -al") == app.SHELL_STYLE, styled("!ls -al"))
        check("leading spaces do not hide it",
              styled("  !git status") == app.SHELL_STYLE)

        # The banner reads the buffer of the running application; with none
        # running there is nothing being typed, so it must say so rather than
        # raise from inside a redraw.
        check("with no application, nothing is being typed", not config.typing_shell())
        check("so the prompt is the ordinary one", "Shell" not in app._prompt_message())
    else:
        print("\n  [skip] prompt_toolkit is not installed; the menu is not testable here")

finally:
    os.chdir(origin)
    shutil.rmtree(HOME, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("mention checks passed")
