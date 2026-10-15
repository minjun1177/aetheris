"""Refuse a wheel or an sdist that is not what this checkout says it is.

Three ways a release has gone out wrong, or could have, without anything
failing on the way:

* **A module missing from the wheel.** `packages = ["aetheris"]` takes what it
  finds, and an import that only fails on a user's machine is found by them.
* **An sdist the suite cannot run from.** It already carried `tests/`, but not
  the CHANGELOG, `docs/` or `bench/` that three of those tests read - so
  "download the source and check your install" failed on the first try.
* **Metadata behind the code.** The classifiers stopped at 3.13 while 3.14 was
  being tested, and said "OS Independent" about the one module written per
  platform.

So the wheel must hold every tracked file under `aetheris/` and nothing else,
the sdist every tracked file - `.github/` too, since `test_dist.py` reads it -
and the classifiers must say
what CI runs: every Python from `requires-python` up to the newest one in the
matrix, and one operating system per runner.

    python .github/check_dist.py dist/
"""
import email
import glob
import os
import re
import subprocess
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")

RUNNER_OS = {
    "ubuntu": "Operating System :: POSIX :: Linux",
    "macos": "Operating System :: MacOS",
    "windows": "Operating System :: Microsoft :: Windows",
}


def tracked() -> list:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def _version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def ci_matrix(text: str) -> tuple:
    """(Python versions, runner OS names) the CI workflow tests on."""
    versions = set()
    for listed in re.findall(r"python-version:\s*\[([^\]]*)\]", text):
        versions.update(re.findall(r'"(3\.\d+)"', listed))
    versions.update(re.findall(r'python-version:\s*"(3\.\d+)"', text))
    systems = set()
    for listed in re.findall(r"\bos:\s*\[([^\]]*)\]", text):
        systems.update(re.findall(r"([a-z]+)-latest", listed))
    systems.update(re.findall(r"-\s*os:\s*([a-z]+)-latest", text))
    return versions, systems


def check_wheel(names: list, files: list) -> list:
    package = {f for f in files if f.startswith("aetheris/")}
    problems = [f"wheel lacks {f}" for f in sorted(package - set(names))]
    stray = [n for n in names if not n.startswith("aetheris/")
             and not n.split("/", 1)[0].endswith(".dist-info")]
    problems += [f"wheel carries {n}, which is not the package" for n in stray]
    return problems


def check_sdist(names: list, files: list) -> list:
    # Every member sits under one `<name>-<version>/` directory.
    inside = {n.split("/", 1)[1] for n in names if "/" in n}
    return [f"sdist lacks {f}" for f in files if f not in inside]


def check_metadata(metadata: str, ci_text: str) -> list:
    meta = email.message_from_string(metadata)
    classifiers = meta.get_all("Classifier") or []
    versions, systems = ci_matrix(ci_text)
    problems = []

    floor = re.match(r">=\s*(3\.\d+)", meta.get("Requires-Python", ""))
    if not floor:
        return ["Requires-Python is not a >=3.x floor"]
    floor = floor.group(1)
    if not versions:
        return ["no Python versions found in ci.yml"]
    if floor not in versions:
        problems.append(f"requires-python says {floor}, which CI does not test")
    newest = max(versions, key=_version)
    expected = {f"3.{minor}" for minor in range(_version(floor)[1], _version(newest)[1] + 1)}
    claimed = {c.rsplit(" :: ", 1)[1] for c in classifiers
               if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)}
    if claimed != expected:
        problems.append(f"Python classifiers are {sorted(claimed, key=_version)}, "
                        f"CI tests {floor} to {newest}")

    if "Operating System :: OS Independent" in classifiers:
        problems.append("'OS Independent' - the waiting detection is per platform")
    claimed_os = {c for c in classifiers if c.startswith("Operating System ::")}
    wanted_os = {RUNNER_OS[s] for s in systems if s in RUNNER_OS}
    if claimed_os != wanted_os:
        problems.append(f"OS classifiers are {sorted(claimed_os)}, "
                        f"CI runs on {sorted(wanted_os)}")
    return problems


def check(dist: str) -> list:
    wheels = glob.glob(os.path.join(dist, "*.whl"))
    sdists = glob.glob(os.path.join(dist, "*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return [f"expected one wheel and one sdist in {dist}, "
                f"found {len(wheels)} and {len(sdists)}"]
    files = tracked()
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        metadata = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = wheel.read(metadata).decode("utf-8")
    with tarfile.open(sdists[0]) as sdist:
        sdist_names = sdist.getnames()
    with open(CI, encoding="utf-8") as handle:
        ci_text = handle.read()
    return (check_wheel(names, files) + check_sdist(sdist_names, files)
            + check_metadata(metadata, ci_text))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python .github/check_dist.py DIST_DIR")
    found = check(sys.argv[1])
    for problem in found:
        print(f"  {problem}")
    if found:
        sys.exit(f"{len(found)} problem(s) with what would be published")
    print("wheel, sdist and classifiers match this checkout")
