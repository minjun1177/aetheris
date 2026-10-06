"""State belongs to the person, not to whatever directory they started in.

`sessions/`, `memory.json` and `.chat_history` used to be written into the
working directory. That was survivable while the harness was `python app.py`
inside its own checkout. As an installed command it meant starting in a home
directory left files there, two projects gave you two unrelated memories, and
`/sessions` only ever listed the ones belonging to wherever you were standing.

The checks below are the ones that would have caught that: nothing personal
resolves to a relative path, every module agrees on one home, and the override
the tests themselves rely on actually works.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aetheris import paths

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


# The override has to be set before `config` is imported: it reads the paths at
# import time, and this must not touch the real ~/.aetheris.
HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".paths-test-home")
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
from aetheris import mcp_client, permissions, providers, skills   # noqa: E402

print("--- the override decides where everything goes ---")
check("home() follows AETHERIS_HOME", paths.home() == os.path.abspath(HOME),
      paths.home())
check("state() builds inside it",
      paths.state("x.json") == os.path.join(os.path.abspath(HOME), "x.json"))

print("\n--- nothing personal is written where the user happens to stand ---")
personal = {
    "config.MEMORY_FILE": config.MEMORY_FILE,
    "config.SESSION_DIR": config.SESSION_DIR,
    "config.HISTORY_FILE": config.HISTORY_FILE,
    "providers.CONFIG_PATH": providers.CONFIG_PATH,
    "skills.USER_SKILL_DIR": skills.USER_SKILL_DIR,
    "mcp_client.USER_CONFIG_FILE": mcp_client.USER_CONFIG_FILE,
    "permissions.USER_CONFIG_FILE": permissions.USER_CONFIG_FILE,
}
for name, value in personal.items():
    check(f"{name} is absolute", os.path.isabs(value), value)
    check(f"{name} is under the home directory",
          os.path.commonpath([value, paths.home()]) == paths.home())

print("\n--- what is about a project stays with the project ---")
# These are read from the working directory *first* and from the home directory
# second. A project's own rules, servers and skills have to be able to win.
check("permissions still reads a project file",
      any(source == "project" for source, _ in permissions.config_paths()))
check("mcp still reads a project file",
      any(source == "project" for source, _ in mcp_client.config_paths()))
check("skills still reads a project directory",
      skills.skill_dirs()[0] == ("project", os.path.abspath("skills")))

print("\n--- state left by an older version is named, never moved ---")
work = os.path.join(HOME, "work")
os.makedirs(work, exist_ok=True)
origin = os.getcwd()
os.chdir(work)
try:
    check("a clean directory reports nothing", paths.strays_in_cwd() == [])

    open("memory.json", "w").close()
    os.makedirs("sessions", exist_ok=True)
    found = paths.strays_in_cwd()
    check("an older version's files are found", set(found) == {"memory.json", "sessions"},
          str(found))
    check("and they are still there afterwards",
          os.path.exists("memory.json") and os.path.isdir("sessions"))
finally:
    os.chdir(origin)

print("\n--- a pre-1.0.0 install keeps the directory it already has ---")
# The rename to Aetheris is not allowed to cost anyone their sessions, so
# `~/.localchat` still wins where it exists and `~/.aetheris` does not. Both
# home variables are faked: `expanduser` reads HOME on POSIX and USERPROFILE on
# Windows, and this suite runs on both.
fake = os.path.join(HOME, "fake-user")
os.makedirs(fake, exist_ok=True)
saved = {name: os.environ.get(name)
         for name in (paths.ENV_VAR, paths.LEGACY_ENV_VAR, "HOME", "USERPROFILE")}
try:
    os.environ.pop(paths.ENV_VAR, None)
    os.environ.pop(paths.LEGACY_ENV_VAR, None)
    os.environ["HOME"] = os.environ["USERPROFILE"] = fake

    check("with neither directory present, the new name is the home",
          paths.home() == os.path.join(fake, paths.DIR_NAME), paths.home())

    legacy = os.path.join(fake, paths.LEGACY_DIR_NAME)
    os.makedirs(legacy, exist_ok=True)
    check("an existing ~/.localchat is used as it stands",
          paths.home() == legacy, paths.home())

    os.makedirs(os.path.join(fake, paths.DIR_NAME), exist_ok=True)
    check("once both exist, the new name wins",
          paths.home() == os.path.join(fake, paths.DIR_NAME), paths.home())
    check("and the old directory is never moved or removed", os.path.isdir(legacy))

    # An unattended job that exports the old variable must keep working.
    os.environ[paths.LEGACY_ENV_VAR] = os.path.join(HOME, "from-legacy-var")
    check(f"{paths.LEGACY_ENV_VAR} still moves the home",
          paths.home() == os.path.abspath(os.path.join(HOME, "from-legacy-var")),
          paths.home())
    os.environ[paths.ENV_VAR] = os.path.join(HOME, "from-new-var")
    check(f"and {paths.ENV_VAR} wins when both are set",
          paths.home() == os.path.abspath(os.path.join(HOME, "from-new-var")),
          paths.home())
finally:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

shutil.rmtree(HOME, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("path checks passed")
