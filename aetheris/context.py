import re
import sys
import asyncio
import itertools
from aetheris import config
from aetheris import providers
from aetheris.config import S, smrp


# How many characters go into one token, by script. Measured against the model
# actually running here: Korean prose came out at 2.2 characters per token,
# English prose at 4.7 and Python source at 3.2. One flat number cannot serve
# all three - the old 3.5 under-counted Korean by 1.6x, and under-counting is
# the dangerous direction, because it is how the context silently overflows.
#
# Latin sits below its prose measurement on purpose: a conversation full of
# code and tool output is denser than prose, and guessing low costs a little
# context while guessing high costs a truncated conversation.
_WIDE_CHARS_PER_TOKEN = 2.0     # Hangul, Kana, Han - one token per 2 characters
_LATIN_CHARS_PER_TOKEN = 4.0    # everything else, code included

# Every provider reports how many prompt tokens it actually charged for. That is
# ground truth for this model, this language and this kind of content, so it is
# folded back in and beats any table of constants - including a bundled
# tokenizer, which would be some *other* model's idea of a token.
_correction = None               # observed tokens / estimated tokens
_CORRECTION_WEIGHT = 0.3         # how fast it follows a change of subject


def _wide_chars(text: str) -> int:
    """Characters from a script that packs roughly two per token."""
    return sum(1 for ch in text if
               "\u1100" <= ch <= "\u11ff" or      # Hangul Jamo
               "\u2e80" <= ch <= "\ua4cf" or      # CJK radicals, Kana, Han
               "\uac00" <= ch <= "\ud7a3" or      # Hangul syllables
               "\uf900" <= ch <= "\ufaff" or      # CJK compatibility
               "\uff00" <= ch <= "\uff60")        # fullwidth forms


def _raw_estimate(messages: list[dict]) -> float:
    total = 0.0
    for message in messages:
        content = _text(message)
        wide = _wide_chars(content)
        total += wide / _WIDE_CHARS_PER_TOKEN
        total += (len(content) - wide) / _LATIN_CHARS_PER_TOKEN
    return total


def _estimate_tokens(messages: list[dict]) -> int:
    return int(_raw_estimate(messages) * (_correction or 1.0))


def observe_usage(messages: list[dict], prompt_tokens: int) -> None:
    """Learn from what the provider says it actually counted.

    `messages` must be exactly what was sent, before the reply was appended.
    Wildly off ratios are ignored: they mean the two do not describe the same
    request - a cached prompt, or a provider that counts images.
    """
    global _correction
    if prompt_tokens <= 0:
        return
    raw = _raw_estimate(messages)
    if raw < 50:                        # too short to measure anything from
        return
    ratio = prompt_tokens / raw
    if not 0.25 <= ratio <= 4.0:
        return
    _correction = (ratio if _correction is None
                   else _correction * (1 - _CORRECTION_WEIGHT)
                        + ratio * _CORRECTION_WEIGHT)


def _get_ctx_budget() -> int:
    return int(config.NUM_CTX * 0.85) - config.NUM_PREDICT


def _get_summary_predict_tokens() -> int:
    # Sized off the local model's weights, which only Ollama reports; a hosted
    # model gets the middle setting.
    provider = providers.current()
    if provider.name != "ollama":
        return 400
    try:
        # Through the provider's host, not a bare `ollama.list()`: pointing the
        # harness at another machine used to move the chat and leave this
        # asking localhost what the model weighs.
        model_list = providers.ollama_client(getattr(provider, "host", "")).list()
        m_list = model_list.get("models", []) if isinstance(model_list, dict) else getattr(model_list, 'models', [])
        for m in m_list:
            name = m.get("model", m.get("name", "")) if isinstance(m, dict) else getattr(m, 'model', getattr(m, 'name', ''))
            if name == config.MODEL:
                size_gb = (m.get("size", 0) if isinstance(m, dict) else getattr(m, 'size', 0)) / (1024 ** 3)
                if size_gb >= 15.0: return 600
                if size_gb >= 7.0:  return 400
                return 250
    except Exception:
        pass
    return 300


# A tool result is trimmed from the middle, not from the end.
#
# Keeping only the front used to throw away the one line that mattered. The
# answer in a traceback is its **last** line - `RuntimeError: kaboom` - and the
# front is boilerplate: "Traceback (most recent call last):" and frames from
# inside the library. Head-only trimming kept the boilerplate and deleted the
# conclusion, and it did it permanently, because `manage_context` writes the
# trimmed list back over `messages`. On a local model, whose budget is small
# enough that this runs constantly, the harness was quietly removing the reason
# every failure failed and then wondering why the model could not fix it.
#
# The front still matters for the results that are not errors - the top of a
# file, the first search hits - so both ends are kept.
_TAIL_SHARE = 0.35

