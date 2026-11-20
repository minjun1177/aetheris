"""What a turn does, as it does it, for a program reading `-p --output-format stream-json`.

`--json` prints one object when the turn is over. Something driving the harness
from a script or an editor wants to show the answer arriving and see each tool
call as it starts, which is one line of JSON per event:

    {"type": "start", ...}
    {"type": "text", "text": "..."}
    {"type": "tool_use", "name": "...", "arguments": {...}}
    {"type": "tool_result", "name": "...", "outcome": "ok", "seconds": 0.1, "result": "..."}
    {"type": "result", ...the --json object...}

Nobody listens unless `listen` was called, and then `emit` costs one call.
Stdlib only, importing nothing local, so the turn loop and the tool dispatcher
can both use it.
"""

_sink = None
RESULT_CHARS = 4000     # of a tool result, what goes out on the stream


def listen(sink) -> None:
    """Hand every event to `sink(dict)` from now on. None stops it."""
    global _sink
    _sink = sink


def listening() -> bool:
    return _sink is not None


def emit(kind: str, **fields) -> None:
    """Send one event, if anybody is listening. A broken listener is not the turn's problem."""
    if _sink is None:
        return
    try:
        _sink({"type": kind, **fields})
    except Exception:
        pass
