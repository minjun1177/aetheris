"""Editing a file by naming its lines instead of quoting them.

`read_file` has always returned `50:1fa|    print(answer)`, and `edit_file` has
always thrown that prefix away - so to change one line the model had to
reproduce it exactly: every space of indentation, every quote, every backslash.
That is the thing a small model gets wrong most often, and when the line
appeared twice in the file it could not be done at all, because the snippet was
ambiguous and the edit was refused.

An `old_content` made of those prefixes names the lines directly. The checks
here are in two halves, and the second half matters more: that the anchors
reach the right lines, and that an anchor which no longer describes the file
is *refused* rather than applied a few lines off - which would be a silent
wrong edit, the worst thing this tool can do.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from aetheris import paths

HOME = tempfile.mkdtemp(prefix="hashline-home-")
os.environ[paths.ENV_VAR] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.GIT_AUTO_COMMIT = False
config.CHANNEL_ENABLED = False             # the board is tested in test_channel.py

from aetheris import tools           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


WORK = tempfile.mkdtemp(prefix="hashline-work-")

# Lines 3 and 8 are identical on purpose: that is the case the old text match
# could not do at all.
SAMPLE = ('def greet(name):\n'
          '    answer = "hi " + name\n'
          '    print(answer)\n'
          '    return answer\n'
          '\n'
          'def farewell(name):\n'
          '    answer = "bye " + name\n'
          '    print(answer)\n'
          '    return answer\n')


def sample(name="demo.py", text=SAMPLE):
    path = os.path.join(WORK, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def edit(path, old, new):
    return tools.handle_edit_file(path, old, new)


def line_of(path, number):
    return read(path).split("\n")[number - 1]


print("--- the whole edit is one row: the anchor, and what the line becomes ---")
path = sample()
result = edit(path, "", '3:9b2|    print("said:", answer)')
check("new_content alone, with no old_content at all, edits the line",
      result.startswith("[Success"), result)
check("and it is the right line", line_of(path, 3) == '    print("said:", answer)',
      line_of(path, 3))
check("the identical line further down is untouched",
      line_of(path, 8) == "    print(answer)", line_of(path, 8))
check("nothing moved, and the result says so",
      len(read(path).split("\n")) == 10 and "nothing below moved" in result, result)

path = sample()
result = edit(path, "", '2:cd7|    answer = f"hi {name}"\n8:9b2|    pass')
check("several rows change several lines", result.startswith("[Success"), result)
check("which need not be next to each other",
      line_of(path, 2) == '    answer = f"hi {name}"' and line_of(path, 8) == "    pass",
      f"{line_of(path, 2)!r} {line_of(path, 8)!r}")
check("and the lines between them are untouched",
      line_of(path, 3) == "    print(answer)" and line_of(path, 5) == "")
check("the result names every line it changed", "lines 2, 8 replaced" in result, result)

path = sample()
same_line = edit(path, "", "3:9b2|a\n3:9b2|b")
check("naming one line twice is refused", same_line.startswith("[Error]"), same_line)
check("because a line can only become one thing", "twice" in same_line, same_line)
check("and nothing is written", read(path) == SAMPLE)

print("\n--- the hash is the only check this form has, so it is not waived ---")
path = sample()
stale = edit(path, "", "3:0ee|    pass")
check("a hash that does not match refuses the edit", stale.startswith("[Error]"), stale)
check("the file is untouched", read(path) == SAMPLE)
check("and the refusal says what is on that line now",
      "3:9b2|    print(answer)" in stale, stale)
check("saying why nothing was written",
      "overwrite something you have not read" in stale, stale)

path = sample()
missing = edit(path, "", "99:9b|    pass")
check("a line the file does not have is refused", missing.startswith("[Error]"), missing)
check("and it says how many lines there are", "10 lines" in missing, missing)

path = sample()
clash = edit(path, "1:eb1", "3:9b2|    pass")
check("an old_content naming other lines is refused rather than half-obeyed",
      clash.startswith("[Error]"), clash)
check("and says which lines new_content meant", "line(s) 3" in clash, clash)
check("nothing is written", read(path) == SAMPLE)
check("the same anchors in both is accepted",
      edit(path, "3:9b2", "3:9b2|    pass").startswith("[Success"))
check("and edits that line", line_of(path, 3) == "    pass")

path = sample()
check("an indented row is still read as an anchor, not written literally",
      edit(path, "", "    3:9b2|    pass").startswith("[Success"))
check("so the file never gets an anchor written into it",
      "3:9b2|" not in read(path) and line_of(path, 3) == "    pass", line_of(path, 3))

print("\n--- the longer form is for what the short one cannot do ---")
path = sample()
listing = tools.handle_read_file(path).split("\n")
check("read_file still prints the anchor it is asked for",
      listing[2] == "3:9b2|    print(answer)", listing[2])

result = edit(path, "3:9b2", '    print("said:", answer)')
check("an anchor in old_content edits that line", result.startswith("[Success"), result)
check("and it is the right line", line_of(path, 3) == '    print("said:", answer)',
      line_of(path, 3))
check("the identical line further down is untouched",
      line_of(path, 8) == "    print(answer)", line_of(path, 8))
check("nothing else moved", len(read(path).split("\n")) == 10)

path = sample()
result = edit(path, "3:9b2|    print(answer)", '    print("said:", answer)')
check("the whole row copied out of the listing works too",
      result.startswith("[Success") and line_of(path, 3) == '    print("said:", answer)',
      result)

print("\n--- which is the case the text match could never do ---")
path = sample()
duplicate = edit(path, "    print(answer)", "    pass")
check("quoting a line that appears twice is still refused",
      duplicate.startswith("[Error]") and "2 times" in duplicate, duplicate)
check("and the refusal now says what to do instead", "50:1fa" in duplicate, duplicate)
check("the file was not touched", read(path) == SAMPLE)
check("by anchor the same edit lands", edit(path, "8:9b2", "    pass").startswith("[Success"))
check("on the second one", line_of(path, 8) == "    pass" and
      line_of(path, 3) == "    print(answer)")

print("\n--- an anchor that no longer describes the file is refused ---")
path = sample()
stale = edit(path, "3:0ee", "    pass")
check("a hash that does not match refuses the edit", stale.startswith("[Error]"), stale)
check("the file is untouched", read(path) == SAMPLE)
check("and the refusal says what is actually on that line",
      "3:9b2|    print(answer)" in stale, stale)
check("and what to do about it", "read_file" in stale)

# The real thing this protects against: the file moved under the model.
path = sample()
edit(path, "1:eb1", "def greet(name):\n    # a new line, everything below shifts")
after = edit(path, "3:9b2", "    pass")
check("an anchor taken before an edit does not fire after it",
      after.startswith("[Error]"), after)
check("nothing was written at the wrong place",
      "print(answer)" in read(path) and "pass" not in read(path))

print("\n--- but a mistyped hash beside the right line is forgiven ---")
path = sample()
# Two hand-copied hex characters are far easier to get wrong than the line
# itself, so the line is the stronger evidence when the two disagree.
typo = edit(path, "3:zz|    print(answer)", "    pass")
check("a malformed hash is not an anchor at all - it falls through to text",
      typo.startswith("[Error]"), typo[:60])
typo = edit(path, "3:abc|    print(answer)", "    pass")
check("a wrong-but-well-formed hash beside the exact line is accepted",
      typo.startswith("[Success"), typo)
check("and it edited that line", line_of(path, 3) == "    pass")

path = sample()
both_wrong = edit(path, "3:abc|    print(nothing)", "    pass")
check("a wrong hash beside the wrong text is refused",
      both_wrong.startswith("[Error]"), both_wrong[:70])
check("the file is untouched", read(path) == SAMPLE)

print("\n--- a span still spelled with the pipes read_file printed ---")
# `read_file` puts a `|` after every anchor it produces, so a model writing a
# span puts one there too. That used to fall through to text matching and come
# back as "old_content was not found", which says nothing about the anchor being
# one character off - a local 4B model spent eight tool calls resending it.
SPAN_NEW = 'def farewell(name):\n    return "bye " + name'
for label, spelling in (("bare", "6:cae-9:964"),
                        ("with a trailing pipe", "6:cae-9:964|"),
                        ("with the row's text after it", "6:cae-9:964|    return answer"),
                        ("with a pipe on both ends", "6:cae|def farewell(name):-9:964|")):
    path = sample()
    result = edit(path, spelling, SPAN_NEW)
    check(f"a span {label} resolves to the same lines",
          result.startswith("[Success"), result[:70])
    check("  ...and replaced them", read(path).split("\n")[5:7] ==
          ["def farewell(name):", '    return "bye " + name'])

# One anchor whose quoted text happens to end in something shaped like the far
# end of a span. Reading that as a span would edit a line the model never named,
# so the ordinary one-anchor reading has to win - which is why the piped span is
# tried last rather than first.
path = sample("hyphen.py", "a = 1\nb = 2\nc = x-4:964\nd = 4\n")
one_row = edit(path, "3:d0e|c = x-4:964", "c = 3")
check("a lone anchor is not re-read as a span because its text has a hyphen",
      one_row.startswith("[Success"), one_row[:70])
check("so only the line it named changed",
      read(path) == "a = 1\nb = 2\nc = 3\nd = 4\n", repr(read(path)))

print("\n--- a row that names a line and quotes it, with no hash at all ---")
# The hash is two hand-copied characters and the quoted line is not, so a row
# that proves itself against the file is as good as one carrying the hash -
# this is the rule already applied when a hash is *mistyped*, extended to one
# that was never written. It is taken only when every row verifies; when it
# does not, the content goes back to being matched as text, so nothing that
# worked before can start failing here.
path = sample()
result = edit(path, "3     print(answer)", "    pass")
check("a hash-less row whose text matches edits that line",
      result.startswith("[Success"), result[:70])
check("and it is the right line", line_of(path, 3) == "    pass")
check("the identical line 8 is untouched", line_of(path, 8) == "    print(answer)")

path = sample()
check("the pipe spelling works too",
      edit(path, "3|    print(answer)", "    pass").startswith("[Success"))
check("on the same line", line_of(path, 3) == "    pass")

path = sample()
wrong = edit(path, "3     print(nothing)", "    pass")
check("a hash-less row whose text does not match is refused",
      wrong.startswith("[Error]"), wrong[:60])
check("the file is untouched", read(path) == SAMPLE)
check("and the refusal shows what that line really says",
      "3:9b2|    print(answer)" in wrong, wrong)

# The guarantee that makes the whole thing safe to add: a snippet that merely
# looks like a numbered row is still matched as text.
TEXTY = "intro\n3 spaces are required\ntail\n"
path = sample("doc.md", TEXTY)
check("a real snippet shaped like an anchor is still matched as text",
      edit(path, "3 spaces are required", "4 spaces are required").startswith("[Success"))
check("and it edited the text, not line 3",
      read(path) == "intro\n4 spaces are required\ntail\n", repr(read(path)))

print("\n--- the same row in new_content, where nothing can vouch for it ---")
# Here the text after the `|` is what the line is to *become*, so there is no
# second piece of evidence the way there is in old_content - only the hash can
# say the model has read what it is about to overwrite. Without this the
# literal string "3|    pass" is what would be written into the file.
path = sample()
bare = edit(path, "", "3|    pass")
check("a hash-less row in new_content is refused", bare.startswith("[Error]"), bare[:60])
check("the file is untouched", read(path) == SAMPLE)
check("and it is not written in literally", "3|" not in read(path))
check("the refusal carries the line, ready to copy back",
      "3:9b2|    print(answer)" in bare, bare)

path = sample()
literal = edit(path, "3:9b2", "1|a\n2|b")
check("but rows like that are still ordinary text when old_content named lines",
      literal.startswith("[Success"), literal[:60])
check("and are written as they stand", line_of(path, 3) == "1|a" and
      line_of(path, 4) == "2|b", repr(read(path)))

print("\n--- a <content> block on an edit_file call ---")
# `content` is write_file's block name, and a model that has just used it
# reaches for it again. Without the alias the block is dropped on the floor and
# the model is told old_content was empty, which is true and unhelpful.
from aetheris import toolspec          # noqa: E402
check("content binds to new_content",
      toolspec.get("edit_file").bind({"filepath": "x", "content": "y"}) == ["x", "", "y"],
      str(toolspec.get("edit_file").bind({"filepath": "x", "content": "y"})))
path = sample()
check("so the call goes through",
      tools.dispatch_tool("edit_file",
                          {"filepath": path, "content": "3:9b2|    pass"}).startswith("[Success"))
check("and edits the line", line_of(path, 3) == "    pass")

print("\n--- a hash that agrees with a line the model quoted differently ---")
# Three hex characters are 4096 values, and an anchor points at a *position*:
# a line that has moved away and a different line that has moved in collide
# once in 4096. When the row quotes the line, that quote catches the collision
# - so when the two disagree, the quote decides, in both directions.
path = sample()
collision = edit(path, "3:9b2|    print(nothing at all)", "    pass")
check("a matching hash beside a line quoted differently is refused",
      collision.startswith("[Error]"), collision[:70])
check("the file is untouched", read(path) == SAMPLE)
check("and the refusal shows both what was claimed and what is there",
      "print(nothing at all)" in collision and "3:9b2|    print(answer)" in collision,
      collision)
check("while the hash alone still decides when nothing is quoted",
      edit(path, "3:9b2", "    pass").startswith("[Success"))

print("\n--- a two-character anchor is read, and refused ---")
# Anchors got a third character. One left over from an older listing can never
# match, but it is still *recognised*, so what comes back names the line and
# gives the anchor that would have worked - which is the difference between a
# model that corrects itself and one that resends.
path = sample()
short = edit(path, "3:9b", "    pass")
check("an anchor a character short is refused", short.startswith("[Error]"), short[:60])
check("and it is read as an anchor, not as text",
      "Line 3" in short and "3:9b2|" in short, short)
check("the file is untouched", read(path) == SAMPLE)
check("the hash is three characters wide", len(tools._line_hash("x")) == 3)

print("\n--- an edit hands back the lines around it, already anchored ---")
# Editing a line changes its hash, and changing the line count moves every
# anchor below. Reading the whole file again to recover a few lines is a round
# trip and a context full of file, so the neighbourhood comes back with the
# result - current, and in the format the next edit is written in.
path = sample()
result = edit(path, "", "3:9b2|    pass")
check("a one-row edit echoes the region", "The file now reads" in result, result[:80])
check("with the line it just wrote, freshly hashed",
      f"3:{tools._line_hash('    pass')}|    pass" in result, result)
check("and its neighbours", "2:cd7|" in result and "4:964|" in result, result)
check("it does not claim anything moved", "have moved with it" not in result, result)

path = sample()
result = edit(path, "6:cae-9:964", "def farewell(name):")
check("a span that shortens the file echoes the region too",
      "The file now reads" in result, result[:80])
check("and says the anchors outside it have moved",
      "have moved with it" in result, result)
check("the echoed anchors are the ones that now describe the file",
      all(f"{n}:{tools._line_hash(line_of(path, n))}|" in result
          for n in range(2, 7)), result)

path = sample("wide.py", "\n".join(f"line {n}" for n in range(1, 200)) + "\n")
big = edit(path, "", "\n".join(f"{n}:{tools._line_hash(f'line {n}')}|changed {n}"
                               for n in (10, 100, 190)))
check("scattered edits echo each neighbourhood, not the span between them",
      big.count("…") >= 2 and "50:" not in big, big[-400:])

print("\n--- several lines at once ---")
path = sample()
result = edit(path, "6:cae-9:964", 'def farewell(name):\n    return "bye " + name')
check("a span replaces every line in it", result.startswith("[Success"), result)
check("with the new text", read(path).split("\n")[5:7] ==
      ["def farewell(name):", '    return "bye " + name'], str(read(path).split("\n")))
check("and says the lines below have moved", "moved" in result, result)

path = sample()
result = edit(path, "3:9b2\n4:964", "    return None")
check("a run of anchors, one per line, does the same",
      result.startswith("[Success"), result)
check("replacing both of them", read(path).split("\n")[2] == "    return None" and
      read(path).split("\n")[3] == "")

path = sample()
result = edit(path, "3:9b2\n4:964", "    print(answer)\n    return answer")
check("a replacement of the same length does not claim anything moved",
      result.startswith("[Success") and "moved" not in result, result)

print("\n--- anchors that do not describe a run of lines are refused ---")
path = sample()
gap = edit(path, "3:9b2\n5:d41", "    pass")
check("a gap between anchors is refused", gap.startswith("[Error]"), gap)
check("and the span form is offered instead", "3:9b2-5:d41" in gap, gap)
check("the file is untouched", read(path) == SAMPLE)

backwards = edit(path, "9:964-3:9b2", "    pass")
check("a span written backwards is refused", backwards.startswith("[Error]"), backwards)

missing = edit(path, "99:9b", "    pass")
check("a line the file does not have is refused", missing.startswith("[Error]"), missing)
check("and the refusal says how many lines there are", "10 lines" in missing, missing)
check("the file is still untouched", read(path) == SAMPLE)

print("\n--- deleting, and the shape of the file afterwards ---")
path = sample()
result = edit(path, "4:964\n5:d41", "")
check("an empty replacement removes the lines", result.startswith("[Success"), result)
check("without leaving a blank one behind",
      read(path).split("\n")[3] == "def farewell(name):", str(read(path).split("\n")[:5]))
check("and the rest of the file is intact",
      read(path) == ('def greet(name):\n'
                     '    answer = "hi " + name\n'
                     '    print(answer)\n'
                     'def farewell(name):\n'
                     '    answer = "bye " + name\n'
                     '    print(answer)\n'
                     '    return answer\n'), repr(read(path)))

path = sample()
edit(path, "1:eb1", "def greet(name):")
check("a file that ended in a newline still does", read(path).endswith("answer\n"))
check("and has not grown a line", len(read(path).split("\n")) == 10)

print("\n--- everything that is not an anchor still behaves as it did ---")
path = sample()
result = edit(path, '    answer = "hi " + name', '    answer = f"hi {name}"')
check("an ordinary snippet is still matched as text", result.startswith("[Success"), result)
check("and replaced", line_of(path, 2) == '    answer = f"hi {name}"')

path = sample()
# One ordinary line makes the whole snippet text - anchors have to be certain
# before they take over. `_strip_hashlines` then declines to strip a snippet
# that is only half-prefixed (its 0.8 ratio, unchanged), so this is refused
# rather than half-understood. It was refused before this feature too; what is
# new is that the refusal says what to do instead.
mixed = edit(path, '3:9b2|    print(answer)\n    return answer', "    pass")
check("a snippet that is only partly anchors is not treated as anchors",
      mixed.startswith("[Error]"), mixed[:60])
check("and it is refused rather than half-stripped", read(path) == SAMPLE)
check("with the refusal pointing at the anchor form", "anchor" in mixed, mixed)
check("while every row being an anchor does take over",
      edit(path, '3:9b2|    print(answer)\n4:964|    return answer', "    pass"
           ).startswith("[Success"))
check("replacing exactly those lines",
      read(path).split("\n")[2:4] == ["    pass", ""], str(read(path).split("\n")[:5]))

path = sample()
gone = edit(path, "    print(nothing_like_this)", "    pass")
check("a snippet that is not there is still refused", gone.startswith("[Error]"), gone)
check("and now says the anchor is the way round it", "anchor" in gone, gone)

path = sample()
result = edit(path, "3:9b2", "3:9b2|    print(answer, flush=True)")
check("hashline prefixes in new_content are still stripped",
      line_of(path, 3) == "    print(answer, flush=True)", line_of(path, 3))

path = sample()
empty = edit(path, "", "    pass")
check("replacement text with no anchors and no old_content is refused",
      empty.startswith("[Error]"), empty)
check("rather than matched against the whole file", read(path) == SAMPLE)
check("and it says both ways of naming the lines", "50:1fa|" in empty, empty)

print("\n--- the user still decides ---")
path = sample()
approvals = []
real_prompt = tools._approval_prompt


def refuse(title, details, rule=""):
    approvals.append(dict(details))
    return False


tools._approval_prompt = refuse
try:
    denied = edit(path, "3:9b2", "    pass")
finally:
    tools._approval_prompt = real_prompt
check("an anchored edit is still put to the user", len(approvals) == 1)
check("and shows the real lines, not the anchor",
      approvals[0].get("from") == "    print(answer)", str(approvals[0]))
check("saying which lines they are", approvals[0].get("replacing") == "line 3",
      str(approvals[0]))
check("declining refuses it", denied.startswith("[System]"), denied)
check("and writes nothing", read(path) == SAMPLE)

approvals.clear()
tools._approval_prompt = refuse
try:
    denied = edit(path, "", "3:9b2|    pass")
finally:
    tools._approval_prompt = real_prompt
check("a one-row edit is put to the user too", len(approvals) == 1)
check("showing the line before and after",
      approvals[0].get("line 3") == "    print(answer)  →      pass", str(approvals[0]))
check("declining refuses it", denied.startswith("[System]") and read(path) == SAMPLE)

print("\n--- line 0 is not a line, and saying so is not optional ---")
# `_anchor_problem` looked for the first out-of-range number with
# `next(..., 0)`, so the one value it could not report was 0 - the sentinel and
# the answer were the same. `0:<hash>` therefore skipped the range check and
# fell through to the hash comparison, which indexes `lines[0 - 1]`: the LAST
# line of the file. A hash that matched it made the anchor "verified", and
# `_edit_by_anchor(start=0)` then assigned to `lines[-1:0]` - an empty slice -
# so the replacement was INSERTED near the end of the file and reported as
# "[Success] line 0 replaced". A write nobody asked for under a success
# message is the exact failure invariant 5.12 exists to prevent.
path = sample("zero.py", "alpha\nbeta\ngamma\n")
tail_hash = tools._line_hash("")           # the empty line a trailing \n leaves
result = edit(path, f"0:{tail_hash}", "REPLACEMENT")
check("an anchor on line 0 is refused", result.startswith("[Error]"), result)
check("and says there is no such line", "no line 0" in result, result)
check("and writes nothing", read(path) == "alpha\nbeta\ngamma\n", repr(read(path)))
check("a line past the end is still refused too",
      edit(path, "99:aaa", "x").startswith("[Error]"))
check("and the real lines still edit",
      edit(path, "", f"2:{tools._line_hash('beta')}|BETA").startswith("[Success"))
check("in the right place", read(path) == "alpha\nBETA\ngamma\n", repr(read(path)))


print("\n--- the refusal that hands back the lines it meant cannot itself fail ---")
# `38|print()` names a line and carries no hash, so it is refused - and the
# refusal shows what those lines actually say, so the next call can be right
# without a read_file round trip. It built its example from `numbers[0]`
# unconditionally, so a call naming one real line and one imaginary one -
# enough for a listing, not enough for `lines[numbers[0] - 1]` - raised
# IndexError out of the handler, and the model was told "edit_file raised
# IndexError" instead of being shown the anchors it was missing.
path = sample("nohash.py", "a = 1\nb = 2\nc = 3\n")
result = edit(path, "", "9999|x\n2|y")
one_line = " ".join(result.split())[:110]
check("an out-of-range row first does not raise", result.startswith("[Error]"), one_line)
check("the listing shows the line that does exist", "2:" in result)
check("the example names a line that is really there",
      f"2:{tools._line_hash('b = 2')}|" in result)
check("and nothing was written", read(path) == "a = 1\nb = 2\nc = 3\n")
result = edit(path, "", "9999|x")
check("with no real line at all it falls back to the plain error",
      result.startswith("[Error]") and "old_content was empty" in result)
check("and still writes nothing", read(path) == "a = 1\nb = 2\nc = 3\n")


print("\n--- a missing file is a missing file, whichever form is used ---")
absent = os.path.join(WORK, "not-here.py")
check("by one-row anchor", edit(absent, "", "1:aaa|x").startswith("[Error]"))
check("by anchor", edit(absent, "1:aaa", "x").startswith("[Error]"))
check("by text", edit(absent, "x", "y").startswith("[Error]"))

shutil.rmtree(HOME, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("hashline edit checks passed")
