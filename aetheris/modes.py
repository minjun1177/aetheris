"""The permission mode: how much the model may do before it asks.

Five switches used to be five separate things to remember - `/set PLANMODE`,
`/set DEEPTHINK`, `/set AUTO_ALLOW` - with no single answer to "what will it do
if I send this?". A mode is that answer, and Shift+Tab at the prompt moves
through them in order:

    manual -> plan -> accept-edits -> deepthink -> auto -> manual

- **manual**: every guarded tool asks first.
- **plan**: the model submits a plan for approval before it changes anything.
- **accept-edits**: writing, editing, copying and moving files inside the
  project runs without asking; commands, deletes and anything outside the
  project still ask.
- **deepthink**: one request becomes the deepthink pipeline (`deepthink`).
- **auto**: every guarded tool runs without asking.

The settings stay the storage - a mode is a combination of them - so `/set`,
the old switches and a settings file all keep working, and choosing a mode
changes this session only, the way the switches always did.
"""
from aetheris import config
from aetheris.config import S

MODES = ("manual", "plan", "accept-edits", "deepthink", "auto")

# What the line under the prompt says, and in which colour.
_SHOWN = {
    "manual": ("⏸ manual", "MUTED"),
    "plan": ("⏸ plan mode on", "INFO"),
    "accept-edits": ("⏵⏵ accept edits on", "ACCENT"),
    "deepthink": ("✻ deepthink on", "OK"),
    "auto": ("⏵⏵⏵ auto mode on", "WARN"),
}

# What each one means, for `/mode`.
ABOUT = {
    "manual": "every guarded tool asks first",
    "plan": "a plan is submitted for approval before anything changes",
    "accept-edits": "file edits inside the project run; commands still ask",
    "deepthink": "one request becomes plan, check, build, review and verify",
    "auto": "every guarded tool runs without asking",
}

# The tools accept-edits waves through: the ones that put a file in place.
# `delete_file` is not one of them - taking a file away is not an edit.
ACCEPTED_EDITS = frozenset({"write_file", "edit_file", "multi_edit",
                            "create_dir", "copy_file", "move_file"})


def current() -> str:
    """The mode the settings add up to. The most permissive one wins."""
    if config.AUTO_ALLOW:
        return "auto"
    if config.DEEPTHINK:
        return "deepthink"
    if config.ACCEPT_EDITS:
        return "accept-edits"
    if config.PLANMODE:
        return "plan"
    return "manual"


def set_mode(name: str) -> str:
    """Switch to `name`, for this session. Returns the mode now in force."""
    name = (name or "").strip().lower().replace("_", "-").replace(" ", "-")
    if name not in MODES:
        raise ValueError(f"'{name}' is not a mode - one of {', '.join(MODES)}")
    config.AUTO_ALLOW = name == "auto"
    config.DEEPTHINK = name == "deepthink"
    config.ACCEPT_EDITS = name == "accept-edits"
    planning = name == "plan"
    config.PLANMODE = planning
    if planning:
        _plan_anew()
    return name


def cycle() -> str:
    """The next mode in `MODES`, switched to. What Shift+Tab does."""
    return set_mode(MODES[(MODES.index(current()) + 1) % len(MODES)])


def _plan_anew() -> None:
    # On Attacca, plan mode starts a plan-mode job with the next message; said
    # again, it means "plan the next thing too" - as `/planmode on` does.
    try:
        from aetheris import providers
        if providers.current().drives_turns:
            from aetheris import attacca
            attacca.plan_anew()
    except Exception:
        pass                  # a mode switch is never what breaks the prompt


def status_line() -> str:
    """The line under the prompt: the mode, and how to change it."""
    label, colour = _SHOWN[current()]
    return (f"  {getattr(S, colour)}{label}{S.R}"
            f"{S.MUTED} (shift+tab to cycle){S.R}")
