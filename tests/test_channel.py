"""Two harnesses in one project, and what stops them destroying each other's work.

The failure this exists to prevent is silent: agent A reads a file, agent B
reads the same file, both write their own idea of it, and B's write throws A's
away with nothing on either screen to say so. So the checks that matter most
here are the ones about a *refusal* - that a file somebody else is holding
cannot be written from another session, that the refusal names who to ask, and
that it stops being a refusal the moment the holder goes away.

The rest is about not creating a new way to lose work: nothing half-claimed,
nothing claimed forever by a terminal that crashed, no message reported as
delivered to an agent that was never there, and no lost write when several
processes touch the board at the same moment.

Two agents are simulated in one process by swapping `channel._id`, which is the
only thing that distinguishes them; the concurrency check uses real subprocesses
because a lock is not worth testing inside one interpreter.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from aetheris import paths

# Before `config` is imported: it resolves the state paths at import time, and
# none of this may touch the real ~/.aetheris.
HOME = tempfile.mkdtemp(prefix="channel-home-")
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = False             # git is tested in test_git_ops.py

from aetheris import channel         # noqa: E402
from aetheris import tools           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


WORK = tempfile.mkdtemp(prefix="channel-work-")


def fresh_board():
    """An empty workspace with nobody on it."""
    channel.reset()
    channel._workspace = os.path.realpath(WORK)
    for path in (channel.board_path(), channel.board_path() + ".lock"):
        try:
            os.unlink(path)
        except OSError:
            pass


def as_agent(agent_id):
    """Act as an agent that is already registered. The id is all that differs."""
    channel._id = agent_id
    channel._last_beat, channel._last_label = 0.0, ""


def join(label):
    """Register another agent, and leave this process acting as it."""
    channel._id = ""
    return channel.join(label)


def board():
    return channel.read_board()


def drain(*agent_ids):
    """Read past the arrival notices, which are messages like any other.

    Joining is announced to everybody already here, so an agent that has just
    watched somebody else arrive has something waiting for it. That is checked
    on its own further down; the checks about what was *said* start from quiet.
    """
    for agent_id in agent_ids:
        as_agent(agent_id)
        channel.take_for_model()
        channel.take_for_screen()


def path_in_work(name):
    full = os.path.join(WORK, name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


print("--- the board is state about a workspace, not a file in it ---")
fresh_board()
a1 = join("model-one")
check("joining gives this session an id", a1 == "a1", a1)
check("the board is under the home directory",
      os.path.commonpath([channel.board_path(), paths.home()]) == paths.home(),
      channel.board_path())
check("and nothing was written into the project",
      os.listdir(WORK) == [], str(os.listdir(WORK)))
check("its name says which project it is for",
      os.path.basename(channel.board_path()).startswith(
          os.path.basename(os.path.realpath(WORK))[:32]))

print("\n--- one workspace, whichever directory of it you started in ---")
repo = tempfile.mkdtemp(prefix="channel-repo-")
subprocess.run(("git", "init", "-q", "-b", "main", repo), capture_output=True)
deep = os.path.join(repo, "src", "inner")
os.makedirs(deep, exist_ok=True)
origin = os.getcwd()
try:
    from aetheris import git_ops
    os.chdir(deep)
    channel.reset()
    git_ops._repo_root_cache.clear()
    check("a terminal in a subdirectory shares the top of the repository",
          channel.workspace() == os.path.realpath(repo),
          f"{channel.workspace()} != {os.path.realpath(repo)}")
    deep_board = channel.board_path()
    os.chdir(repo)
    channel.reset()
    git_ops._repo_root_cache.clear()
    check("so both of them read the same board", channel.board_path() == deep_board)
finally:
    os.chdir(origin)
    shutil.rmtree(repo, ignore_errors=True)

print("\n--- two agents can see each other ---")
fresh_board()
a1 = join("model-one")
a2 = join("model-two")
check("the second gets its own id", (a1, a2) == ("a1", "a2"), f"{a1}, {a2}")
as_agent(a1)
check("each sees the other as a peer", [p["id"] for p in channel.peers()] == ["a2"])
check("and itself in the full list",
      sorted(r["id"] for r in channel.agents()) == ["a1", "a2"])
as_agent(a2)
check("from the other side too", [p["id"] for p in channel.peers()] == ["a1"])

print("\n--- a message reaches the other agent, and only them ---")
drain(a1, a2)
as_agent(a1)
ok, said = channel.send("who has app.py?")
check("a broadcast is accepted when somebody is here", ok, said)
check("the sender is not handed back their own message",
      channel.take_for_model() == [])
as_agent(a2)
mine = channel.take_for_model()
check("the other agent receives it", [m["text"] for m in mine] == ["who has app.py?"],
      str(mine))
check("it is described as coming from a1",
      channel.describe(mine[0]).startswith("a1 → everyone:"), channel.describe(mine[0]))
check("and it is not delivered to the model twice", channel.take_for_model() == [])
check("but the screen has its own cursor, so it is still printed once",
      [m["text"] for m in channel.take_for_screen()] == ["who has app.py?"])
check("and only once", channel.take_for_screen() == [])

as_agent(a1)
ok, _ = channel.send("just you", to="a2")
as_agent(a2)
direct = channel.take_for_model()
check("a direct message arrives", [m["text"] for m in direct] == ["just you"])
check("addressed to you rather than the room",
      channel.describe(direct[0]) == "a1 → you: just you")

print("\n--- a message with nobody to read it is refused, not swallowed ---")
as_agent(a1)
ok, said = channel.send("hello?", to="a9")
check("a message to an agent that is not here fails", not ok, said)
check("and says who is actually here", "a2" in said, said)
fresh_board()
only = join("alone")
ok, said = channel.send("anyone?")
check("a broadcast into an empty workspace fails", not ok, said)
check("nothing was posted", board()["messages"] == [])

print("\n--- a claimed file cannot be written from another session ---")
fresh_board()
a1, a2 = join("one"), join("two")
shared = path_in_work("shared.py")
with open(shared, "w") as f:
    f.write("original\n")

as_agent(a1)
taken, refused = channel.claim(shared, "rewriting the parser")
check("the first agent takes it", taken == ["shared.py"], str(taken))
check("with nothing refused", refused == [])

as_agent(a2)
conflict = channel.holder(shared)
check("the second agent sees the claim", conflict.get("agent") == "a1", str(conflict))
result = tools.dispatch_tool("write_file", {"filepath": shared, "content": "clobbered\n"})
check("and its write is refused", result.startswith("[System]"), result[:80])
check("the refusal names the holder", "a1" in result, result[:120])
check("and says what they are doing", "rewriting the parser" in result)
check("the file on disk is untouched", open(shared).read() == "original\n")
check("edit_file is refused the same way",
      tools.dispatch_tool("edit_file", {"filepath": shared, "old_content": "original",
                                        "new_content": "x"}).startswith("[System]"))
check("delete_file too",
      tools.dispatch_tool("delete_file", {"filepath": shared}).startswith("[System]"))
check("and copy_file, which is refused on where it would land",
      tools.dispatch_tool("copy_file", {"src": path_in_work("other.py"),
                                        "dst": shared}).startswith("[System]"))

as_agent(a1)
check("the holder is not refused its own claim", channel.holder(shared) == {})
check("and can write it", tools.dispatch_tool(
    "write_file", {"filepath": shared, "content": "mine\n"}).startswith("[Success"))

print("\n--- nothing is half-claimed ---")
one, two = path_in_work("one.py"), path_in_work("two.py")
as_agent(a2)
taken, refused = channel.claim(f"{one}, {shared}, {two}", "a batch")
check("a batch containing somebody else's file takes nothing", taken == [], str(taken))
check("and reports the conflict", [c["path"] for c in refused] == ["shared.py"],
      str(refused))
check("so the others are still free", channel.holder(one) == {} and
      board()["claims"].get("one.py") is None)

print("\n--- a claim is given back ---")
as_agent(a1)
check("release drops it", channel.release(shared) == ["shared.py"])
as_agent(a2)
check("and the other agent may now write", tools.dispatch_tool(
    "write_file", {"filepath": shared, "content": "theirs\n"}).startswith("[Success"))
as_agent(a1)
check("but only the holder can release one",
      channel.release("two.py") == [])

print("\n--- a claim dies with the agent that took it ---")
fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
channel.claim(shared, "mid-edit")
as_agent(a2)
check("held while a1 is alive", channel.holder(shared).get("agent") == "a1")
# a1's terminal is killed: no `leave`, no heartbeat, and a pid that is not there.
data = board()
data["agents"]["a1"].update({"pid": 999999999, "host": "somewhere-else",
                             "seen": time.time() - 10000})
channel._update(lambda b: b.update(data))
check("a crashed agent stops holding anything", channel.holder(shared) == {},
      str(channel.holder(shared)))
check("and the write goes through", tools.dispatch_tool(
    "write_file", {"filepath": shared, "content": "b\n"}).startswith("[Success"))
check("the dead agent is off the list", [r["id"] for r in channel.agents()] == ["a2"])

print("\n--- and it expires on its own ---")
fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
channel.claim(shared, "briefly", seconds=0.4)
as_agent(a2)
check("held at first", channel.holder(shared).get("agent") == "a1")
time.sleep(0.5)
check("gone once it has expired", channel.holder(shared) == {})

print("\n--- writing a file claims it, without being asked to ---")
fresh_board()
a1, a2 = join("one"), join("two")
untouched = path_in_work("auto.py")
as_agent(a1)
check("a successful write takes a claim", tools.dispatch_tool(
    "write_file", {"filepath": untouched, "content": "one\n"}).startswith("[Success"))
as_agent(a2)
held = channel.holder(untouched)
check("so the other agent is stopped", held.get("agent") == "a1", str(held))
check("even though a1 never called claim_files",
      held.get("reason") == "changed it just now", str(held))

fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
missing = os.path.join(WORK, "no-such-dir", "x.py")
failed = tools.dispatch_tool("edit_file", {"filepath": missing, "old_content": "a",
                                           "new_content": "b"})
check("a write that failed claims nothing", not failed.startswith("[Success"), failed[:60])
as_agent(a2)
check("so nothing is held", channel.holder(missing) == {})

print("\n--- the person can overrule a claim, and the model cannot ---")
fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
channel.claim(shared, "gone to lunch")
as_agent(a2)
check("release does not touch somebody else's claim", channel.release(shared) == [])
check("still held", channel.holder(shared).get("agent") == "a1")
check("force_release - what /agents release calls - takes it back",
      channel.force_release(shared) == ["shared.py"])
check("and the write goes through", tools.dispatch_tool(
    "write_file", {"filepath": shared, "content": "c\n"}).startswith("[Success"))

print("\n--- turned off, none of it happens ---")
fresh_board()
a1, a2 = join("one"), join("two")
as_agent(a1)
channel.claim(shared, "holding")
as_agent(a2)
config.CHANNEL_CLAIMS = False
check("claims can be stopped from being enforced", channel.holder(shared) == {})
config.CHANNEL_CLAIMS = True
config.CHANNEL_ENABLED = False
check("with the channel off there are no agents at all", channel.agents() == [])
check("no claim is enforced", channel.holder(shared) == {})
check("nothing is delivered", channel.turn_note() == "")
ok, said = channel.send("hello")
check("and a message says so rather than pretending", not ok, said)
check("the tool says so too, without an [Error] the model would retry",
      tools.dispatch_tool("list_agents", {}).startswith("[System]"))
check("and so does a message with nowhere to go",
      tools.dispatch_tool("send_agent_message",
                          {"kind": "question", "message": "anyone?"}).startswith("[System]"))
config.CHANNEL_ENABLED = True

print("\n--- what the model is told, and when ---")
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a1)
check("a quiet turn adds nothing to the conversation", channel.turn_note() == "")
as_agent(a2)
channel.send("I am taking the tests directory", to="a1")
as_agent(a1)
note = channel.turn_note()
check("a turn with a message does", note.startswith("[Channel]"), note[:40])
check("and carries what was said", "taking the tests directory" in note)
check("and names the tool to answer with", "send_agent_message" in note)
check("the next turn is quiet again", channel.turn_note() == "")

print("\n--- a question that was never answered is chased, once ---")
# Two 4B models were run against each other to see whether the board holds up.
# It did, until the last step: asked "are you finished with shared.py?", the
# holder released the file and then wrote its reply into its own *answer* -
# "you have my agreement for a1 to proceed" - addressed to the other agent and
# delivered to nobody, while the asker sat waiting for a reply. The note asks
# the model to use send_agent_message; asking is not enough here either.
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
channel.clear_asked()

as_agent(a2)
channel.send("everyone: I am starting on the parser")
as_agent(a1)
channel.turn_note()
check("a broadcast is news, and news needs no answer", channel.awaiting_reply() == [],
      str(channel.awaiting_reply()))

as_agent(a2)
channel.send("are you finished with shared.py?", to=a1)
as_agent(a1)
channel.turn_note()
check("a message addressed to you is a question outstanding",
      channel.awaiting_reply() == [a2], str(channel.awaiting_reply()))

answered = tools.dispatch_tool("send_agent_message",
                               {"kind": "answer", "message": "yes, go ahead", "to": a2})
check("answering it clears that", answered.startswith("[Success")
      and channel.awaiting_reply() == [], str(channel.awaiting_reply()))

# And the turn loop is what chases it: a model that ends its turn without
# answering gets told once, and only once.
as_agent(a2)
channel.send("and the other one?", to=a1)
as_agent(a1)
channel.turn_note()

import asyncio                                    # noqa: E402
from aetheris import llm_client             # noqa: E402

requests = []


async def prose_only(messages, tools=None, calls_out=None):
    """A model that answers the other agent in its own reply, as one really did."""
    requests.append(len(messages))
    return "Sure, that is fine by them - go ahead."


conversation = [{"role": "system", "content": "x"},
                {"role": "user", "content": "carry on"}]
saved_stream = llm_client.stream_reply
llm_client.stream_reply = prose_only
try:
    asyncio.run(llm_client.chat_turn(conversation))
finally:
    llm_client.stream_reply = saved_stream

nudges = [m["content"] for m in conversation
          if m["role"] == "user" and m["content"].startswith("[System]")
          and "waiting on an answer" in m["content"]]
check("a turn that answered in prose is asked to send it properly",
      len(nudges) == 1, f"{len(nudges)} nudges, {len(requests)} requests")
check("and asked exactly once, not until it complies",
      len(requests) == 2, f"{len(requests)} requests")
if nudges:
    check("the nudge names who is waiting", a2 in nudges[0], nudges[0][:90])
    check("and says that prose does not reach them",
          "goes to the user" in nudges[0] or "reaches them" in nudges[0], nudges[0][:120])
    check("and offers the way out of having nothing to say",
          "nothing to say" in nudges[0], nudges[0][-90:])
check("the question is not left outstanding after the nudge",
      channel.awaiting_reply() == [], str(channel.awaiting_reply()))

print("\n--- a message that arrives mid-turn does not wait for the next one ---")
# The board's whole job is to be read in time. A turn is where the time goes -
# twenty tool calls is minutes - and until this, "I am holding parser.py" sent
# during one was read after it, which for the agent that sent it means after
# the file had already been written over. Every gap between two requests is
# now offered what has arrived since the last one.
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
channel.clear_asked()
as_agent(a1)

config.NATIVE_TOOLS = False          # the text protocol, so a faked reply carries the call
sent_mid_turn = []
seen_by_request = []


async def works_then_answers(messages, tools=None, calls_out=None):
    """A model in the middle of a job, messaged by the other agent as it works."""
    seen_by_request.append([m["content"] for m in messages])
    if len(seen_by_request) == 1:
        # The message lands while the first request is in flight, which is the
        # case that used to be delivered a whole turn late.
        as_agent(a2)
        channel.send("I am holding parser.py, do not write it", to=a1)
        as_agent(a1)
        sent_mid_turn.append(True)
        return '<tool_call>{"name": "list_agents", "arguments": {}}</tool_call>'
    return "Understood - I will leave parser.py alone."


conversation = [{"role": "system", "content": "x"},
                {"role": "user", "content": "refactor the parser"}]
saved_stream = llm_client.stream_reply
llm_client.stream_reply = works_then_answers
try:
    asyncio.run(llm_client.chat_turn(conversation))
finally:
    llm_client.stream_reply = saved_stream
    config.NATIVE_TOOLS = True

check("the message was sent while the turn was still running", sent_mid_turn == [True])
check("and the model was asked again rather than the turn ending",
      len(seen_by_request) > 1, f"{len(seen_by_request)} requests")
mid = [m["content"] for m in conversation
       if m["role"] == "user" and m["content"].startswith("[Channel]")]
check("it reached the conversation without the turn ending", len(mid) == 1,
      f"{len(mid)} channel notes")
if mid:
    check("carrying what was actually said", "holding parser.py" in mid[0])
    check("and said to be newer than the rest of the turn",
          "while you were working" in mid[0], mid[0][:80])
    check("and told not to abandon the job it is in the middle of",
          "carry on with the job" in mid[0], mid[0][-80:])
if len(seen_by_request) > 1:
    check("the second request is the one that carried it",
          any(c.startswith("[Channel]") for c in seen_by_request[1]))
check("the user's request is still what the first one ended on",
      bool(seen_by_request) and seen_by_request[0][-1] == "refactor the parser",
      seen_by_request[0][-1][:60] if seen_by_request else "")
check("and it is not delivered again at the top of the next turn",
      channel.turn_note() == "", channel.turn_note()[:60])
# A question is a question whenever it arrived. The turn ended with a2 still
# unanswered - the model said its piece into its own reply, which reaches
# nobody - so the same nudge that catches one asked at the top of a turn
# catches this one, and having been asked, it is no longer outstanding.
chased = [c for c in seen_by_request[-1]
          if c.startswith("[System]") and "waiting on an answer" in c]
check("a direct question asked mid-turn is chased at the end of it too",
      len(chased) == 1 and a2 in chased[0], f"{len(chased)} nudges")
check("and is not left outstanding afterwards",
      channel.awaiting_reply() == [], str(channel.awaiting_reply()))
channel.clear_asked()

print("\n--- a beat from inside a long turn does not rename the agent ---")
# The tool loop beats with no label, because it does not know one. An empty
# label must not read as a rename, or the loop and the prompt clear each
# other's and the board is written on every beat of a long turn.
fresh_board()
solo = join("gemma4:e4b - fixing the parser")
as_agent(solo)
channel.heartbeat()
check("a labelled agent keeps its label through an unlabelled beat",
      board()["agents"][solo].get("label") == "gemma4:e4b - fixing the parser",
      str(board()["agents"][solo].get("label")))
before = board()["agents"][solo]["seen"]
channel.heartbeat()
check("and a beat that is not due writes nothing",
      board()["agents"][solo]["seen"] == before)

print("\n--- a question to an idle terminal presses Enter by itself ---")
# The other half of the same problem. Delivering a message into the context of
# a session that is sitting at its prompt delivers it to nothing: the model
# reads it on its *next* turn, and the next turn happens when a person comes
# back and types. So "are you finished with shared.py?" waited for somebody's
# lunch to end. Now the channel presses Enter - for a direct question, at an
# empty prompt, a bounded number of times.
from aetheris import app                    # noqa: E402
import types                                      # noqa: E402


class FakePromptApp:
    """Only the three things `_maybe_auto_turn` asks a prompt_toolkit app for."""

    def __init__(self, typed=""):
        self.is_running = True
        self.current_buffer = types.SimpleNamespace(text=typed)
        self.result = None

    def exit(self, result=None):
        self.result, self.is_running = result, False


class FakeSession:
    """A prompt that sits open until something exits it, as the real one does."""

    def __init__(self, typed=""):
        self.app = FakePromptApp(typed)

    async def prompt_async(self, message=None):
        while self.app.is_running:
            await asyncio.sleep(0.01)
        return self.app.result


class TypedSession(FakeSession):
    """A person who was there all along, and typed before the watcher polled."""

    async def prompt_async(self, message=None):
        return "  refactor the parser  "


def rearm():
    app._auto_turns, app._auto_spent_said = 0, False
    app._auto_asked_by.clear()


def direct(sender, to, text="are you finished with shared.py?"):
    return {"seq": 1, "from": sender, "to": to, "text": text}


def broadcast(sender, text="I am starting on the parser"):
    return {"seq": 1, "from": sender, "to": "", "text": text}


fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a1)
config.CHANNEL_POLL_SECONDS = 0.5

# The whole path, through the real `_read_line`: nobody types, and a line comes
# back anyway.
rearm()
as_agent(a2)
channel.send("are you finished with shared.py?", to=a1)
as_agent(a1)
idle = FakeSession()
line = asyncio.run(asyncio.wait_for(app._read_line(idle), timeout=10))
check("an idle prompt returns a line nobody typed", bool(line), repr(line)[:60])
check("and it names who is waiting", a2 in line, line[:70])
check("and says to answer on the channel, not into the void",
      "send_agent_message" in line, line[:90])
check("and tells it not to start a job nobody is here to approve",
      "not a new job" in line, line[-70:])
check("the turn is counted against the budget", app._auto_turns == 1,
      str(app._auto_turns))
# The message itself is not duplicated into the typed line - it goes in ahead of
# it through the model's own cursor, which is the copy that stays in step.
note = channel.turn_note()
check("the message itself still arrives through turn_note",
      "finished with shared.py" in note, note[:60])
check("and the typed line does not repeat it",
      "finished with shared.py" not in line, line[:80])

print("\n  · and the three things that stop it being a nuisance")
rearm()
check("a broadcast does not wake anybody - six agents would all answer it",
      app._maybe_auto_turn(FakeSession(), [broadcast(a2)]) is False)
rearm()
half_typed = FakeSession(typed="I was in the middle of")
check("a half-typed line is not thrown away to make room",
      app._maybe_auto_turn(half_typed, [direct(a2, a1)]) is False)
check("and it is still there", half_typed.app.current_buffer.text.startswith("I was"))
rearm()
config.CHANNEL_AUTO_TURN = False
check("the setting turns it off outright",
      app._maybe_auto_turn(FakeSession(), [direct(a2, a1)]) is False)
config.CHANNEL_AUTO_TURN = True
rearm()
config.CHANNEL_ENABLED = False
check("and so does taking this session off the board",
      app._maybe_auto_turn(FakeSession(), [direct(a2, a1)]) is False)
config.CHANNEL_ENABLED = True

print("\n  · and the budget, which is what stops two idle agents talking all night")
rearm()
config.CHANNEL_AUTO_TURN_MAX = 3
fired = [app._maybe_auto_turn(FakeSession(), [direct(a2, a1)]) for _ in range(5)]
check("it answers by itself three times and then stops",
      fired == [True, True, True, False, False], str(fired))
check("the fourth question is still delivered to the screen",
      app._auto_turns == 3, str(app._auto_turns))
# A person typing anything is the signal that somebody is here again.
typed = asyncio.run(asyncio.wait_for(app._read_line(TypedSession()), timeout=10))
check("a line somebody typed comes back as they typed it",
      typed == "refactor the parser", repr(typed))
check("and it refills the budget", app._auto_turns == 0, str(app._auto_turns))
check("so the next question is answered again",
      app._maybe_auto_turn(FakeSession(), [direct(a2, a1)]) is True)

print("\n  · a question that landed between two turns is not lost")
# The arrivals are drained just before the prompt opens, when there is no
# running application to exit out of. Queued there, fired by the watcher.
rearm()
never_opened = FakeSession()
never_opened.app.is_running = False
check("it is held rather than dropped",
      app._maybe_auto_turn(never_opened, [direct(a2, a1)]) is False
      and app._auto_asked_by == [a2], str(app._auto_asked_by))
check("and fires on the next look, with no new message needed",
      app._maybe_auto_turn(FakeSession(), []) is True)

rearm()
config.CHANNEL_AUTO_TURN_MAX = 3
config.CHANNEL_POLL_SECONDS = 2

print("\n--- the model is told which agent it is ---")
# Everything else about the board is a tool call away; an id is not. `a1, are
# you finished with parser.py?` is addressed to nobody the model recognises
# unless it has been told it is a1, and there is no question it can ask that
# comes back "you".
fresh_board()
channel.reset()
channel._workspace = os.path.realpath(WORK)
check("a session that has not joined says nothing about an id",
      channel.prompt_section() == "", channel.prompt_section()[:60])
solo = channel.join("gemma4:e4b")
section = channel.prompt_section()
check("one that has, names it", f"**{solo}**" in section, section[:80])
check("and says it is the name the others use", "address you by" in section)
check("and where a reply has to go to reach them",
      "send_agent_message" in section and "does not reach them" in section)
config.CHANNEL_ENABLED = False
check("with the channel off it is silent again", channel.prompt_section() == "")
config.CHANNEL_ENABLED = True

# And it is actually in the system message, not merely available to be.
composed = app._compose_system_prompt()
check("it reaches the composed system prompt", f"**{solo}**" in composed,
      f"...{composed[-120:]}" if composed else "empty")
summary = "\n\n<SUMMARY>older talk</SUMMARY>"
kept = app._compose_system_prompt(summary)
check("and lands ahead of the summary, which has to stay last",
      kept.endswith(summary) and f"**{solo}**" in kept)

print("\n--- nothing on the board can paint the screen it is printed on ---")
# A message is printed straight onto another terminal, above a prompt somebody
# is typing at. An escape sequence in one would move their cursor from another
# process entirely.
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a2)
channel.send("red\x1b[31m alert\x07\r and a\ttab\nand a line", to=a1)
as_agent(a1)
posted = board()["messages"][-1]["text"]
check("the escape is gone", "\x1b" not in posted, repr(posted))
check("and so is every other control byte",
      "\x07" not in posted and "\r" not in posted, repr(posted))
check("but a tab and a newline are still a tab and a newline",
      "\t" in posted and "\n" in posted, repr(posted))
check("and the words survive", "red" in posted and "alert" in posted, repr(posted))

print("\n--- an arrival keeps its colour on the way above the prompt ---")
# prompt_toolkit's `patch_stdout()` sends what is printed through
# `Output.write()`, which "removes vt100 escape codes" by replacing every ESC
# with a literal `?`. That is what turned an arrival into
# `?[38;2;211;134;155m✉ a2 joined this workspace` on a real terminal. `raw=True`
# is the documented way not to, and the reason the colour survives at all.
recorded = {}


class FakePatch:
    def __init__(self, **kwargs):
        recorded.update(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


saved_patch = getattr(config, "patch_stdout", None)
config.patch_stdout = lambda **kwargs: FakePatch(**kwargs)
try:
    rearm()
    asyncio.run(asyncio.wait_for(app._read_line(TypedSession()), timeout=10))
finally:
    if saved_patch is not None:
        config.patch_stdout = saved_patch
check("the prompt is patched raw, so an ESC stays an ESC",
      recorded.get("raw") is True, str(recorded))
# The replacement is real and in this very version of prompt_toolkit - if it
# ever stops being, this check is what says the workaround can go.
try:
    import inspect
    from prompt_toolkit.output.vt100 import Vt100_Output
    source = inspect.getsource(Vt100_Output.write)
    mangles = '"?"' in source or "'?'" in source
except Exception:
    mangles = True          # cannot look: assume the workaround is still needed
check("and that is still what the unraw path would do", mangles)

print("\n--- the politeness loop, which is what two small models actually do ---")
# gemma4:e4b and qwen2.5:3b, put on one job together, spent the whole run being
# polite: "could you test it?" - "yes, could you test it?" - and round again.
# Neither ever ran anything; both wrote down that the other was handling it.
# Replying is one tool call and doing the job is twenty, so the loop is the
# cheap path, and a model told to answer its messages has been told to take it.
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a1)
config.CHANNEL_MAX_IDLE_REPLIES = 3
channel.note_tool("read_file")          # a clean slate: something was done

# First, the move itself. This is verbatim what two gemma4:e4b instances did to
# each other, given "make a node web server that returns the caller's IP": they
# asked each other to test it, both of them, for the whole run. It is not a
# question and there is no kind for it, so it does not go at all.
deflect = tools.dispatch_tool("send_agent_message",
                              {"kind": "question", "message": "could you test it?",
                               "to": a2})
check("asking another agent to do the work does not go",
      deflect.startswith("[System]"), deflect[:60])
check("and it is named for what it is",
      "asking another agent to do your work" in deflect, deflect[:120])
check("and says there is no way to send it, rather than just refusing",
      "there is no kind for it" in deflect, deflect[:150])
check("testing especially, because the harness already does that",
      "runs the project's own check" in deflect, deflect[-260:-80])
check("and points at the tool that answers who-holds-what",
      "list_agents, not a message" in deflect, deflect[-60:])
# Naming a file is not enough, and this is what a real run proved: both agents
# wrote "who should run and test server.js?" - a delegation wearing a filename -
# and it sailed through. A question is legitimate about a file somebody else is
# *holding*, and at no other time, because a free file needs no permission:
# writing it takes the claim for you, and a file another agent holds refuses
# your write by name. So a question about an unheld file asks for something that
# could not have been denied.
named = tools.dispatch_tool("send_agent_message",
                            {"kind": "question",
                             "message": "who should run and test server.js?", "to": a2})
check("naming a file does not make a delegation into a question",
      named.startswith("[System]"), named[:60])
check("and it says why there was nothing to ask",
      "Nothing you named is claimed by anyone" in named, named[60:200])
check("and that a free file needs no permission",
      "writing it takes the claim for you" in named, named[:400])

as_agent(a2)
channel.claim("server.js", "writing the server")
as_agent(a1)
held = tools.dispatch_tool("send_agent_message",
                           {"kind": "question",
                            "message": "are you finished with server.js?", "to": a2})
check("a question about a file they are actually holding goes",
      held.startswith("[Success"), held[:60])
channel.note_tool("read_file")

# And the streak, for messages that are legitimate one at a time and a stall in
# a row. Each names a file, so each is a real question.
replies = [tools.dispatch_tool("send_agent_message",
                               {"kind": "question",
                                "message": f"any news on server.js? ({n})", "to": a2})
           for n in range(4)]
check("three messages with nothing done between them go out",
      all(r.startswith("[Success") for r in replies[:3]),
      str([r[:12] for r in replies]))
check("and the fourth is refused", replies[3].startswith("[System]"), replies[3][:60])
check("the refusal names the loop rather than scolding vaguely",
      "neither does it" in replies[3], replies[3][:140])
check("and says what to do instead of talking",
      "do the job yourself" in replies[3], replies[3][60:170])
check("and forbids the hallucination that goes with it",
      "do not say it is done when it is not" in replies[3], replies[3][-120:])
check("it is [System], so a model that keeps knocking ends its turn",
      not replies[3].startswith("[Error]"))

# Doing something - anything - opens it again. Even a read: a model reading the
# file to answer the question is engaging with the job, not handing it back.
tools.dispatch_tool("list_agents", {})
check("looking at the board is not work", channel.talk_streak(a2) >= 3,
      str(channel.talk_streak(a2)))
check("and does not buy another message",
      tools.dispatch_tool("send_agent_message",
                          {"kind": "question", "message": "well? is server.js free?", "to": a2}).startswith("[System]"))
channel.note_tool("edit_file")
check("actually doing something clears the streak", channel.talk_streak(a2) == 0)
check("and the next message goes out",
      tools.dispatch_tool("send_agent_message",
                          {"kind": "done", "message": "done - it passes", "to": a2}
                          ).startswith("[Success"))
config.CHANNEL_MAX_IDLE_REPLIES = 0
channel.note_tool("read_file")
for n in range(6):
    tools.dispatch_tool("send_agent_message",
                        {"kind": "done", "message": f"finished piece {n}", "to": a2})
check("0 removes the limit for anyone who wants it gone",
      tools.dispatch_tool("send_agent_message",
                          {"kind": "done", "message": "and the last one", "to": a2}
                          ).startswith("[Success"))
config.CHANNEL_MAX_IDLE_REPLIES = 3

print("\n--- and the note tells it the same thing before it gets that far ---")
as_agent(a2)
channel.send("could you run the tests?", to=a1)
as_agent(a1)
note = channel.turn_note()
check("the note names the wrong move", "do not ask them to do it" in note, note[-160:])
check("and forbids reporting work that never happened",
      "Never say something is done that you have not done" in note, note[-90:])
as_agent(a2)
channel.send("and the linter?", to=a1)
as_agent(a1)
check("a mid-turn note says it too",
      "do not ask them to do it" in channel.turn_note(mid_turn=True))

print("\n--- the budget is spent on stalling, not on turns ---")
# This is what makes an unlimited exchange a reasonable thing to ask for: two
# agents that are working can go back and forth all day, and two that are only
# talking cannot.
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a1)
rearm()
config.CHANNEL_AUTO_TURN_MAX = 3
app._auto_turns = 3
check("three fruitless turns and it will not fire again",
      app._auto_turn_armed() is False)
channel.note_tool("edit_file")
check("but a turn that did something refills it before the next prompt",
      channel.worked() is True)
as_agent(a2)
channel.send("and now?", to=a1)
as_agent(a1)
woken = asyncio.run(asyncio.wait_for(app._read_line(FakeSession()), timeout=10))
check("so the question is answered rather than left", "[Channel]" in woken,
      woken[:50])
check("and the budget starts again from there", app._auto_turns == 1,
      str(app._auto_turns))

rearm()
channel.reset_work()
config.CHANNEL_AUTO_TURN_MAX = 0
fired = [app._maybe_auto_turn(FakeSession(), [direct(a2, a1)]) for _ in range(8)]
check("0 means no ceiling at all, as it does for MAX_TOOL_CALLS",
      all(fired), str(fired))
config.CHANNEL_AUTO_TURN_MAX = 3
rearm()

print("\n--- clearing the board, which only the person can do ---")
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a2)
channel.send("a direct word", to=a1)
channel.send("something for the room")
as_agent(a1)
channel.turn_note()
check("a direct message can be cleared on its own",
      channel.clear_messages("dm") == 1, str(len(board()["messages"])))
kinds = [(m.get("from"), m.get("to")) for m in board()["messages"]]
check("and the broadcast is still there", (a2, "") in kinds, str(kinds))
check("an outstanding question goes with it", channel.awaiting_reply() == [],
      str(channel.awaiting_reply()))
check("the room's messages clear on their own too",
      channel.clear_messages("everyone") == 1, str(board()["messages"]))
as_agent(a2)
channel.send("one more", to=a1)
as_agent(a1)
before = len(board()["messages"])
check("and clearing everything takes the board's own notices with it",
      channel.clear_messages() == before and board()["messages"] == [],
      str(board()["messages"]))
check("clearing an empty board says nothing went", channel.clear_messages() == 0)
# It is a person's command and deliberately not a tool: the board is shared, so
# clearing it clears it for every agent here.
check("no tool reaches it",
      tools.dispatch_tool("clear_messages", {}) is None)
as_agent(a2)
channel.send("after the wipe", to=a1)
as_agent(a1)
check("and the board still works afterwards",
      "after the wipe" in channel.turn_note())

print("\n--- the boat does not go up the mountain ---")
# The politeness loop was "neither does the work". This is the other one: they
# DO talk, and the topic drifts, because a peer's message arrives as a `user`
# message - the history is flat {role, content} for every provider, so there is
# no role meaning "another agent" - and mid-turn it is the newest thing in the
# conversation. Newest, wearing the same badge as the person's own request. So
# the peer quietly becomes the user, and two 4B models negotiate their way into
# a different problem than the one they were given.
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a1)
channel.set_job("fix the CSV parser so quoted commas survive")
as_agent(a2)
channel.send("should we move all of this to pandas?", to=a1, kind="question")
as_agent(a1)
note = channel.turn_note()
check("the note ends on this agent's own job, not the peer's idea",
      note.rstrip().endswith("nothing above changes it."), note[-70:])
check("and the job is the one a person actually gave",
      "quoted commas survive" in note, note[-160:])
check("and it says plainly who the peer is",
      "You do not work for them" in note, note[-220:-120])
as_agent(a2)
channel.send("or rewrite it in rust?", to=a1, kind="question")
as_agent(a1)
check("a mid-turn note is anchored too - it is the one that lands last",
      "still your job" in channel.turn_note(mid_turn=True))

# A turn the channel started by itself must never become the job.
rearm()
app._auto_woke = True
before = channel.job()
check("the anchor survives a turn nobody typed",
      channel.job() == before and "quoted commas" in channel.job(), channel.job())

print("\n--- a message is a move, not a paragraph ---")
check("an essay does not fit on the board", channel.MAX_TEXT <= 300,
      str(channel.MAX_TEXT))
as_agent(a2)
channel.send("x" * 900, to=a1, kind="warn")
as_agent(a1)
check("and one sent anyway is cut to size",
      len(board()["messages"][-1]["text"]) == channel.MAX_TEXT,
      str(len(board()["messages"][-1]["text"])))

print("\n--- and it has to be one of the moves there are ---")
fresh_board()
a1, a2 = join("one"), join("two")
drain(a1, a2)
as_agent(a1)
channel.note_tool("read_file")
loose = tools.dispatch_tool("send_agent_message",
                            {"message": "I think we should redesign the loader",
                             "to": a2})
check("free prose with no kind is refused", loose.startswith("[Error]"), loose[:50])
check("and the refusal lists what a message may be",
      all(k in loose for k in channel.KINDS), loose[:120])
check("and says where a design debate belongs, which is nowhere here",
      "does not belong on this channel" in loose, loose[-150:])
bogus = tools.dispatch_tool("send_agent_message",
                            {"kind": "discuss", "message": "the architecture",
                             "to": a2})
check("an invented kind is refused too", bogus.startswith("[Error]"), bogus[:40])
vague = tools.dispatch_tool("send_agent_message",
                            {"kind": "claim", "message": "taking the parser stuff",
                             "to": a2})
check("a claim that names no file is a mood, not a claim",
      vague.startswith("[Error]") and "name the file" in vague, vague[:80])
real = tools.dispatch_tool("send_agent_message",
                           {"kind": "claim", "message": "taking src/parser.py",
                            "to": a2})
check("one that names a file goes", real.startswith("[Success"), real[:40])
as_agent(a2)
shown = channel.describe(board()["messages"][-1])
check("and the kind is shown to whoever reads it", "[claim]" in shown, shown)
as_agent(a1)

print("\n--- an idle terminal is woken for a blocker, not for chatter ---")
check("a question wakes it", channel.wakes({"kind": "question"}) is True)
check("so does a claim", channel.wakes({"kind": "claim"}) is True)
check("and a warning", channel.wakes({"kind": "warn"}) is True)
check("and a release - whoever asked for that file is blocked on it",
      channel.wakes({"kind": "release"}) is True)
check("an answer wakes nobody who did not ask",
      channel.wakes({"kind": "answer", "from": a2}) is False)
# The defect this caught, found by running two gemma4:e4b against each other:
# a2 asked a1 "are you still working on server.js?", a1 answered, and a2 - idle,
# with `answer` classed as information - slept through the reply and left. An
# agent that asks and then misses the answer has asked nothing.
as_agent(a2)
channel.claim("server.js", "writing it")
as_agent(a1)
tools.dispatch_tool("send_agent_message",
                    {"kind": "question", "message": "still on server.js?", "to": a2})
check("but the reply to your own question does",
      channel.wakes({"kind": "answer", "from": a2}) is True,
      str(channel.awaiting_answer()))
check("and only from the agent you asked",
      channel.wakes({"kind": "answer", "from": "a9"}) is False)
as_agent(a2)
channel.send("yes, still on it", to=a1, kind="answer")
as_agent(a1)
channel.turn_note()
check("once read, that wait is over",
      channel.awaiting_answer() == [] and
      channel.wakes({"kind": "answer", "from": a2}) is False,
      str(channel.awaiting_answer()))
check("a broadcast question puts nobody in particular on the hook",
      (tools.dispatch_tool("send_agent_message",
                           {"kind": "question", "message": "any news on server.js?"})
       and channel.awaiting_answer() == []), str(channel.awaiting_answer()))
check("nor does a done", channel.wakes({"kind": "done"}) is False)
check("a person's own /agents say does, having no kind at all",
      channel.wakes({"kind": ""}) is True)
rearm()
as_agent(a1)
check("so a done does not start a turn on a quiet terminal",
      app._maybe_auto_turn(FakeSession(),
                           [dict(direct(a2, a1), kind="done")]) is False)
check("and a warning does",
      app._maybe_auto_turn(FakeSession(),
                           [dict(direct(a2, a1), kind="warn")]) is True)
rearm()

print("\n--- one plan, in the place a project's shared writing already lives ---")
from aetheris import notes                        # noqa: E402
config.CHANNEL_PLAN_NOTE = "plan"
check("with no such note there is nothing to point at",
      channel.plan_note() == "", channel.plan_note())
notes.handle_write_note("plan", "Ship the CSV fix. a1 owns parsing, a2 owns tests.")
check("once it exists the channel names it", channel.plan_note() == "plan",
      channel.plan_note())
# The reason a note can serve as the shared goal at all: it is filed under the
# same workspace slug the board is, so two terminals that see each other on the
# board are reading the same note - and cannot come to different conclusions
# about which project they are in.
slug = paths.workspace_slug(channel.workspace())
check("and it is filed under the same workspace the board is",
      os.path.basename(notes.project_dir()) == slug
      and os.path.basename(channel.board_path()) == slug + ".json",
      f"{notes.project_dir()} vs {channel.board_path()}")
as_agent(a2)
channel.send("what are we doing about the loader?", to=a1, kind="question")
as_agent(a1)
pointed = channel.turn_note()
check("and every note points at it rather than re-deriving the plan in chat",
      "read_note 'plan'" in pointed, pointed[-200:])
check("with the instruction that matters most",
      "Do not re-invent it in messages" in pointed, pointed[-90:])
config.CHANNEL_PLAN_NOTE = ""
check("and the pointer can be turned off", channel.plan_note() == "")
config.CHANNEL_PLAN_NOTE = "plan"

print("\n--- joining and leaving are announced ---")
fresh_board()
a1 = join("one")
as_agent(a1)
check("nobody is told about the first agent to arrive", channel.turn_note() == "")
a2 = join("two")
check("and an agent is not told about its own arrival", channel.turn_note() == "",
      channel.turn_note()[:60])
as_agent(a1)
note = channel.turn_note()
check("but an arrival reaches the agent already here", "a2 joined" in note, note[:60])
as_agent(a2)
channel.take_for_model()
as_agent(a2)
channel.leave()
as_agent(a1)
check("and so does a departure", "a2 left" in channel.turn_note())
check("with their claims gone too", board()["claims"] == {})

print("\n--- several processes writing at once lose nothing ---")
fresh_board()
here = join("host")
WRITERS = 6
script = (
    "import os, sys\n"
    f"os.environ['{paths.ENV_VAR}'] = {HOME!r}\n"
    f"sys.path.insert(0, {ROOT!r})\n"
    "from aetheris import config\n"
    "config.MCP_ENABLED = False\n"
    "from aetheris import channel\n"
    f"channel._workspace = {os.path.realpath(WORK)!r}\n"
    "me = channel.join('writer ' + sys.argv[1])\n"
    "channel.send('message from ' + sys.argv[1] + ' as ' + me)\n"
    # Held open so that all of them are on the board at once. Without this each
    # one leaves before the next joins, and every one of them is legitimately
    # handed the same free id - which would prove nothing about the race.
    "import time; time.sleep(2.5)\n"
)
runners = [subprocess.Popen((sys.executable, "-c", script, str(n)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
           for n in range(WRITERS)]
for runner in runners:
    runner.wait(timeout=60)
sent = [m for m in board()["messages"] if m["text"].startswith("message from ")]
check(f"all {WRITERS} messages survived the concurrent writes",
      len(sent) == WRITERS, f"{len(sent)} of {WRITERS}")
check("no two of them were handed the same agent id",
      len({m["from"] for m in sent}) == WRITERS,
      str(sorted(m["from"] for m in sent)))
check("and each one left the board on its way out",
      [r["id"] for r in channel.agents()] == [here],
      str([r["id"] for r in channel.agents()]))
check("the board is still valid JSON", isinstance(
    json.load(open(channel.board_path(), encoding="utf-8")), dict))
check("and no lock was left behind",
      not os.path.exists(channel.board_path() + ".lock"))

print("\n--- a damaged board is not a broken harness ---")
with open(channel.board_path(), "w") as f:
    f.write("{ this is not json")
check("it reads as an empty one", channel.read_board()["agents"] == {})
check("a claim check on it refuses nothing", channel.holder(shared) == {})
os.unlink(channel.board_path())
check("a missing one reads as empty too", channel.read_board()["messages"] == [])

channel.reset()
shutil.rmtree(HOME, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("agent channel checks passed")
