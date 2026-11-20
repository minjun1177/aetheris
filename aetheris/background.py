"""Commands the model starts and does not wait for.

`run_cmd` stays connected to a command until it ends or falls quiet, which is
right for a test run and wrong for a dev server, a watcher, or a build that
takes ten minutes: the turn either sits waiting for it or the command is
reported "waiting for input" and killed when its session times out. With
`background` set the command is started here instead and the call returns at
once with a job id.

What makes a background job worth having is being told when it ends, without
anybody remembering to look. So a job that finishes is queued (`take_finished`)
and the harness passes it on at the first moment the model can use it:

* in the middle of a turn, as a note ahead of the next request
  (`llm_client.chat_turn`, beside the channel's own note);
* at the start of the next turn the person types (`app.main`);
* at an idle, empty prompt, by starting a turn of its own (`app._watch_tasks`,
  under `BACKGROUND_WAKE`) - the same way the channel answers a question that
  arrives while nobody is at the keyboard.

Each job's output is drained by a thread for as long as it runs and the newest
`BACKGROUND_OUTPUT_CHARS` of it kept, so `read_background` can show it at any
point. Its stdin is closed at the start: nobody is there to answer a prompt,
and a program that asks one gets end-of-file instead of hanging for ever.

Stdlib only, like `shell_session`, whose `Session` this reuses.
"""

import atexit
import itertools
import threading
import time

from aetheris import shell_session

_counter = itertools.count(1)
_jobs: "dict[str, Job]" = {}
_lock = threading.Lock()


def _cfg(name):
    from aetheris import config
    return getattr(config, name)


class Job:
    """One command running with nobody waiting on it."""

    def __init__(self, command: str, session):
        self.id = f"b{next(_counter)}"
        self.command = command
        self.session = session
        self.started = time.time()
        self.ended_at = 0.0
        self.code = None            # the exit status, once it has one
        self.stopped = False        # ended by `stop`, not on its own
        self.reported = False       # the model has been told it ended
        self._text = ""
        self._dropped = 0           # characters let go of off the front
        self._read_to = 0           # how much `read_background` has handed over
        self._done = threading.Event()
        threading.Thread(target=self._collect, name=f"background-{self.id}",
                         daemon=True).start()

    def _collect(self) -> None:
        limit = max(1000, int(_cfg("BACKGROUND_OUTPUT_CHARS")))
        closed = False
        dead_quiet_since = None
        while True:
            text, closed = self.session.drain(0.5 if not closed else 0.1)
            if text:
                with _lock:
                    self._text += text
                    if len(self._text) > limit:
                        cut = len(self._text) - limit
                        self._text = self._text[cut:]
                        self._dropped += cut
            if self.session.alive:
                dead_quiet_since = None
                continue
            if closed:
                break
            # The shell has exited and the pipes are still open. Either the
            # pumps have not caught up with its last words yet - a command that
            # ends at once exits before its output is queued - or something it
            # started holds them. Quiet for a second after the exit settles it.
            if text or dead_quiet_since is None:
                dead_quiet_since = time.time()
            elif time.time() - dead_quiet_since >= 1.0:
                break
        try:
            self.code = self.session.process.wait(timeout=3)
        except Exception:
            self.code = None
        self.ended_at = time.time()
        self._done.set()

    # -- what it has said -------------------------------------------------

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    @property
    def seconds(self) -> float:
        return (self.ended_at or time.time()) - self.started

    def output(self, tail: int = 0) -> str:
        """Everything kept, or its last `tail` characters."""
        with _lock:
            text = self._text
            dropped = self._dropped
        if tail and len(text) > tail:
            return "...[earlier output trimmed]\n" + text[-tail:]
        if dropped:
            return "...[earlier output trimmed]\n" + text
        return text

    def unread(self, tail: int) -> str:
        """What arrived since the last call, at most its last `tail` characters."""
        with _lock:
            start = max(0, self._read_to - self._dropped)
            fresh = self._text[start:]
            self._read_to = self._dropped + len(self._text)
        if len(fresh) > tail:
            return "...[earlier output trimmed]\n" + fresh[-tail:]
        return fresh

    def state(self) -> str:
        if self.running:
            return f"running for {_took(self.seconds)}"
        if self.stopped:
            return f"stopped after {_took(self.seconds)}"
        if self.code:
            return f"failed (exit code {self.code}) after {_took(self.seconds)}"
        return f"finished (exit code {self.code if self.code is not None else '?'}) after {_took(self.seconds)}"

    def wait(self, timeout: float = None) -> bool:
        return self._done.wait(timeout)


def _took(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


# ---------------------------------------------------------------------------
# the jobs
# ---------------------------------------------------------------------------

def running() -> "list[Job]":
    return [job for job in _jobs.values() if job.running]


def jobs() -> "list[Job]":
    """Every job this session started, oldest first."""
    return sorted(_jobs.values(), key=lambda job: job.started)


def get(job_id: str) -> "Job | None":
    return _jobs.get(str(job_id or "").strip())


def start(command: str, env: dict | None = None) -> Job:
    """Start `command` in the background. Raises OSError, or RuntimeError when full."""
    limit = max(1, int(_cfg("BACKGROUND_MAX_JOBS")))
    if len(running()) >= limit:
        raise RuntimeError(f"{limit} background commands are already running")
    session = shell_session.start(command, env=env)
    try:
        if session.process.stdin is not None:
            session.process.stdin.close()
    except Exception:
        pass
    job = Job(command, session)
    _jobs[job.id] = job
    return job


def stop(job_id: str) -> "Job | None":
    """End a job and everything it started. None if there is no such job."""
    job = get(job_id)
    if job is None:
        return None
    if job.running:
        job.stopped = True
        job.reported = True         # stopped on purpose: nothing to be told about
        shell_session.kill_process_tree(job.session.process)
        job.wait(5)
    return job


def take_finished() -> "list[Job]":
    """The jobs that have ended since the model was last told. Each is handed out once."""
    finished = []
    for job in jobs():
        if not job.running and not job.reported:
            job.reported = True
            finished.append(job)
    return finished


def describe(job: Job, tail: int = 2000) -> str:
    """One finished job, as the model is told about it."""
    output = job.output(tail).rstrip()
    head = (f"[Background] Job {job.id} (`{job.command}`) {job.state()}.")
    if output:
        return f"{head} The end of its output:\n{output}"
    return f"{head} It printed nothing."


def note(jobs_ended: list) -> str:
    """The message that passes ended jobs on, or "" for none."""
    if not jobs_ended:
        return ""
    parts = [describe(job) for job in jobs_ended]
    parts.append("Read more of a job's output with read_background if you need it.")
    return "\n\n".join(parts)


def shutdown() -> None:
    for job in list(_jobs.values()):
        if job.running:
            job.stopped = True
            shell_session.kill_process_tree(job.session.process)


atexit.register(shutdown)
