"""A mode that makes one request into a chain: plan, check, build, review, fix, verify.

Asked to implement something, a model goes straight at it. It writes code from
what it remembers of a file rather than what the file says, and when it is done
it reports success without ever running the thing. Both failures come from the
same place: one pass, with no step that exists only to find fault.

So the turn is broken into six, and the harness drives them rather than asking
the model to remember to:

    1  plan     work out what it takes - read the files, change nothing
    2  check    argue against that plan and settle every assumption
    3  build    carry out the plan as it now stands
    4  review   read the diff of what actually changed, and list what is wrong
    5  revise   fix what the review found, and nothing else
    6  verify   run it, check it against the plan, report what really came back

Each stage sees everything the ones before it did, so it is one conversation,
not six. What changes is the instruction at the top of each turn.

Finding and fixing are two stages, not one, for the same reason 1 and 2 are.
A stage allowed to fix stops looking as soon as it has something to fix, so
review is read-only and only writes the list; revise turns the tools back on and
works through it. Review used to do both and the second half of its list went
unread.

Stage 2 is the only one whose job is to find the plan wrong, and a plan nobody
argued with is usually the one that fails. Stage 4 is handed the **real diff**
from git rather than being asked what it changed - reviewing from memory finds
nothing, because the memory is of the intention, not of the code. Stage 6 goes
back to the plan, because code that runs and is not what was agreed is still not
finished.

Six stages are not always enough. A verify stage that finds half the plan still
undone has nowhere to put that finding: the chain ends, and the report of
unfinished work is handed back as the answer. So when stage 6 says the job is
not done, the whole chain runs again - from stage 1, never from the middle,
because what is left after a failed pass is a different piece of work and
planning it is the part that was missing. `DEEPTHINK_MAX_PASSES` is the ceiling;
a model that is never satisfied would otherwise never stop.

The mode is off by default and toggled with `/deepthink`. It costs six turns
where one would do, which is worth it for a change to real code and a waste for
a question - so stage 1 is allowed to end the chain when there is nothing to
build, and that is what ends a repeat pass with nothing left in it.
"""

from aetheris import config
from aetheris import git_ops
from aetheris import providers
from aetheris.config import S, _hr


class Stage:
    def __init__(self, key, title, instruction, edits=False):
        self.key = key
        self.title = title
        self.instruction = instruction
        self.edits = edits          # may this stage change files?


STAGES = (
    Stage("plan", "Plan", """\
Work out what this actually takes, before anything changes.

Read the files involved - `read_file`, `search_in_file`, `get_code_skeleton`.
Plan against what the files say, not against what you remember of them.

Then write the plan: which files change, what changes in each, in what order.
Be concrete - name the files, functions and lines you have actually looked at.

Change NOTHING in this stage. The tools that change things are switched off
until stage 3, so trying one only wastes a turn.

If the request needs no changes at all - it is a question, or it is already
done - then answer it properly and put NO_PLAN_NEEDED on the last line by
itself. That ends the chain here instead of spending five more turns on
nothing."""),

    Stage("check", "Check the plan", """\
Now argue against your own plan. Your job in this stage is to find the reason it
fails, not to feel better about it.

- What did you assume without checking? Check it now, with a tool.
- What else does this touch? Who calls it, who imports it, what tests cover it?
- Which case does the plan not handle?
- Is there a simpler change that does the same job?

Read whatever you need to settle each point - do not reason it out in your head
when a tool can tell you.

Then state the plan as it now stands, in full, whether or not it changed. Say
plainly which of your assumptions turned out to be wrong.

The tools that change things are still switched off."""),

    Stage("build", "Implement", """\
Carry out the plan as it now stands.

Match the style of the file you are editing - its naming, its spacing, the way
it already does this sort of thing.

Change only what the plan calls for. If you notice something else worth fixing,
write it down at the end and leave it alone.""", edits=True),

    Stage("review", "Review the changes", """\
Read what you changed as if someone else wrote it and you have to approve it.

- Does it do what the plan said it would? Name any part of the plan that did
  not get carried out, and any change that was not in the plan.
- Anything you changed the behaviour of, removed, or renamed: who used it?
  `search_in_file` for the name before you decide it was safe. A function that
  now raises where it used to return will break its callers and its tests.
- Is anything half-finished - a branch that returns nothing, a name that no
  longer describes what the thing does, an import you added and never used?
- Any wrong variable, off-by-one, inverted condition, or missing case?

Do not fix anything here. Your only job in this stage is to find what is wrong,
and a stage that is allowed to fix stops looking as soon as it has something to
fix. The tools that change things are switched off again; the next stage turns
them back on and does the fixing.

End with a numbered list of every problem you found, each naming the file and
what is wrong with it. If you found none, say that plainly - do not invent a
problem to look thorough."""),

    Stage("revise", "Fix what the review found", """\
Work through the list from the review, in order, and fix each one.

Fix what the review actually found. This is not a second review and not an
invitation to improve something it did not mention - a change nobody reviewed
is a change nobody checked.

If the review found nothing, change nothing and say so in one line. An empty
list is a result; making an edit to look busy undoes work that was already
right.

For each problem: say which one you are fixing, make the change, and say what
you changed. If one of them turns out not to be a problem after all, say why
and leave it alone.""", edits=True),

    Stage("verify", "Final check", """\
Prove it works. Do not describe it working.

Run it with `run_cmd` - the test suite, the script, the command, whatever
actually exercises what you changed - and report what came back.

Then check it against the plan. Go back to the plan as it stood after stage 2,
and go through it item by item: was each thing it said would change actually
changed, and does it now do what the plan said it would? Name anything that was
planned and is still not done, and anything that was done and was never
planned. A change that works and is not what was agreed is still not finished.

Go through the original request point by point and check each point directly.
The tests that already existed were written before your change and know nothing
about it - passing them is not proof you did what was asked. If the request said
a broken line must be skipped, feed it a broken line and look. If it said the
file must be read one line at a time, check that it is. A green test suite that
never touches the thing you changed proves nothing about it.

Find out how a test is meant to be run before you guess. Read the test file: a file
whose tests are plain functions under `if __name__ == "__main__"` is run with
`python3 <file>`, not through unittest. "No tests ran", "file not found" and
"command not found" are not results - they mean you ran the wrong thing. Fix the
command and run it again.

Then report honestly:
- what you ran, and what it said;
- what passed;
- what failed, with the real output;
- what you could not check, and why.

Then stop. If you could not verify it, the answer is "I could not verify it" -
do not follow that with a summary saying it works. You have not earned that
sentence, and the person reading it will believe you.

If what you have just reported leaves the request unfinished - something the
plan called for is still not done, something you ran still fails, the change
does not do what was asked - then put MORE_WORK_NEEDED on the last line by
itself. The chain starts over at the plan and works on what is left, which is
better than handing back work you have just said is not finished. Write it only
because what was asked is not done, never because something could be nicer.""",
          edits=True),
)

