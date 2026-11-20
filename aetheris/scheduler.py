"""Prompts sent later, or again and again: `/loop` and `/schedule`.

A task is a line to type into this session's prompt at a time: once
(`/schedule in 20m check the deploy`) or on an interval
(`/loop 5m check the CI run and fix what fails`). It is typed the way the
channel types a line for an agent nobody is sitting at - `app._watch_tasks`
closes an *empty* prompt with it - so a task is exactly what the person would
have typed: a message, or a slash command (`/loop 30m /compact`).

Three rules keep it from getting in anybody's way:

* **only an empty prompt.** A half-typed line is the person's; the task waits
  for it to be sent or cleared, and goes next;
* **never in the middle of a turn.** A task that falls due while the model is
  working goes when the prompt opens again, once - not once for every interval
  that passed meanwhile;
* **the session's, not the machine's.** Tasks live as long as this process.
  Nothing is left running after it exits, and nothing starts by itself the next
  time it is opened.

Stdlib only, importing nothing local: what time it is and what is due is all
this knows. Who types the line, and when the prompt is free, is `app`'s.
"""

import datetime
import itertools
import re
import time

MIN_INTERVAL = 10           # seconds; a loop tighter than this is a busy-wait on a model

_loops = itertools.count(1)
_timers = itertools.count(1)
_tasks: "dict[str, Task]" = {}


class Task:
    def __init__(self, task_id: str, prompt: str, due: float, every: float = 0.0):
        self.id = task_id
        self.prompt = prompt
        self.due = due
        self.every = every          # 0 for a one-off
        self.runs = 0
        self.created = time.time()

    @property
    def repeats(self) -> bool:
        return self.every > 0


# ---------------------------------------------------------------------------
# reading what was typed
# ---------------------------------------------------------------------------

_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400}
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]+)$")
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")


def parse_interval(text: str) -> float:
    """`90s`, `5m`, `1h30m`, `2 hours` as seconds. 0 when it is not one."""
    text = (text or "").strip().lower()
    if not text:
        return 0.0
    whole = _DURATION.match(text)
    if whole:
        unit = _UNITS.get(whole.group(2))
        return float(whole.group(1)) * unit if unit else 0.0
    parts = _PART.findall(text)
    if not parts or _PART.sub("", text).strip():
        return 0.0
    total = 0.0
    for number, unit in parts:
        if unit not in _UNITS:
            return 0.0
        total += float(number) * _UNITS[unit]
    return total


def split_interval(text: str) -> tuple:
    """(seconds, the rest) for `5m check the CI`, or (0, text) if it does not start with one.

    Two words are tried before one, so `2 hours check it` reads as two hours.
    """
    words = (text or "").strip().split()
    for take in (2, 1):
        if len(words) > take - 1:
            seconds = parse_interval(" ".join(words[:take]))
            if seconds > 0:
                return seconds, " ".join(words[take:])
    return 0.0, (text or "").strip()


_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ t](\d{1,2}:\d{2})$")


def split_when(text: str, now: float = None) -> tuple:
    """(epoch, the rest) for when a `/schedule` should run. (0, why) when it cannot tell.

    `in 20m ...`, `20m ...`, `at 14:30 ...`, `14:30 ...`, `2026-11-21 09:00 ...`.
    A clock time already past today is tomorrow's.
    """
    now = time.time() if now is None else now
    words = (text or "").strip().split()
    if not words:
        return 0.0, "say when: in 20m, at 14:30, or 2026-11-21 09:00"
    lead = words[0].lower()
    if lead == "in":
        seconds, rest = split_interval(" ".join(words[1:]))
        if seconds <= 0:
            return 0.0, f"'{' '.join(words[1:3])}' is not a length of time - try 20m or 1h30m"
        return now + seconds, rest
    if lead == "at":
        words = words[1:]
        if not words:
            return 0.0, "at what time? e.g. at 14:30"
    if len(words) >= 2 and _STAMP.match(f"{words[0]} {words[1]}"):
        stamp = _STAMP.match(f"{words[0]} {words[1]}")
        try:
            when = datetime.datetime.strptime(f"{stamp.group(1)} {stamp.group(2)}",
                                              "%Y-%m-%d %H:%M")
        except ValueError:
            return 0.0, f"'{words[0]} {words[1]}' is not a real date and time"
        if when.timestamp() <= now:
            # A clock time rolls on to tomorrow; a date cannot, and run at once
            # is not what anybody who wrote a date meant.
            return 0.0, f"{words[0]} {words[1]} has already passed"
        return when.timestamp(), " ".join(words[2:])
    clock = _CLOCK.match(words[0])
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            return 0.0, f"'{words[0]}' is not a time of day"
        base = datetime.datetime.fromtimestamp(now)
        when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when.timestamp() <= now:
            when += datetime.timedelta(days=1)
        return when.timestamp(), " ".join(words[1:])
    if lead != "at":
        seconds, rest = split_interval(" ".join(words))
        if seconds > 0:
            return now + seconds, rest
    return 0.0, f"'{words[0]}' is not a time - try in 20m, at 14:30 or 2026-11-21 09:00"


# ---------------------------------------------------------------------------
# the tasks
# ---------------------------------------------------------------------------

def add_loop(every: float, prompt: str, now: float = None, first_now: bool = False) -> Task:
    """Send `prompt` every `every` seconds - the first time now, or one interval from now.

    Now is what `/loop` asks for: the person sees at once that the prompt does
    what they meant, rather than finding out five minutes later that it did not.
    """
    now = time.time() if now is None else now
    every = max(float(every), MIN_INTERVAL)
    task = Task(f"l{next(_loops)}", prompt.strip(), now if first_now else now + every, every)
    _tasks[task.id] = task
    return task


def add_once(due: float, prompt: str) -> Task:
    task = Task(f"t{next(_timers)}", prompt.strip(), float(due))
    _tasks[task.id] = task
    return task


def tasks() -> "list[Task]":
    """Every task still to run, soonest first."""
    return sorted(_tasks.values(), key=lambda task: task.due)


def cancel(task_id: str) -> "list[Task]":
    """Drop one task by id, or every one with `all`. Returns what was dropped."""
    wanted = (task_id or "").strip().lower()
    if wanted == "all":
        dropped = tasks()
        _tasks.clear()
        return dropped
    task = _tasks.pop(wanted, None)
    return [task] if task else []


def due(now: float = None) -> "Task | None":
    """The task to type now, if one is due - and move it on.

    A one-off is removed; a loop's next run is one interval from *now*, not
    from when it was due, so a loop that was held up by a long turn does not
    fire several times in a row to catch up.
    """
    now = time.time() if now is None else now
    ready = [task for task in _tasks.values() if task.due <= now]
    if not ready:
        return None
    task = min(ready, key=lambda t: t.due)
    task.runs += 1
    if task.repeats:
        task.due = now + task.every
    else:
        _tasks.pop(task.id, None)
    return task


def clear() -> None:
    _tasks.clear()


def until(task: Task, now: float = None) -> str:
    """How long until `task` runs, short."""
    now = time.time() if now is None else now
    seconds = max(0, int(task.due - now))
    return "now" if seconds == 0 else f"in {every_text(seconds)}"


def every_text(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60}m"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    if seconds > 60:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds}s"
