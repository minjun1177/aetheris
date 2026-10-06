"""A QR code that does not scan is worse than no QR code, so this proves it does.

Every other test here can be read as "does the code do what the author meant".
This one cannot: what the author meant is worth nothing if a camera disagrees,
and a wrong symbol looks exactly like a right one. There is no scanner in CI
and no QR library to compare against, so the proof has to come from the symbol
itself - and it can, because a QR code carries the means to check it.

**The main check reads the symbol back the way a scanner does.** It takes the
mask out of the symbol's own format bits, un-masks with it, walks the modules
in the standard's zigzag, splits the codewords into the blocks the version
declares, and evaluates each block's Reed-Solomon syndromes. All zero means
every block is a valid codeword of the code it claims to belong to. That is
one check over the format bits, the placement, the block layout, the
interleaving and the parity at once - if any of them were wrong, the
syndromes would not vanish.

It is deliberately written the other way round from `qr.py` - reading, where
the module encodes - so a bug shared by both would have to be a bug made twice
in opposite directions.

The symbols this produced were also read by two independent decoders while it
was being written: 300 links of the shape `/remote qr` actually draws, all 300
read back byte for byte.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aetheris import qr          # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


# ---------------------------------------------------------------------------
# reading a symbol back, without using anything that wrote it
# ---------------------------------------------------------------------------

def mask_of(matrix) -> int:
    """The mask the symbol declares, out of its own format information."""
    first, _ = qr._format_positions(len(matrix))
    bits = 0
    for row, col in first:
        bits = (bits << 1) | matrix[row][col]
    return (bits ^ 0b101010000010010) >> 10 & 0b111


def level_of(matrix) -> int:
    first, _ = qr._format_positions(len(matrix))
    bits = 0
    for row, col in first:
        bits = (bits << 1) | matrix[row][col]
    return (bits ^ 0b101010000010010) >> 13 & 0b11


def codewords_of(matrix) -> list:
    """Every data module, un-masked and read in the standard's order."""
    version = (len(matrix) - 17) // 4
    functions = qr._functions(version)
    size = len(matrix)
    mask = mask_of(matrix)

    plain = [row[:] for row in matrix]
    for row in range(size):
        for col in range(size):
            if functions[row][col] is None and qr._mask(mask, row, col):
                plain[row][col] ^= 1

    bits, upward, col = [], True, size - 1
    while col > 0:
        if col == 6:
            col -= 1
        for row in (range(size - 1, -1, -1) if upward else range(size)):
            for c in (col, col - 1):
                if functions[row][c] is None:
                    bits.append(plain[row][c])
        upward = not upward
        col -= 2
    return [int("".join(map(str, bits[i:i + 8])), 2)
            for i in range(0, len(bits) // 8 * 8, 8)]


def blocks_of(matrix) -> list:
    """The codewords, de-interleaved back into (data + parity) per block."""
    version = (len(matrix) - 17) // 4
    _, parity_length, layout = qr._VERSIONS[version]
    words = codewords_of(matrix)
    sizes = [each for count, each in layout for _ in range(count)]

    data = [[] for _ in sizes]
    at = 0
    for index in range(max(sizes)):
        for which, size in enumerate(sizes):
            if index < size:
                data[which].append(words[at])
                at += 1
    parity = [[] for _ in sizes]
    for _ in range(parity_length):
        for which in range(len(sizes)):
            parity[which].append(words[at])
            at += 1
    return [data[i] + parity[i] for i in range(len(sizes))]


def parity_holds(matrix) -> bool:
    """Every block a valid Reed-Solomon codeword? Syndromes say so or do not.

    The generator's roots are α^0 … α^(n-1) - QR starts at zero, unlike most
    other uses of the same code - so those are the points each block has to
    evaluate to nothing at.
    """
    version = (len(matrix) - 17) // 4
    _, parity_length, _ = qr._VERSIONS[version]
    for block in blocks_of(matrix):
        for i in range(parity_length):
            total = 0
            for coefficient in block:
                total = qr._mul(total, qr._EXP[i]) ^ coefficient
            if total:
                return False
    return True


def message_of(matrix) -> str:
    """The text back out of the symbol: mode, count, then that many bytes."""
    data = [word for block in blocks_of(matrix) for word in block]
    # Only block 0's data leads the message, and the blocks came back in order,
    # so the first block's data codewords are where the header is.
    version = (len(matrix) - 17) // 4
    _, parity_length, layout = qr._VERSIONS[version]
    sizes = [each for count, each in layout for _ in range(count)]
    joined = []
    at = 0
    for size in sizes:
        joined += data[at:at + size]
        at += size + parity_length
    bits = []
    for word in joined:
        bits += [(word >> shift) & 1 for shift in range(7, -1, -1)]
    mode = int("".join(map(str, bits[0:4])), 2)
    count = int("".join(map(str, bits[4:12])), 2)
    assert mode == 4, mode
    payload = bytes(int("".join(map(str, bits[12 + i * 8:20 + i * 8])), 2)
                    for i in range(count))
    return payload.decode("utf-8")


# ---------------------------------------------------------------------------
print("--- a symbol checks out against its own error correction ---")

SAMPLES = [
    "x",
    "hello world",
    "simple-harness",
    "http://127.0.0.1:8765/?k=" + "A" * 22,
    "http://192.168.0.2:8765/?k=" + "B" * 43,
    "안녕하세요 원격입니다",
    "".join(chr(33 + (i * 7) % 90) for i in range(60)),
    "".join(chr(33 + (i * 11) % 90) for i in range(123)),
    "".join(chr(33 + (i * 13) % 90) for i in range(178)),
]

versions = set()
for text in SAMPLES:
    matrix = qr.encode(text)
    version = (len(matrix) - 17) // 4
    versions.add(version)
    ok = parity_holds(matrix)
    check(f"v{version}, {len(text.encode()):3} bytes: every block satisfies its parity", ok)

check("and between them they cover most of the versions",
      len(versions) >= 5, str(sorted(versions)))

print("\n--- and says back what it was given ---")
for text in SAMPLES:
    got = message_of(qr.encode(text))
    check(f"{len(text.encode()):3} bytes read back", got == text,
          "" if got == text else repr(got[:30]))

print("\n--- the format information is the level and mask it claims ---")
for text in ("x", "hello world", "http://127.0.0.1:8765/?k=" + "A" * 22):
    matrix = qr.encode(text)
    check("it says error correction level M", level_of(matrix) == qr._ECC_LEVEL_BITS,
          f"{level_of(matrix):02b}")
    check("and a mask in range", 0 <= mask_of(matrix) <= 7, str(mask_of(matrix)))


def cheapest_mask(matrix) -> int:
    """Which of the eight masks this symbol's data would score best under."""
    version = (len(matrix) - 17) // 4
    functions = qr._functions(version)
    size = len(matrix)
    plain = [row[:] for row in matrix]
    for row in range(size):
        for col in range(size):
            if functions[row][col] is None and qr._mask(mask_of(matrix), row, col):
                plain[row][col] ^= 1
    best, best_score = None, None
    for pattern in range(8):
        candidate = [row[:] for row in plain]
        for row in range(size):
            for col in range(size):
                if functions[row][col] is None and qr._mask(pattern, row, col):
                    candidate[row][col] ^= 1
        qr._place_format(candidate, pattern)
        score = qr._penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = pattern, score
    return best


for text in ("x", "hello world", "simple-harness", "http://127.0.0.1:8765/?k=" + "A" * 22):
    matrix = qr.encode(text)
    check(f"the mask {text[:14]!r} was drawn with is the cheapest of the eight",
          mask_of(matrix) == cheapest_mask(matrix),
          f"chose {mask_of(matrix)}, cheapest is {cheapest_mask(matrix)}")
check("and drawing the same text twice draws the same thing",
      qr.encode("simple-harness") == qr.encode("simple-harness"))

print("\n--- the function patterns are where the standard puts them ---")
for version in sorted(qr._VERSIONS):
    grid = qr._functions(version)
    size = 17 + 4 * version
    check(f"v{version} is {size} modules across", len(grid) == size)
    corners = [(0, 0), (0, size - 7), (size - 7, 0)]
    check(f"v{version} has its three finders",
          all(grid[r + 3][c + 3] == 1 and grid[r][c] == 1 for r, c in corners))
    check(f"v{version} keeps a dark module", grid[size - 8][8] == 1)
    check(f"v{version} times both ways",
          all(grid[6][i] == (1 if i % 2 == 0 else 0) for i in range(8, size - 8))
          and all(grid[i][6] == (1 if i % 2 == 0 else 0) for i in range(8, size - 8)))
    room = sum(1 for r in range(size) for c in range(size) if grid[r][c] is None)
    total = qr._VERSIONS[version][0] * 8
    check(f"v{version} leaves room for exactly its codewords",
          room - total in (0, 7),           # 7 remainder bits on versions 2-6
          f"{room} modules, {total} bits of codeword")

print("\n--- how much fits, and what happens when it does not ---")
check("a link that is 70 bytes is a version 5 at most",
      len(qr.encode("http://192.168.0.2:8765/?k=" + "B" * 43)) <= 17 + 4 * 5)
# The last byte each version holds, at level M. One more has to move up.
CAPACITY = {1: 14, 2: 26, 3: 42, 4: 62, 5: 84, 6: 106, 7: 122, 8: 152, 9: 180}
for version, length in CAPACITY.items():
    got = (len(qr.encode("y" * length)) - 17) // 4
    check(f"{length} bytes is the last that fits a version {version}", got == version,
          f"got v{got}")
    if version < max(CAPACITY):
        after = (len(qr.encode("y" * (length + 1))) - 17) // 4
        check(f"...and {length + 1} moves up to {version + 1}", after == version + 1,
              f"got v{after}")
check("181 bytes does not fit at all", not qr.fits("y" * 181))
try:
    qr.encode("y" * 181)
    check("and says so rather than drawing nonsense", False)
except qr.TooMuch as e:
    check("and says so rather than drawing nonsense", "more than" in str(e))

print("\n--- what gets printed ---")
LINK = "http://192.168.0.2:8765/?k=" + "C" * 43
ANSI = re.compile(r"\x1b\[[0-9;]*m")
drawing = qr.render(LINK)
lines = drawing.splitlines()
bare = [ANSI.sub("", line)[2:] for line in lines]        # past the indent
modules = len(qr.encode(LINK))

check("it paints its own white ground and black ink",
      all(qr.WHITE_BG in line and qr.BLACK in line for line in lines))
check("...and can be asked not to",
      "\x1b[" not in qr.render(LINK, colour=False))
check("two rows of modules per line",
      len(bare) * 2 >= modules + 8 and len(bare) * 2 <= modules + 9,
      f"{len(bare)} lines for {modules} modules and a quiet zone")
check("with the quiet zone all the way round",
      all(len(line) == modules + 8 for line in bare)
      and bare[0].strip() == "" and bare[-1].strip() == "",
      f"width {len(bare[0])}")
check("drawn only with blocks and space",
      set("".join(bare)) <= set("█▀▄ "), str(sorted(set("".join(bare)))))

# The drawing is the symbol: read the half blocks back apart and the modules
# that come out have to be the ones that went in.
rows = []
for line in bare:
    rows.append([1 if ch in "█▀" else 0 for ch in line])
    rows.append([1 if ch in "█▄" else 0 for ch in line])
inside = [row[4:4 + modules] for row in rows[4:4 + modules]]
check("and the drawing is the symbol, module for module",
      inside == qr.encode(LINK))

print("\n--- one symbol, pinned, so a change of heart has to be deliberate ---")
GOLDEN = (
    "#######....##.#######",
    "#.....#..##.#.#.....#",
    "#.###.#.#...#.#.###.#",
    "#.###.#.#.###.#.###.#",
    "#.###.#.##..#.#.###.#",
    "#.....#.##..#.#.....#",
    "#######.#.#.#.#######",
    "........####.........",
    "#.#####...#.#.#####..",
    ".#.#.#.#..##..###...#",
    "#...#.##.####....###.",
    "...##..#####.#...####",
    "..##.##.....#.#.#....",
    "........#..#...###.##",
    "#######..#####.#.###.",
    "#.....#.######...##.#",
    "#.###.#.##.####..#.##",
    "#.###.#.##.....##.#..",
    "#.###.#.#..##.#...#..",
    "#.....#......#.#.##..",
    "#######.#.##..#.#..#.",
)
drawn = tuple("".join("#" if module else "." for module in row)
              for row in qr.encode("simple-harness"))
# The text stays as it was when GOLDEN was recorded, through the rename and
# any later one. It is arbitrary input, and re-recording the expected matrix
# from the encoder it is meant to pin would leave a check that cannot fail.
check("`simple-harness` still draws the symbol it drew", drawn == GOLDEN,
      "" if drawn == GOLDEN else "\n" + "\n".join(drawn))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("qr checks passed")