# Ceilings tried in order, loosest first, and only as far down as the budget
# forces. Trimming used to be unconditional at 3000 characters: with a 65536
# context and 49,000 tokens of headroom, asking to read a 12,000-character file
# returned a quarter of it and threw the rest away for good. The model then
# edited from a fragment - which is the failure the whole harness is built to
# avoid, arriving from inside the harness.
#
# 24000 stays as a backstop, because `read_file` has no ceiling of its own and a
# single result should not be able to eat the whole budget. The tighter steps
# only happen under real pressure, and compression follows if even 3000 is not
# enough.
_TRIM_STEPS = (24000, 12000, 6000, 3000)


def _text(message: dict) -> str:
    """A message's content as a string, whatever the message actually holds.

    `None` is the case that matters: a session file written with a null content,
    or a provider that reported a tool call and no text, puts one into the
    history - and every `startswith` below then raises on *every* later turn,
    because the bad message stays where it is. `_raw_estimate` has always
    defended against it; the rest of this module did not.
    """
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _trim_tool_results(messages: list[dict], max_chars: int = 3000) -> list[dict]:
    result = []
    for m in messages:
        content = _text(m)
        if m.get("role") == "user" and content.startswith("[Tool Result") and len(content) > max_chars:
            tail_chars = int(max_chars * _TAIL_SHARE)
            head_chars = max_chars - tail_chars
            omitted = len(content) - max_chars
            trimmed = (content[:head_chars]
                       + f"\n...[{omitted} chars omitted from the middle]...\n"
                       + content[-tail_chars:])
            result.append({**m, "content": trimmed})
        else:
            result.append(m)
    return result


def token_turns(history: list[dict] | None = None) -> list[dict]:
    """`config.token_history` folded into one entry per turn.

    The raw history has one entry per *request to the model*, and answering one
    thing the person asked for takes as many requests as it takes tools - plus
    six under deepthink, plus a whole sub-agent conversation. Reporting those
    separately said the person had asked five questions when they had asked one,
    and put the cost of a turn in five places.

    `requests` is kept because it is the part that was worth knowing: a turn
    that took nine requests is a turn worth looking at.

    An entry with no turn number comes from a session recorded before this
    existed. There is nothing to group it by, so it stands alone, exactly as it
    used to - and never merges with the entry beside it, which would invent a
    grouping the file does not support.
    """
    history = config.token_history if history is None else history
    turns: list[dict] = []
    previous = None
    for entry in history:
        turn = entry.get("turn")
        if turn is None or turn != previous or not turns:
            turns.append({"turn": turn, "prompt": 0, "completion": 0,
                          "cached": 0, "requests": 0})
        previous = turn
        turns[-1]["prompt"] += entry.get("prompt", 0) or 0
        turns[-1]["completion"] += entry.get("completion", 0) or 0
        turns[-1]["cached"] += entry.get("cached", 0) or 0
        turns[-1]["requests"] += 1
    return turns


def _get_conv_pairs(messages: list[dict]) -> list[list[dict]]:
    pairs: list[list[dict]] = []
    current: list[dict] = []
    for m in messages:
        if m.get("role") == "system":
            continue
        if m.get("role") == "user" and not _text(m).startswith("[Tool Result"):
            if current:
                pairs.append(current)
            current = [m]
        else:
            current.append(m)
    if current:
        pairs.append(current)
    return pairs


