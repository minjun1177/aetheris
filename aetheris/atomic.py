"""Write a file so that a crash can never leave half of it behind.

The pattern is always the same: write the new contents to a temporary file in
the same directory, then `os.replace` it over the target. `os.replace` is atomic
on POSIX and on Windows, so a reader either sees the whole old file or the whole
new one - never a truncated mixture of the two.

`open(path, "w")` truncates first and writes second. Losing power, or being
killed, in between leaves an empty or half-written file. For a session
transcript that is an afternoon's conversation gone; for `providers.json` it is
the API key gone.

The temporary file is created with owner-only permissions and can be asked to
keep them, so a file holding a secret is never briefly readable by anyone else -
which is what `open()` followed by `chmod` leaves it as.

Stdlib only: this is imported by modules that load before anything else.
"""

import json
import os
import tempfile


def write_text(path: str, data: str, private: bool = False) -> None:
    """Replace `path` with `data`, all at once or not at all.

    With `private=True` the file ends up readable only by its owner, and was
    never readable by anyone else at any point in between.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    # mkstemp creates the file 0600, so a secret starts out private and the
    # umask never gets a say in it.
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-",
                                         suffix=os.path.basename(path))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())    # the bytes, not just the rename, must survive
        if not private:
            _relax(temporary, path)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_json(path: str, data, private: bool = False, indent: int = 2) -> None:
    """Serialise first, write second - a value that will not encode writes nothing."""
    text = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
    write_text(path, text, private=private)


def _relax(temporary: str, target: str) -> None:
    """Give an ordinary file the permissions it should have.

    An existing file keeps the mode it already has, so a file someone chmodded
    does not quietly revert. A new one gets 0644, which is what the usual umask
    would have produced anyway - reading the umask to be exact would mean
    setting it to 0 and back, and this program has background threads running.
    """
    try:
        mode = os.stat(target).st_mode & 0o777
    except OSError:
        mode = 0o644
    try:
        os.chmod(temporary, mode)
    except OSError:
        pass


if __name__ == "__main__":
    print("This file can not run directly.")