STOP_MARKER = "NO_PLAN_NEEDED"
MORE_MARKER = "MORE_WORK_NEEDED"

# What the next pass is told, so it plans what is left rather than the whole
# request again. Its last line hands the chain back its own way out: a pass with
# nothing left in it ends after stage 1 like any other request that needs no
# work.
CARRY_OVER = """\
[Deepthink - going round again]
The check you just wrote says this is not finished, so the chain starts over
from the plan rather than handing it back.

What is left is what that report named as undone or still failing, and nothing
else. Do not re-plan the parts that already work, and do not widen the request.
If it turns out there is nothing left to do after all, say so and end the plan
with NO_PLAN_NEEDED."""


def enabled() -> bool:
    return bool(getattr(config, "DEEPTHINK", False))


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------

async def run(messages: list, pass_number: int = 1) -> str:
    """Drive the six stages over one request. Returns the last answer.

    `pass_number` counts the times round: a final check that says the work is
    not done sends the chain back to stage 1, and the count is what stops that
    happening forever.
    """
    from aetheris.context import manage_context
    from aetheris.llm_client import chat_turn

    answer = ""
    build_from = ""

    for number, stage in enumerate(STAGES, 1):
        instruction = stage.instruction
        if stage.key == "review":
            instruction = _with_changes(instruction, build_from)
            if instruction is None:
                _note("nothing was changed, so there is nothing to review")
                break
        if stage.key == "build":
            # Only worth marking when commits are being made: with auto-commit
            # off git has no record of the build, and "no diff" would then mean
            # "not watching", not "nothing changed".
            build_from = git_ops.head() if git_ops.enabled() else ""

        _banner(number, stage, pass_number)
        messages.append({"role": "user",
                         "content": f"[Deepthink {number}/{len(STAGES)} - "
                                    f"{stage.title}]\n{instruction}"})
        # Asking a model to hold off does not hold it off. A stage that is meant
        # to think rather than act is made unable to act.
        config.DEEPTHINK_READONLY = not stage.edits
        appended_from = len(messages)
        try:
            await manage_context(messages)
            answer = await chat_turn(messages)
        except KeyboardInterrupt:
            _note(f"stopped by you during '{stage.title}'")
            raise
        finally:
            config.DEEPTHINK_READONLY = False

        if stage.key == "verify":
            _report_checks(messages[appended_from:])
            again = await _needs_another_pass(answer)
            answer = answer.replace(MORE_MARKER, "").strip()
            if again:
                ceiling = max(1, config.DEEPTHINK_MAX_PASSES)
                if pass_number >= ceiling:
                    _note(f"the final check says it is still not done, but "
                          f"{ceiling} pass{'es' if ceiling > 1 else ''} is the "
                          f"limit - what is left is in the report above")
                else:
                    _note(f"not finished, so it goes round again from the plan "
                          f"- pass {pass_number + 1} of at most {ceiling}")
                    messages.append({"role": "user", "content": CARRY_OVER})
                    return await run(messages, pass_number + 1)

        if stage.key == "plan" and not await _needs_building(answer):
            answer = answer.replace(STOP_MARKER, "").strip()
            _note("nothing to build here, so the chain stops")
            break

    _footer()
    return answer