async def _compress_context(messages: list[dict]) -> bool:
    conv_msgs = [m for m in messages if m.get("role") != "system"]
    if not conv_msgs:
        return False

    predict_tokens = _get_summary_predict_tokens()
    prompt = smrp()
    for m in conv_msgs:
        role = "User" if m.get("role") == "user" else "Assistant"
        content = re.sub(r'<tool_call>.*?</tool_call>', '', _text(m), flags=re.DOTALL).strip()
        if content:
            if m.get("role") == "user" and _text(m).startswith("[Tool Result"):
                content = content[:400] + ("..." if len(content) > 400 else "")
            prompt += f"[{role}]: {content}\n\n"

    summary_msg = [{"role": "user", "content": prompt}]

    async def spinner():
        frames = [
            f"{S.PURPLE}    ·  {S.R}",
            f"{S.PURPLE}   · · {S.R}",
            f"{S.PURPLE}  · · ·{S.R}",
            f"{S.PURPLE} · · · {S.R}",
            f"{S.PURPLE}· · ·  {S.R}",
            f"{S.PURPLE} · ·   {S.R}",
        ]
        cycle = itertools.cycle(frames)
        try:
            while True:
                frame = next(cycle)
                sys.stdout.write(f'\r  {frame} {S.GRAY}Compressing context…{S.R}  ')
                sys.stdout.flush()
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()

    spin_task = asyncio.create_task(spinner())
    try:
        summary = await providers.complete(summary_msg, max_tokens=predict_tokens)
    except Exception:
        summary = ""
    finally:
        if not spin_task.done():
            spin_task.cancel()
            try: await spin_task
            except asyncio.CancelledError: pass

    if not summary or summary.startswith("(Summary failed"):
        return False

    sys_content = messages[0]["content"]
    sys_content = re.sub(r'\n\n<SUMMARY>.*?</SUMMARY>', '', sys_content, flags=re.DOTALL).strip()
    new_sys = f"{sys_content}\n\n<SUMMARY>\n{summary}\n</SUMMARY>"
    messages[0]["content"] = new_sys
    return True


def _sync_loaded_skills(messages: list[dict]) -> None:
    """Forget skills whose instructions were pruned out of the context.

    Without this the model is told a skill is "already loaded" long after the
    compressor dropped the message that carried its instructions.
    """
    if not config.LOADED_SKILLS:
        return
    blob = "\n".join(_text(m) for m in messages)
    config.LOADED_SKILLS[:] = [n for n in config.LOADED_SKILLS if f"[Skill: {n}]\nSource:" in blob]


def _sync_loaded_mcp_servers(messages: list[dict]) -> None:
    """Forget an MCP server whose tool listing was pruned out of the context.

    Same trap as the skills above, one layer along: the model is told a
    server's tools are "already loaded" after the compressor dropped the
    message that listed them, and then guesses their parameters.
    """
    if not config.LOADED_MCP_SERVERS:
        return
    from aetheris import mcp_client
    config.LOADED_MCP_SERVERS[:] = mcp_client.loaded_in(messages)


async def manage_context(messages: list[dict]) -> None:
    await _manage_context(messages)
    _sync_loaded_skills(messages)
    _sync_loaded_mcp_servers(messages)


async def _manage_context(messages: list[dict]) -> None:
    budget = _get_ctx_budget()

    # Each candidate is built from the untrimmed list, so a result is never
    # trimmed twice and the omission counts stay true.
    tightest = list(messages)
    for ceiling in _TRIM_STEPS:
        tightest = _trim_tool_results(messages, ceiling)
        if _estimate_tokens(tightest) <= budget:
            if tightest != messages:
                messages[:] = tightest
            return

    # None of the ceilings was enough, so the tightest one stands and the rest
    # of this function goes on to compress. Named rather than left as the loop
    # variable: an empty `_TRIM_STEPS` would otherwise be a NameError here.
    messages[:] = tightest

    pairs = _get_conv_pairs(messages)
    n_pairs = len(pairs)

    if n_pairs <= 2:
        if _estimate_tokens(messages) > budget:
            print(f"\n  {S.WARN}⚠ Context limit approaching. Compressing…{S.R}")
            ok = await _compress_context(messages)
            if ok:
                latest = pairs[-1] if pairs else []
                messages[:] = [messages[0]] + latest
                print(f"  {S.OK}✓ Context compressed.{S.R}\n")
        return

    print(f"\n  {S.WARN}⚠ Context limit approaching. Compressing…{S.R}")
    ok = await _compress_context(messages)
    if ok:
        keep = pairs[-2:]
        keep_msgs = [msg for pair in keep for msg in pair]
        messages[:] = [messages[0]] + keep_msgs
        print(f"  {S.OK}✓ Context compressed ({n_pairs} → {len(keep)} pairs kept).{S.R}\n")
        return

    print(f"  {S.WARN}⚠ Summary failed. Dropping oldest turns…{S.R}")
    while _estimate_tokens(messages) > budget and len(_get_conv_pairs(messages)) > 2:
        cur_pairs = _get_conv_pairs(messages)
        keep_pairs = cur_pairs[1:]
        keep_msgs = [msg for pair in keep_pairs for msg in pair]
        messages[:] = [messages[0]] + keep_msgs

    dropped = n_pairs - len(_get_conv_pairs(messages))
    if dropped > 0:
        print(f"  {S.OK}✓ Dropped {dropped} oldest turn(s).{S.R}\n")
