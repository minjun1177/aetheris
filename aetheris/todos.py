"""The model's checklist for a task of several steps, kept by the harness.

What a small model loses first on a long job is its place: it does step three
twice, or stops after four because the list it wrote at the start has scrolled
out of what it attends to. So the list is not left in the conversation to be
remembered. `update_todos` hands it to the harness, which keeps it with the
session and puts the open items back in front of the model at the end of every
request - short, and only while something is left to do.

The reminder is added to what is *sent*, never to the stored history: written
into the conversation it would pile up a copy per request, and the summariser
and the session file would carry all of them.
"""

import re

from aetheris import config

MAX_ITEMS = 30
MAX_TEXT = 200
REMINDER_ITEMS = 5           # open items spelled out in the reminder; the rest are counted

_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s*")
_DONE = re.compile(r"^\s*(?:\[[xX✓✔]\]|\[done\]|✓|✔|☑)\s*", re.IGNORECASE)
_OPEN = re.compile(r"^\s*(?:\[\s?\]|\[todo\]|☐)\s*", re.IGNORECASE)
_DONE_STATUSES = ("done", "completed", "complete", "finished", "x")


def _item(text: str, done: bool) -> dict:
    text = " ".join(str(text).split())
    return {"text": text[:MAX_TEXT], "done": bool(done)}


def _from_line(line: str):
    line = _BULLET.sub("", line, count=1)
    done = bool(_DONE.match(line))
    line = _DONE.sub("", line, count=1) if done else _OPEN.sub("", line, count=1)
    line = _BULLET.sub("", line, count=1)       # "[ ] 1. step" as well as "1. [ ] step"
    return _item(line, done) if line.strip() else None


def parse(todos) -> list:
    """Whatever shape the model sent, as [{"text", "done"}].

    Text, one item per line with `[x]` for a finished one, is what the prompt
    asks for. A list of strings, or of objects with a text and a done flag or a
    status, is what a model trained on other harnesses sends instead, and it is
    taken as it comes rather than refused for its shape.
    """
    if isinstance(todos, str):
        items = [_from_line(line) for line in todos.splitlines()]
    elif isinstance(todos, (list, tuple)):
        items = []
        for entry in todos:
            if isinstance(entry, dict):
                text = next((entry[k] for k in ("text", "content", "task", "title", "item")
                             if isinstance(entry.get(k), str)), "")
                status = str(entry.get("status", "")).strip().lower()
                done = entry.get("done") is True or status in _DONE_STATUSES
                items.append(_item(text, done) if text.strip() else None)
            elif entry is not None:
                items.append(_from_line(str(entry)))
    else:
        items = []
    return [item for item in items if item][:MAX_ITEMS]


def render(items: list) -> str:
    return "\n".join(f"[{'x' if item['done'] else ' '}] {number}. {item['text']}"
                     for number, item in enumerate(items, 1))


def handle_update_todos(todos) -> str:
    items = parse(todos)
    if not items and str(todos or "").strip() not in ("", "[]"):
        return ("[Error] No items could be read from that. Send one item per line, "
                "with [x] in front of a finished one and [ ] in front of an open one.")
    config.TODOS[:] = items
    if not items:
        return "[Success] The todo list is cleared."
    left = [i for i in items if not i["done"]]
    done = len(items) - len(left)
    status = ("all done" if not left else f"{done} of {len(items)} done")
    return f"[Success] Todo list saved - {status}.\n{render(items)}"


def reminder(items: list | None = None) -> str:
    """The open items as one short note, or "" when nothing is left."""
    items = config.TODOS if items is None else items
    numbered = [(n, i) for n, i in enumerate(items, 1) if not i.get("done")]
    if not numbered:
        return ""
    shown = "; ".join(f"{n}. {i['text']}" for n, i in numbered[:REMINDER_ITEMS])
    more = (f"; and {len(numbered) - REMINDER_ITEMS} more"
            if len(numbered) > REMINDER_ITEMS else "")
    return (f"[Todo - {len(numbered)} of {len(items)} left: {shown}{more}. "
            f"Mark each one done with update_todos as you finish it.]")


def with_reminder(messages: list) -> list:
    """`messages` as they should be sent: the reminder on the last user message.

    A copy whenever there is a reminder; the list passed in is never changed.
    """
    note = reminder()
    if not note or not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "user" or not isinstance(last.get("content"), str):
        return messages + [{"role": "user", "content": note}]
    return messages[:-1] + [dict(last, content=f"{last['content']}\n\n{note}")]


def restore(saved) -> None:
    """Adopt the list a session file kept, if it is one."""
    config.TODOS[:] = [_item(i.get("text", ""), i.get("done"))
                       for i in (saved if isinstance(saved, list) else [])
                       if isinstance(i, dict) and str(i.get("text", "")).strip()][:MAX_ITEMS]