async def _needs_building(plan: str) -> bool:
    """Is there actually something to build, or was that an answer?

    The plan stage is asked to mark a request that needs no work, and a model
    that remembers to is believed for free. One that forgets - and a small one
    usually does, at the end of a long plan - gets its plan read back by a
    second short call. Reading a plan is a far easier question than reading the
    original request, so this lands where a classifier on the request would not.

    Anything unclear counts as "yes": spending four turns on a question is a
    waste, but skipping the build on a real request is a failure.
    """
    if STOP_MARKER in plan:
        return False
    if not plan.strip():
        return True
    try:
        verdict = await providers.complete([{"role": "user", "content": (
            "Below is a plan an assistant wrote. Does carrying it out mean "
            "creating, editing or deleting a file, or running a command that "
            "changes something? Answer with one word, YES or NO, and nothing "
            "else.\n\n--- plan ---\n" + plan.strip()[-2500:])}], max_tokens=6)
    except Exception:
        return True
    word = verdict.strip().upper().split()
    return not (word and word[0].strip(".,!:*`") == "NO")


async def _needs_another_pass(report: str) -> bool:
    """Did the final check just say the work is not done?

    The same shape as `_needs_building`, and the default is the other way
    round. There, anything unclear counted as work to do: four wasted turns are
    cheaper than a request that never got built. Here, anything unclear stops.
    Another pass is six more turns, and a chain that sets itself off again on a
    maybe is a chain that does not end - so it takes a plain yes, either the
    marker or a second call that says so in one word.
    """
    if MORE_MARKER in report:
        return True
    if not report.strip():
        return False
    try:
        verdict = await providers.complete([{"role": "user", "content": (
            "Below is a report an assistant wrote after checking its own work. "
            "Does it say that something it set out to do is still not done, or "
            "still failing? Answer with one word, YES or NO, and nothing "
            "else.\n\n--- report ---\n" + report.strip()[-2500:])}], max_tokens=6)
    except Exception:
        return False
    word = verdict.strip().upper().split()
    return bool(word) and word[0].strip(".,!:*`") == "YES"


def _with_changes(instruction: str, build_from: str):
    """The review instruction, with the real diff in it when git can supply one.

    Returns None when git is certain nothing changed - reviewing nothing invites
    a report about work that was never done.
    """
    if not build_from:
        # No repository, or auto-commit is off: the files are the only record.
        return (instruction + "\n\nRe-read every file you changed with "
                "`read_file` before you judge it. Reviewing from memory finds "
                "nothing, because what you remember is what you meant to write.")
    patch = git_ops.diff_since(build_from)
    if not patch.strip():
        return None
    return (instruction + "\n\nThis is what actually changed:\n\n```diff\n"
            + patch + "\n```")


# ---------------------------------------------------------------------------
# what it looks like
# ---------------------------------------------------------------------------

def _report_checks(turns: list) -> None:
    """Say what the final stage actually ran, from the tool results themselves.

    A model that could not verify its work will sometimes say so and then finish
    with a sentence claiming it works anyway. The person reading believes the
    last sentence. So the commands are counted here, from what the harness saw
    rather than from what the model wrote about it.
    """
    ran = failed = 0
    for turn in turns:
        content = turn.get("content", "")
        if turn.get("role") != "user" or not content.startswith("[Tool Result for 'run_cmd']"):
            continue
        ran += 1
        body = content.split("\n", 1)[-1]
        if body.startswith("[Error]"):
            failed += 1

    if ran == 0:
        _note("the final check ran no command - nothing above was actually proven")
    elif failed == ran:
        _note(f"every command the final check ran failed ({failed}/{ran}) - "
              "read the summary above with that in mind")
    elif failed:
        _note(f"{failed} of the {ran} commands the final check ran failed")


def _banner(number: int, stage: Stage, pass_number: int = 1) -> None:
    again = f"  {S.MUTED}pass {pass_number}{S.R}" if pass_number > 1 else ""
    print(f"\n  {S.PURPLE}◆ deepthink {number}/{len(STAGES)}{S.R}  "
          f"{S.BOLD}{stage.title}{S.R}{again}")
    print(f"  {_hr(width=60)}")


def _note(text: str) -> None:
    print(f"  {S.MUTED}◆ deepthink: {text}{S.R}")


def _footer() -> None:
    print(f"  {_hr(width=60)}")
    print(f"  {S.PURPLE}◆ deepthink done{S.R}\n")


if __name__ == "__main__":
    print("This file can not run directly.")
