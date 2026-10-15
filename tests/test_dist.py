"""`.github/check_dist.py`: the check that what would be published is this checkout.

It runs in CI on a real build. Here its rules are held to made-up wheels and
sdists - one missing a module, one carrying the tests, one without the files the
suite reads - and to the classifiers in pyproject.toml against the CI matrix,
which needs no build: the metadata falling behind the code is the drift it was
written for, and this catches it on any machine that runs the suite.
"""
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("check_dist",
                                              os.path.join(ROOT, ".github", "check_dist.py"))
dist = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dist)

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


CI = """
        os: [ubuntu-latest]
        python-version: ["3.10", "3.12", "3.13"]
        include:
          - os: windows-latest
            python-version: "3.13"
          - os: macos-latest
            python-version: "3.13"
"""
OS = ["Operating System :: POSIX :: Linux", "Operating System :: MacOS",
      "Operating System :: Microsoft :: Windows"]


def metadata(versions, systems=OS, floor="3.10"):
    lines = [f"Requires-Python: >={floor}"]
    lines += [f"Classifier: Programming Language :: Python :: {v}" for v in versions]
    lines += [f"Classifier: {s}" for s in systems]
    return "Metadata-Version: 2.4\nName: aetheris\n" + "\n".join(lines) + "\n"


print("--- the CI matrix ---")
versions, systems = dist.ci_matrix(CI)
check("versions from the list and from each include",
      versions == {"3.10", "3.12", "3.13"}, str(versions))
check("and one OS per runner", systems == {"ubuntu", "windows", "macos"}, str(systems))

print("\n--- the classifiers say what CI runs ---")
check("3.10 to the newest tested, gaps in the matrix included, passes",
      dist.check_metadata(metadata(["3.10", "3.11", "3.12", "3.13"]), CI) == [])
check("stopping short of the newest one tested does not",
      dist.check_metadata(metadata(["3.10", "3.11", "3.12"]), CI) != [])
check("nor claiming one that is not tested",
      dist.check_metadata(metadata(["3.10", "3.11", "3.12", "3.13", "3.14"]), CI) != [])
check("nor a floor CI never runs", dist.check_metadata(
    metadata(["3.9", "3.10", "3.11", "3.12", "3.13"], floor="3.9"), CI) != [])
problems = dist.check_metadata(metadata(["3.10", "3.11", "3.12", "3.13"],
                                        ["Operating System :: OS Independent"]), CI)
check("'OS Independent' is refused", any("OS Independent" in p for p in problems), str(problems))
check("and so is a runner with no classifier", dist.check_metadata(
    metadata(["3.10", "3.11", "3.12", "3.13"], OS[:2]), CI) != [])

print("\n--- the wheel is the package, all of it ---")
files = ["aetheris/__init__.py", "aetheris/app.py", "aetheris/py.typed",
         "tests/test_x.py", "CHANGELOG.md", ".github/check_dist.py"]
wheel = ["aetheris/__init__.py", "aetheris/app.py", "aetheris/py.typed",
         "aetheris-1.0.dist-info/METADATA", "aetheris-1.0.dist-info/RECORD"]
check("every tracked package file and the dist-info passes", dist.check_wheel(wheel, files) == [])
check("a module left out does not",
      dist.check_wheel([n for n in wheel if n != "aetheris/app.py"], files)
      == ["wheel lacks aetheris/app.py"])
check("nor the tests carried along", dist.check_wheel(wheel + ["tests/test_x.py"], files) != [])

print("\n--- the sdist is the checkout ---")
sdist = ["aetheris-1.0", "aetheris-1.0/PKG-INFO"] + [f"aetheris-1.0/{f}" for f in files]
check("every tracked file passes", dist.check_sdist(sdist, files) == [])
check("without the CHANGELOG the tests read, it does not",
      dist.check_sdist([n for n in sdist if not n.endswith("CHANGELOG.md")], files)
      == ["sdist lacks CHANGELOG.md"])

print("\n--- this checkout ---")
with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
    project = handle.read()
with open(dist.CI, encoding="utf-8") as handle:
    ci = handle.read()
# No tomllib on 3.10, and the two fields are plain strings in the file.
floor = re.search(r'^requires-python = "([^"]+)"', project, re.M).group(1)
own = "Requires-Python: " + floor + "\n" + "".join(
    f"Classifier: {c}\n" for c in re.findall(r'^\s+"([^"]+ :: [^"]+)",', project, re.M))
problems = dist.check_metadata(own, ci)
check("pyproject.toml's classifiers match ci.yml", problems == [], str(problems))


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("dist checks passed")
