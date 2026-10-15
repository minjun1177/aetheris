"""`/rewind`: take back the last exchange, and the prompt that started it.

A small model that goes wrong does not stay wrong quietly. Its failed calls,
the error it argued with and the plan it abandoned all stay in the context, and
the next answer is written on top of them - so a bad turn costs the turns after
it as well. Asking it to "ignore that" adds to the pile. Taking it back removes
it.

Why snapshots rather than cutting the history at a user message: the history
cannot say which user message a person typed. Tool results, channel notes,
nudges and deepthink's stages are all `user` messages too, and compaction
rewrites the early part of the list, so an index taken at the start of a turn
is wrong by the end of it. A marker on the message itself would be sent to the
provider with it. So the list is copied as it stood just before each prompt was
added - one shallow copy per turn, the last `KEEP` of them, for this run only.

Files are not touched. `/undo` takes AI commits back one at a time and refuses
what it did not make; a rewind that reverted files as well would need that
same care for several at once. It says which commits the dropped turns left
behind instead.
"""

from aetheris import config, git_ops

KEEP = 20

_points: list = []


def mark(messages: list, prompt: str) -> None:
    """Remember `messages` as they are, before `prompt` is added to them."""
    _points.append({
        "messages": [dict(m) for m in messages],
        "prompt": prompt,
        "head": git_ops.head(),
        "todos": [dict(t) for t in config.TODOS],
    })
    del _points[:-KEEP]


def forget() -> None:
    """A different conversation: none of these belong to it."""
    _points.clear()


def available() -> int:
    return len(_points)


def rewind(messages: list, turns: int = 1) -> dict | None:
    """Put `messages` back as they were before the last `turns` prompts.

    Changes the list in place, so every holder of it sees the rewind. Returns
    what was taken back - the prompt that started the first dropped turn, and
    the AI commits those turns left on disk - or None when there are not that
    many turns to go back.
    """
    if turns < 1 or turns > len(_points):
        return None
    point = _points[-turns]
    del _points[-turns:]
    messages[:] = point["messages"]
    config.TODOS[:] = point["todos"]
    return {"prompt": point["prompt"],
            "commits": git_ops.ai_commits_since(point["head"])}
