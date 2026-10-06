"""A Python scratch process the model can compute and try things in.

`run_cmd` can already run `python3 -c "..."`, so why this exists:

* **The code does not have to survive a JSON string.** A snippet handed to
  `run_cmd` is a shell argument inside a JSON value, so it has to be escaped
  twice, and a 4B model gets that wrong more often than it gets it right. Here
  the code arrives in a `<content>` block, byte for byte, exactly like a file
  body - the same trick that made `write_file` work at all.
* **It remembers.** One process stays alive across calls and keeps its globals,
  so a model that cannot hold much in its head can work in small steps:
  compute, look, compute again. `run_cmd` starts from nothing every time.
* **It is a scratchpad, not the project.** The process runs in its own
  directory, so a stray `open(..., "w")` lands there instead of in the
  repository - and never in a git commit.
* **The last expression's value comes back.** `2 ** 10` on its own prints
  nothing under `python -c`; here it answers `1024`. A small model reaches for
  a calculator far more readily when the calculator answers.

What this is *not*: a security boundary. The code runs as the user, with the
user's files and the user's network, and there is no attempt to pretend
otherwise - the isolation here is from *mistakes*, not from a hostile program.
That is why `run_python` still goes through the approval prompt and still counts
as changing the world (`tools._CHANGES_THINGS`). What it does buy is that a
runaway loop, a 40GB allocation or a crash takes down a separate process and
not the harness.

The wire between here and the scratch process, since it is easy to get wrong:

    parent                            kernel (_DRIVER, run as its own file)
    ------                            -------------------------------------
    stdin  pipe  --- one JSON line -> read from a dup of fd 0
                                      (fd 0 itself is /dev/null, so code that
                                       reads stdin cannot eat the protocol)
    stdout pipe  <-- one JSON line --- written to a dup of fd 1
                                      (fd 1 and 2 are the capture file, so
                                       print, a C extension and a subprocess
                                       all land in the same place)
    capture file <------------------- everything the code printed
    read by us from the offset we left off at

Stdlib only, plus `shell_session` for the one thing that is genuinely hard to
get right twice - killing a process along with everything it started.
"""

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time

from aetheris import paths
from aetheris import shell_session


RUNTIME_DIR = ".runtime"        # kernel source and capture files, out of the way


def _cfg(name, default):
    """Read a setting late. `config` builds the system prompt at import time."""
    from aetheris import config
    return getattr(config, name, default)


def scratch_dir() -> str:
    """Where the code runs. Its own directory, never the project's."""
    return paths.state("vm")


# ---------------------------------------------------------------------------
# the kernel
# ---------------------------------------------------------------------------

_DRIVER = r'''"""The scratch process behind run_python. Started by aetheris/vm.py.

Do not edit this copy - it is rewritten from `vm._DRIVER` whenever it differs.

One JSON request per line in, one JSON reply per line out, over dups of fd 0 and
fd 1 taken before anything is redirected. What the code prints goes to the
capture file named in argv[1]; the parent reads it from there, so output from a
C extension or a subprocess is caught too, and a reply cannot be mistaken for
output or the other way round.
"""

import ast
import builtins
import io
import json
import os
import sys
import traceback

KERNEL = os.path.abspath(__file__)
VALUE_CHARS = 2000


def _fresh():
    return {"__name__": "__vm__", "__doc__": None, "__builtins__": builtins}


def _below_kernel(tb):
    """The traceback with this file's own frames removed.

    The model wrote the code; it did not write the loop that ran it, and a
    frame it cannot see or fix is noise in front of the line that matters.
    """
    while tb is not None and os.path.abspath(
            tb.tb_frame.f_code.co_filename or "") == KERNEL:
        tb = tb.tb_next
    return tb


def _show(value):
    """`repr`, but nothing here may raise or run away."""
    try:
        text = repr(value)
    except BaseException as error:
        return f"<unprintable {type(value).__name__}: {error!r}>"
    if len(text) > VALUE_CHARS:
        text = text[:VALUE_CHARS] + f"...[{len(text) - VALUE_CHARS} more characters]"
    return text


# Two exceptions mean something specific about how this tool is being used
# rather than about the code, and saying so is worth more to a small model than
# the traceback is.
_HINTS = {
    "EOFError": ("The code asked for input that was not there. Put the answers "
                 "in a <stdin> block, one line per input() call."),
    "SystemExit": ("The code called exit() or sys.exit(). Nothing after that "
                   "ran; the variables it had set are still here."),
}


def _run(ns, code, stdin_text):
    before = set(ns)
    try:
        tree = ast.parse(code, "<vm>", "exec")
    except SyntaxError as error:
        return {"error": "".join(
            traceback.format_exception_only(type(error), error)).rstrip()}

    # The value of a trailing expression is answered, the way a REPL does it.
    # Without this, `2 ** 10` runs perfectly and reports nothing at all, and a
    # model that has been told this is a calculator stops believing it.
    tail = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tail = ast.Expression(tree.body.pop().value)

    value = error = None
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin_text)
    try:
        if tree.body:
            exec(compile(tree, "<vm>", "exec"), ns)
        if tail is not None:
            result = eval(compile(tail, "<vm>", "eval"), ns)
            if result is not None:
                ns["_"] = result
                value = _show(result)
    except BaseException as raised:
        error = "".join(traceback.format_exception(
            type(raised), raised, _below_kernel(raised.__traceback__))).rstrip()
        hint = _HINTS.get(type(raised).__name__)
        if hint:
            error += "\n" + hint
    finally:
        sys.stdin = saved_stdin

    return {"error": error, "value": value,
            "new": sorted(n for n in set(ns) - before if not n.startswith("_")),
            "kept": sorted(n for n in ns if not n.startswith("_"))}


def main():
    requests = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
    replies = os.dup(1)

    capture = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)         # so code reading stdin cannot eat the protocol
    os.dup2(capture, 1)
    os.dup2(capture, 2)
    os.close(devnull)
    os.close(capture)

    ns = _fresh()
    while True:
        line = requests.readline()
        if not line:
            return                              # the parent has gone
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if request.get("reset"):
            ns = _fresh()
        reply = _run(ns, request.get("code") or "", request.get("stdin") or "")
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os.write(replies, (json.dumps(reply) + "\n").encode("utf-8", "replace"))


if __name__ == "__main__":
    main()
'''


