"""Picking up a conversation instead of starting a new one.

`--resume <id>` and `-c` are the only two things that cannot be slash commands:
by the time there is a prompt to type at, a new session has already begun. Both
resolve on the command line, before the screen is cleared, and neither is
allowed to quietly hand back a blank session when it finds nothing - which is
what most of the checks below are about.

`-c` means "the session I was last in *here*", so a session file has to record
the directory it was worked in. Files written before it did have nothing to
match on, and must never be guessed at.
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


# Set before `config` is imported: it resolves the session directory at import
# time, and none of this may touch the real ~/.aetheris.
HOME = tempfile.mkdtemp(prefix="resume-home-")
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
from aetheris import session         # noqa: E402
from aetheris import app             # noqa: E402

WORK = tempfile.mkdtemp(prefix="resume-work-")
project_a = os.path.join(WORK, "project-a")
project_b = os.path.join(WORK, "project-b")
os.makedirs(project_a)
os.makedirs(project_b)
origin = os.getcwd()


def saved_in(directory, title):
    """Save a session from `directory`, the way running there would."""
    os.chdir(directory)
    try:
        config.SESSION_TITLE = title
        config.token_history.clear()
        return session.save_session(
            [{"role": "system", "content": "s"},
             {"role": "user", "content": f"hello from {title}"}], "")
    finally:
        os.chdir(origin)


try:
    print("--- a session records where it was worked on ---")
    first = saved_in(project_a, "첫 번째 대화")
    data = session.load_session(first)
    check("the file names its directory",
          session._dir_key(data.get("cwd", "")) == session._dir_key(project_a),
          data.get("cwd", ""))
    check("and says which format wrote it", data.get("version") == session.SESSION_FORMAT,
          str(data.get("version")))

    print("\n--- -c takes the newest session from this directory ---")
    second = saved_in(project_a, "second here")
    elsewhere = saved_in(project_b, "another project")
    check("the newest one in the directory wins",
          session.latest_in_dir(project_a) == second,
          session.latest_in_dir(project_a))
    check("a different directory gets its own",
          session.latest_in_dir(project_b) == elsewhere,
          session.latest_in_dir(project_b))
    check("and it is not the newest session overall",
          session.latest_in_dir(project_a) != elsewhere)
    check("a directory with no sessions gets nothing",
          session.latest_in_dir(os.path.join(WORK, "never-used")) == "")
    check("neither does a directory that cannot be named",
          session.latest_in_dir("") == "")

    print("\n--- one directory, however it is spelled ---")
    check("a trailing separator is the same place",
          session.latest_in_dir(project_a + os.sep) == second)
    check("so is a path through '..'",
          session.latest_in_dir(os.path.join(project_b, "..", "project-a")) == second)
    if hasattr(os, "symlink"):
        link = os.path.join(WORK, "link-to-a")
        try:
            os.symlink(project_a, link)
            check("so is a symlink to it", session.latest_in_dir(link) == second)
        except (OSError, NotImplementedError):
            print("  [skip] symlinks are not available here")

    print("\n--- a session from before the format recorded a directory ---")
    old = os.path.join(config.SESSION_DIR, "older-version.json")
    from aetheris import atomic
    atomic.write_json(old, {"version": 3, "title": "no cwd here", "model": "m",
                            "messages": [], "token_history": [],
                            "updated_at": "2099-01-01T00:00:00"})
    check("it still lists", "older-version" in [s[0] for s in session.list_sessions()])
    check("but -c never guesses at it",
          session.latest_in_dir(project_a) == second,
          session.latest_in_dir(project_a))

    print("\n--- --resume finds a session by id or by title ---")
    check("by id", [e[0] for e in session.find_sessions(second)] == [second])
    check("by exact title", [e[0] for e in session.find_sessions("첫 번째 대화")] == [first])
    check("by part of one", first in [e[0] for e in session.find_sessions("첫 번째")])
    check("and nothing matches nothing", session.find_sessions("no-such-session") == [])

    print("\n--- both flags are accepted in every spelling ---")
    for spelling in ("-resume", "--resume", "-r"):
        args = app._parse_args([spelling, "x"])
        check(f"{spelling} <id>", args.resume == "x" and not args.continue_here)
    for spelling in ("-c", "-continue", "--continue"):
        args = app._parse_args([spelling])
        check(f"{spelling}", args.continue_here and not args.resume)
    check("no arguments means a new session",
          app._parse_args([]).resume is None and not app._parse_args([]).continue_here)
    try:
        app._parse_args(["-c", "--resume", "x"])
        check("asking for both at once is refused", False, "it was accepted")
    except SystemExit as e:
        check("asking for both at once is refused", e.code == 2, str(e.code))

    print("\n--- what the command line resolves to ---")
    check("nothing asked for, nothing resumed",
          app._session_to_resume(app._parse_args([])) == "")
    check("a named session resolves to its id",
          app._session_to_resume(app._parse_args(["--resume", second])) == second)

    os.chdir(project_a)
    try:
        check("-c resolves to the newest session here",
              app._session_to_resume(app._parse_args(["-c"])) == second)
    finally:
        os.chdir(origin)

    # A miss must stop the program rather than open an empty conversation.
    unused = os.path.join(WORK, "unused")
    os.makedirs(unused, exist_ok=True)
    for argv, where, label in (
            (["--resume", "no-such-session"], origin, "an unknown name stops"),
            (["-c"], unused, "-c in a directory never worked in stops")):
        os.chdir(where)
        try:
            app._session_to_resume(app._parse_args(argv))
            check(label, False, "it returned instead")
        except SystemExit as e:
            check(label, e.code == 1, str(e.code))
        finally:
            os.chdir(origin)

    # An ambiguous name must not pick one of the candidates.
    both = saved_in(project_a, "second here")     # same title, second file
    check("two files can share a title", both != second)
    try:
        app._session_to_resume(app._parse_args(["--resume", "second here"]))
        check("an ambiguous name stops", False, "it chose one")
    except SystemExit as e:
        check("an ambiguous name stops", e.code == 1, str(e.code))

    print("\n--- a resumed file becomes the live conversation ---")
    config.MODEL, config.SESSION_TITLE, config.CUSTOM_PERSONA = "before", "", ""
    loaded = session.load_session(first)
    messages = app._adopt_session(loaded)
    check("its messages come back", any(m["role"] == "user" for m in messages))
    check("its title comes with them", config.SESSION_TITLE == "첫 번째 대화",
          config.SESSION_TITLE)
    check("and so does its model", config.MODEL != "before", config.MODEL)

    # A file with nothing usable in it still has to leave a conversation that
    # the next turn can address messages[0] of.
    for broken in ({"version": 4, "messages": []},
                   {"version": 4, "messages": [{"role": "user", "content": "hi"}]},
                   [{"role": "user", "content": "legacy"}]):
        messages = app._adopt_session(broken)
        check(f"a system message is always first ({type(broken).__name__})",
              messages and messages[0]["role"] == "system")

finally:
    os.chdir(origin)
    shutil.rmtree(HOME, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("resume checks passed")
