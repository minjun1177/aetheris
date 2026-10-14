# Deepthink

[← README](../README.md)

## Deepthink

Off by default. `/deepthink on` turns one request into six turns:

```
1  Plan      work out what it takes - read the files, change nothing
2  Check     argue against that plan and settle every assumption
3  Implement carry out the plan as it now stands
4  Review    read the diff of what actually changed, and list what is wrong
5  Revise    fix what the review found, and nothing else
6  Verify    run it, check it against the plan, report what really came back
```

All six are one conversation, so each stage sees everything the ones before it
did. What changes is the instruction at the top of each turn.

Asked to implement something, a model goes straight at it. It writes code from
what it remembers of a file rather than what the file says, and when it is done
it reports success without running the thing. Both come from the same place -
one pass, with no step whose only job is to find fault.

**Finding and fixing are two stages, not one.** Review used to do both, and a
stage that is allowed to fix stops looking as soon as it has something to fix -
so the rest of its own list went unread. Review is now read-only and its whole
output is a numbered list of what is wrong; stage 5 turns the tools back on and
works through that list, and is told not to widen it, because a change nobody
reviewed is a change nobody checked. An empty list means stage 5 changes
nothing, which is a result rather than an idle turn to fill.

**Three of the six carry the mode.** Stage 2 is the only one asked to prove the
plan wrong, and a plan nobody argued with is usually the one that fails. Stage 4
is handed the **real `git diff`** rather than being asked what it changed:
reviewing from memory finds nothing, because the memory is of the intention, not
of the code. Without git - no repository, or `/autocommit off` - it is told to
re-read the files instead. Stage 6 goes back to the plan and checks it item by
item, because code that runs and is not what was agreed is still not finished.

**The stages that are meant to think cannot edit.** Not "are asked not to" - the
tools that change things are switched off in stages 1, 2 and 4, and a model that
tries one is told to say what it would change instead. Telling a model to hold
off does not hold it off; a local 4B model tried to edit fifteen times in the
planning stage before this was enforced.

**It stops early when there is nothing to build.** A question costs one turn,
not six: the plan stage marks it, and if the model forgets to, the plan itself
is read back in one short call to decide. Anything unclear counts as work to do.
A build stage that changed nothing also ends the chain rather than reviewing and
verifying work that was never done.

**And it goes round again when six turns were not enough.** A stage 6 that finds
half the plan undone used to have nowhere to put that finding - the chain ended
and handed the report back as the answer. Now it says so, with a marker or in a
short read-back of its own report, and the chain **starts over at stage 1**:
never in the middle, because what is left after a failed pass is a different
piece of work and planning it is the part that was missing. The next pass is
told to finish what the report named and not to widen it, and it ends after one
turn if there is nothing left after all. `DEEPTHINK_MAX_PASSES` (3) is the
ceiling. This gate leans the opposite way to the one above: anything unclear
counts as **finished**, because a chain that sets itself off again on a maybe
does not end.

```
/deepthink            the stages, and whether it is on
/deepthink on|off     turn it on or off
/set DEEPTHINK_MAX_PASSES 3    how many times it may start over
```

Deepthink supersedes plan mode while it is on, so `/planmode` injects nothing -
two sets of planning instructions only contradict each other.