try:
    import resource                      # POSIX only; absent on Windows
except ImportError:
    resource = None


def _limiter(memory_mb: int, file_mb: int):
    """A `preexec_fn` that puts ceilings on the child the child cannot lift.

    A ceiling where the platform provides one, and never the thing keeping a
    runaway contained - `Kernel.run`'s wall clock is. Linux enforces
    RLIMIT_AS, so an over-large allocation becomes a MemoryError the model can
    read and the kernel survives it. Darwin accepts the same call and does not
    enforce it, and Windows has no `resource` module at all; on both the
    allocation runs until the wall clock stops it. `test_vm.py` asserts what
    each platform actually provides rather than what was asked for - CI on
    macOS is what turned that from an assumption into a measurement.

    Everything it needs is captured here, in the parent, on purpose: a
    `preexec_fn` runs between fork and exec in a process that has the parent's
    threads' locks and none of its threads, so importing a module or reading
    `config` there can deadlock. The closure only calls `setrlimit`.

    RLIMIT_CPU is deliberately not among them. It counts CPU seconds over the
    whole life of the process, and this process is meant to live for the whole
    session - a limit sized for one call would kill a healthy kernel after
    twenty of them. A loop that will not end is a wall-clock problem and is
    handled as one.
    """
    if resource is None:
        return None
    megabyte = 1024 * 1024
    wanted = []
    for name, size in (("RLIMIT_AS", memory_mb), ("RLIMIT_FSIZE", file_mb)):
        limit = getattr(resource, name, None)
        if limit is not None and size > 0:      # 0 or less means "do not cap it"
            wanted.append((limit, size * megabyte))
    if getattr(resource, "RLIMIT_CORE", None) is not None:
        wanted.append((resource.RLIMIT_CORE, 0))
    if not wanted:
        return None

    def apply():
        for limit, value in wanted:
            try:
                soft, hard = resource.getrlimit(limit)
                ceiling = value if hard in (resource.RLIM_INFINITY, -1) else min(value, hard)
                resource.setrlimit(limit, (ceiling, hard))
            except (ValueError, OSError):
                pass          # a limit the platform will not take is not fatal

    return apply


