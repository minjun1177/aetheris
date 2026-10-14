"""A refused or dropped request is tried again - but only before it answered.

One 503 used to end the turn, and under deepthink it ended a chain of a dozen
requests with the eleven before it thrown away. What is checked here is where
the line falls: a 429, a 5xx or a connection that never answered is retried
with a wait that doubles, or as long as the server asks; a 401 or a 400 is not,
because asking again cannot change the answer; and a reply that has started
arriving is never started over, because it has already been printed.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AETHERIS_HOME", tempfile.mkdtemp(prefix="retry-home-"))

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
from aetheris import providers       # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


waits = []


async def no_wait(seconds):
    waits.append(seconds)

providers._sleep = no_wait


def flaky(failures_first: list, chunks=({"text": "hi"}, {"done": True})):
    """A stream that raises each of `failures_first` in turn, then answers."""
    opened = []

    def open_stream():
        opened.append(1)

        async def stream():
            if len(opened) <= len(failures_first):
                raise failures_first[len(opened) - 1]
            for chunk in chunks:
                yield chunk
        return stream()
    return open_stream, opened


def run(open_stream, on_retry=None):
    async def collect():
        return [c async for c in providers.stream_with_retry(open_stream, on_retry)]
    return asyncio.run(collect())


def refused(status, retry_after=None):
    return providers.ProviderError(f"HTTP {status}", status, retry_after)


config.PROVIDER_RETRIES = 3

print("--- what is worth asking again ---")
for label, error in [
    ("a 429", refused(429)),
    ("a 503", refused(503)),
    ("a 500 from Ollama's own client", type("ResponseError", (Exception,),
                                            {"status_code": 500})("boom")),
    ("a refused connection", ConnectionRefusedError("refused")),
    ("a timeout", TimeoutError("slow")),
]:
    waits.clear()
    opener, opened = flaky([error])
    chunks = run(opener)
    check(f"{label} is tried again and answers", len(opened) == 2
          and chunks[0] == {"text": "hi"}, f"opened {len(opened)}")

try:
    import requests
    waits.clear()
    opener, opened = flaky([requests.exceptions.ConnectionError("reset")])
    run(opener)
    check("so is a connection requests says dropped", len(opened) == 2)
except ImportError:
    print("  [skip] requests is not installed")

for label, error in [("a 401", refused(401)), ("a 400", refused(400)),
                     ("a 404", refused(404)), ("a bug of ours", ValueError("bad"))]:
    opener, opened = flaky([error])
    try:
        run(opener)
        raised = None
    except Exception as caught:
        raised = caught
    check(f"{label} is not - it is raised as it was", raised is error
          and len(opened) == 1, f"opened {len(opened)}")

print("\n--- how long it waits, and how often ---")
waits.clear()
opener, opened = flaky([refused(503), refused(503), refused(503)])
told = []
run(opener, lambda error, seconds, attempt: told.append((seconds, attempt)))
check("the wait doubles: 1, 2, 4", waits == [1.0, 2.0, 4.0], str(waits))
check("and the person is told each time, with the count",
      [a for _, a in told] == [1, 2, 3], str(told))

waits.clear()
opener, opened = flaky([refused(503)] * 4)
try:
    run(opener)
    gave_up = False
except providers.ProviderError:
    gave_up = True
check("three retries, then the error stands", gave_up and len(opened) == 4,
      f"opened {len(opened)}")

waits.clear()
opener, opened = flaky([refused(429, retry_after=7.0)])
run(opener)
check("Retry-After is waited out instead of the doubling", waits == [7.0], str(waits))

waits.clear()
opener, opened = flaky([refused(429, retry_after=3600.0)])
try:
    run(opener)
    gave_up = False
except providers.ProviderError:
    gave_up = True
check("but an hour is not waited on: the error is shown instead",
      gave_up and not waits and len(opened) == 1, str(waits))

config.PROVIDER_RETRIES = 0
opener, opened = flaky([refused(503)])
try:
    run(opener)
except providers.ProviderError:
    pass
check("PROVIDER_RETRIES 0 turns it off", len(opened) == 1)
config.PROVIDER_RETRIES = 3

print("\n--- never once the reply has started ---")
opened = []


def breaks_midway():
    opened.append(1)

    async def stream():
        yield {"text": "half an ans"}
        raise refused(503)
    return stream()


got = []


async def collect_some():
    async for chunk in providers.stream_with_retry(breaks_midway):
        got.append(chunk)

try:
    asyncio.run(collect_some())
    raised = False
except providers.ProviderError:
    raised = True
check("a stream that fails after its first chunk is not opened again",
      raised and len(opened) == 1 and got == [{"text": "half an ans"}],
      f"opened {len(opened)}, got {got}")

print("\n--- reading Retry-After ---")
check("seconds", providers.retry_after({"Retry-After": "12"}) == 12.0)
check("in any case", providers.retry_after({"retry-after": "2.5"}) == 2.5)
check("absent", providers.retry_after({}) is None)
check("nonsense", providers.retry_after({"Retry-After": "soon"}) is None)
from email.utils import format_datetime          # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
seconds = providers.retry_after({"Retry-After": later})
check("an HTTP date", seconds is not None and 25 <= seconds <= 31, str(seconds))


class FakeResponse:
    def __init__(self, status, headers):
        self.status_code = status
        self.headers = headers
        self.text = '{"error": {"message": "slow down"}}'

    def json(self):
        return {"error": {"message": "slow down"}}


error = providers.AnthropicProvider({})._refused(FakeResponse(429, {"Retry-After": "5"}))
check("a refused hosted request carries its status and Retry-After",
      error.status == 429 and error.retry_after == 5.0, f"{error.status} {error.retry_after}")
check("and is still the RuntimeError callers catch", isinstance(error, RuntimeError))
check("with the message it always had", "429" in str(error) and "slow down" in str(error),
      str(error)[:100])


print("\n--- a turn rides over one 503 ---")
import io                            # noqa: E402
import re                            # noqa: E402
from aetheris import llm_client      # noqa: E402


class Overloaded(providers.Provider):
    name = "overloaded"
    label = "Overloaded"
    needs_key = False

    def __init__(self):
        super().__init__({"model": "o-1"})
        self.calls = 0

    def ready(self):
        return ""

    async def stream(self, messages, max_tokens=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            raise refused(503)
        yield {"text": "fine now\n"}
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


saved, provider = providers._active, Overloaded()
providers._active = provider
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    answer = asyncio.run(llm_client.stream_reply([{"role": "user", "content": "hi"}]))
finally:
    sys.stdout = real
    providers._active = saved
printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", quiet.getvalue())
check("stream_reply answers after the retry", "fine now" in answer and provider.calls == 2,
      repr(answer))
check("and said why it waited", "trying again in 1s (1/3)" in printed, printed[-200:])


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("retry checks passed")
