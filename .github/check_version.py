"""Refuse a tag that is not the version in the code.

PyPI accepts a version number once. Not corrected afterwards, not replaced -
only yanked, which leaves it visible and installable by exact pin. So the one
check worth having before an upload is that the number about to be published is
the number the code says it is.

This runs on any tagged ref, not only on a release: v0.4.0 and v0.4.1 were both
tagged onto code that still said 0.3.1, and the first anyone heard of it was
TestPyPI rejecting a 0.3.1 wheel that had already been uploaded months earlier.
CI runs on a tag push, which is the moment that is still cheap to fix - a tag
can be moved, an upload cannot be taken back.
"""
import os
import pathlib
import re
import sys

init = pathlib.Path("aetheris/__init__.py").read_text(encoding="utf-8")
found = re.search(r'^__version__ = "([^"]+)"', init, re.M)
if not found:
    sys.exit("aetheris/__init__.py has no __version__")
version = found.group(1)
print(f"aetheris.__version__ = {version}")

# Empty unless the caller is on a tag, so a push to a branch says the version
# out loud and passes - there is nothing to disagree with yet.
tag = os.environ.get("TAG", "").lstrip("v")
if not tag:
    sys.exit(0)

if tag != version:
    sys.exit(f"tag '{tag}' does not match __version__ '{version}' - "
             f"bump aetheris/__init__.py and move the tag onto that commit")
print(f"tag matches: {tag}")