class Kernel:
    """One live scratch process, and the namespace it is holding."""

    def __init__(self):
        self.process = None
        self.capture = ""
        self.offset = 0
        self.started = 0.0
        self.calls = 0
        self._replies: queue.Queue = queue.Queue()
        self._stderr = ""

    # -- lifecycle --------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _runtime(self) -> str:
        runtime = os.path.join(scratch_dir(), RUNTIME_DIR)
        os.makedirs(runtime, exist_ok=True)
        return runtime

    def _kernel_source(self, runtime: str) -> str:
        """The driver on disk, rewritten whenever this module's copy differs."""
        path = os.path.join(runtime, "kernel.py")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                if handle.read() == _DRIVER:
                    return path
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_DRIVER)
        return path

    def start(self) -> str:
        """Launch the scratch process. Returns "" or why it could not start."""
        self.close()
        try:
            workspace = os.getcwd()
            scratch = scratch_dir()
            os.makedirs(scratch, exist_ok=True)
            runtime = self._runtime()
            kernel = self._kernel_source(runtime)
            self.capture = os.path.join(runtime, f"out-{os.getpid()}.txt")

            environment = dict(os.environ)
            # The project is importable, so a function that has just been
            # written can be tried - but nothing is written back into it. In
            # particular no __pycache__: that would put files the model never
            # asked for into somebody's git status.
            environment["PYTHONPATH"] = os.pathsep.join(
                [workspace] + ([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else []))
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"

            self.process = subprocess.Popen(
                [sys.executable, "-u", kernel, self.capture],
                cwd=scratch, env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name != "nt"),
                preexec_fn=_limiter(int(_cfg("VM_MEMORY_MB", 512)),
                                    int(_cfg("VM_FILE_MB", 64))),
            )
        except Exception as error:
            self.process = None
            return f"the Python VM could not be started: {error}"

        self.offset = 0
        self.started = time.time()
        self.calls = 0
        self._replies = queue.Queue()
        self._stderr = ""
        # Both readers are handed what they read rather than reaching back
        # through `self`: `close()` clears `self.process`, and a thread that
        # started a moment too late would raise into the terminal mid-tool,
        # which is the one thing invariant 5.9 asks nothing to do.
        threading.Thread(target=self._read_replies, daemon=True,
                         args=(self.process.stdout, self._replies)).start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         args=(self.process.stderr,)).start()
        return ""

    @staticmethod
    def _read_replies(stream, replies) -> None:
        try:
            for line in stream:
                replies.put(line)
        except Exception:
            pass
        replies.put(None)               # the kernel is gone

    def _read_stderr(self, stream) -> None:
        """Only ever a startup failure: fd 2 is the capture file after that."""
        try:
            self._stderr = stream.read().decode("utf-8", "replace")
        except Exception:
            pass

    def close(self) -> None:
        if self.process is not None:
            shell_session.kill_process_tree(self.process)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        self.process = None
        if self.capture:
            try:
                os.remove(self.capture)
            except OSError:
                pass

    # -- running ----------------------------------------------------------

    def _printed(self) -> str:
        """What the code has put in the capture file since the last read."""
        try:
            with open(self.capture, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
            self.offset += len(data)
        except OSError:
            return ""
        return data.decode("utf-8", "replace").replace("\r\n", "\n")

    def run(self, code: str, stdin_text: str = "", reset: bool = False) -> dict:
        """Run one snippet. Always returns a dict; never raises.

        `crashed` and `timeout` are separate outcomes from `error`, because they
        mean the namespace is gone and the next call starts from nothing - which
        the model has to be told, or it will go on referring to a variable that
        no longer exists.
        """
        timeout = float(_cfg("VM_TIMEOUT", 20))

        if not self.alive:
            failure = self.start()
            if failure:
                return {"crashed": failure}
            reset = False               # a fresh kernel is already empty

        request = json.dumps({"code": code, "stdin": stdin_text, "reset": bool(reset)})
        began = time.time()
        try:
            self.process.stdin.write((request + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except Exception as error:
            self.close()
            return {"crashed": f"the Python VM stopped listening ({error})"}

        try:
            line = self._replies.get(timeout=timeout)
        except queue.Empty:
            printed = self._printed()
            self.close()
            return {"timeout": timeout, "output": printed,
                    "seconds": time.time() - began}

        if line is None:
            printed = self._printed()
            detail = (self._stderr or "").strip().splitlines()
            self.close()
            return {"crashed": ("the code stopped the Python VM itself - a crash, "
                                "an os._exit(), or more memory than "
                                f"{_cfg('VM_MEMORY_MB', 512)}MB"),
                    "output": printed,
                    "detail": detail[-1] if detail else ""}

        printed = self._printed()
        try:
            reply = json.loads(line)
        except ValueError:
            self.close()
            return {"crashed": "the Python VM sent something unreadable",
                    "output": printed}

        self.calls += 1
        reply["output"] = printed
        reply["seconds"] = time.time() - began
        return reply


_kernel = Kernel()


def run(code: str, stdin_text: str = "", reset: bool = False) -> dict:
    return _kernel.run(code, stdin_text, reset)


def state() -> dict:
    """What `/vm` reports: whether it is up, and what it is holding."""
    return {"alive": _kernel.alive, "calls": _kernel.calls,
            "directory": scratch_dir(),
            "age": time.time() - _kernel.started if _kernel.alive else 0.0}


def shutdown() -> None:
    _kernel.close()


atexit.register(shutdown)


if __name__ == "__main__":
    print("This file can not run directly.")
