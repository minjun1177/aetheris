"""A QR code, drawn in the terminal, with nothing installed to draw it.

`/remote on` prints a link with a token in it. Typing forty-three random
characters into a phone is not a thing anybody does twice, so the link is also
offered as something a camera can read.

Which needs a QR encoder, and the honest options were a dependency or this.
The dependency lost on the same grounds everything else here is weighed: this
is one screen of a harness, the algorithm is a published standard that has not
moved since 2006, and `pip install` asking for a wheel because somebody wanted
to scan a URL is a poor trade. So: byte mode, error correction level M,
versions 1 to 9 - up to 180 bytes, against a link that is about seventy - and
stdlib only.

**Getting it right is the whole problem.** A QR that does not scan is worse
than no QR, because it is not obvious from looking at it. So `tests/test_qr.py`
reads every symbol back the way a scanner does - the mask out of the symbol's
own format bits, the zigzag, the blocks the version declares - and checks that
each block still satisfies its Reed-Solomon parity. Nothing can satisfy that by
accident: it is one check over the format bits, the placement, the block
tables, the interleaving and the arithmetic at once. What cannot be derived is
tabulated below; the rest is computed, so the format and version bits come out
of the BCH codes they are defined by rather than a list of constants.

While this was written its output was also put in front of two independent
decoders: 300 links of the shape `/remote qr` draws, all 300 read back byte for
byte.

What is here:

    encode(text)    the modules, as rows of 0 and 1, no quiet zone
    render(text)    those modules as something to print

`render` draws two rows of modules per line of text with a half block, which
is what makes the modules square on a terminal whose cells are not. It paints
its own white background and black foreground rather than using the
terminal's, because a QR drawn in a dark theme is an inverted QR, and while
most phones now read those, "most" is not a thing to rely on for the one
feature whose entire job is to be read by a phone.
"""

# --- the tables that have to be right ---------------------------------------

# version -> (total codewords, ecc codewords per block, [(blocks, data each)])
# Level M throughout: a screen is a clean surface, but the camera pointed at it
# is usually at an angle, and M is what the standard suggests for exactly that.
_VERSIONS = {
    1: (26, 10, [(1, 16)]),
    2: (44, 16, [(1, 28)]),
    3: (70, 26, [(1, 44)]),
    4: (100, 18, [(2, 32)]),
    5: (134, 24, [(2, 43)]),
    6: (172, 16, [(4, 27)]),
    7: (196, 18, [(4, 31)]),
    8: (242, 22, [(2, 38), (2, 39)]),
    9: (292, 22, [(3, 36), (2, 37)]),
}

# version -> the row/column centres its alignment patterns sit on
_ALIGNMENT = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
}

_ECC_LEVEL_BITS = 0b00          # M, as it appears in the format information

# The 1:1:3:1:1 run a scanner looks for when it is hunting finder patterns.
_RATIO = [1, 0, 1, 1, 1, 0, 1]
_PAD = (0xEC, 0x11)             # the two pad bytes, alternating

LARGEST = max(_VERSIONS)


class TooMuch(ValueError):
    """More bytes than a version 9 code holds. The caller says what to do."""


# --- GF(256), which Reed-Solomon needs --------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256


def _build_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:            # the primitive polynomial QR is defined over
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()


def _mul(a: int, b: int) -> int:
    if not a or not b:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list:
    """The generator polynomial for `degree` error-correction codewords."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for index, coefficient in enumerate(poly):
            nxt[index] ^= _mul(coefficient, 1)
            nxt[index + 1] ^= _mul(coefficient, _EXP[i])
        poly = nxt
    return poly


def _remainder(data: list, degree: int) -> list:
    """The error-correction codewords for one block."""
    generator = _generator(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result[0]
        result = result[1:] + [0]
        for index, coefficient in enumerate(generator[1:]):
            result[index] ^= _mul(coefficient, factor)
    return result


# --- the bits ---------------------------------------------------------------

def _pick_version(length: int) -> int:
    for version in sorted(_VERSIONS):
        _, ecc, blocks = _VERSIONS[version]
        data_codewords = sum(count * each for count, each in blocks)
        if length + 2 <= data_codewords:      # 4 bits of mode + 8 of count
            return version
    raise TooMuch(f"{length} bytes is more than a version {LARGEST} code holds")


def _codewords(payload: bytes, version: int) -> list:
    """Mode, length, the bytes, the padding - as whole codewords."""
    _, ecc_per_block, blocks = _VERSIONS[version]
    data_codewords = sum(count * each for count, each in blocks)

    bits = [0, 1, 0, 0]                                   # byte mode
    bits += [(len(payload) >> shift) & 1 for shift in range(7, -1, -1)]
    for byte in payload:
        bits += [(byte >> shift) & 1 for shift in range(7, -1, -1)]

    room = data_codewords * 8
    bits += [0] * min(4, room - len(bits))                # terminator
    bits += [0] * (-len(bits) % 8)                        # up to a whole byte

    words = [int("".join(str(bit) for bit in bits[i:i + 8]), 2)
             for i in range(0, len(bits), 8)]
    while len(words) < data_codewords:
        words.append(_PAD[(len(words) - len(bits) // 8) % 2])
    return words


def _interleave(words: list, version: int) -> list:
    """The codewords in the order the standard puts them on the grid.

    Data from every block first, one codeword at a time round the blocks, then
    the error correction the same way. It is what makes a scratch across the
    code damage a little of every block rather than all of one.
    """
    _, ecc_per_block, layout = _VERSIONS[version]
    blocks, at = [], 0
    for count, each in layout:
        for _ in range(count):
            blocks.append(words[at:at + each])
            at += each
    corrections = [_remainder(block, ecc_per_block) for block in blocks]

    out = []
    for index in range(max(len(block) for block in blocks)):
        for block in blocks:
            if index < len(block):
                out.append(block[index])
    for index in range(ecc_per_block):
        for correction in corrections:
            out.append(correction[index])
    return out


# --- the grid ---------------------------------------------------------------

def _blank(size: int) -> list:
    return [[None] * size for _ in range(size)]


def _place_finder(grid: list, row: int, col: int) -> None:
    size = len(grid)
    for i in range(-1, 8):
        for j in range(-1, 8):
            r, c = row + i, col + j
            if not (0 <= r < size and 0 <= c < size):
                continue
            ring = (0 <= i <= 6 and j in (0, 6)) or (0 <= j <= 6 and i in (0, 6))
            middle = 2 <= i <= 4 and 2 <= j <= 4
            grid[r][c] = 1 if (ring or middle) else 0


def _functions(version: int) -> list:
    """The grid with everything that is not data already on it."""
    size = 17 + 4 * version
    grid = _blank(size)

    _place_finder(grid, 0, 0)
    _place_finder(grid, 0, size - 7)
    _place_finder(grid, size - 7, 0)

    for i in range(8, size - 8):                          # timing
        grid[6][i] = grid[i][6] = 1 if i % 2 == 0 else 0

    centres = _ALIGNMENT[version]
    for row in centres:
        for col in centres:
            if (row, col) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue                                  # under a finder
            for i in range(-2, 3):
                for j in range(-2, 3):
                    grid[row + i][col + j] = (
                        1 if max(abs(i), abs(j)) != 1 else 0)

    for i in range(9):                                    # format information
        if grid[8][i] is None:
            grid[8][i] = 0
        if grid[i][8] is None:
            grid[i][8] = 0
    for i in range(8):                                    # its second copy:
        grid[8][size - 1 - i] = 0                         # eight along row 8
    for i in range(7):
        grid[size - 1 - i][8] = 0                         # and seven up column 8

    # Last, because it sits one module above that column of seven and the
    # reservation above would otherwise put it out again.
    grid[size - 8][8] = 1                                 # the dark module

    if version >= 7:
        bits = _version_bits(version)
        for i in range(18):
            bit = (bits >> i) & 1
            grid[i // 3][size - 11 + i % 3] = bit
            grid[size - 11 + i % 3][i // 3] = bit
    return grid


def _place_data(grid: list, stream: list) -> None:
    """The zigzag: two columns at a time, upwards then downwards, from the right."""
    size = len(grid)
    bit = iter(stream)
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:                                      # the timing column
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if grid[row][c] is None:
                    grid[row][c] = next(bit, 0)
        upward = not upward
        col -= 2


def _mask(pattern: int, row: int, col: int) -> bool:
    if pattern == 0:
        return (row + col) % 2 == 0
    if pattern == 1:
        return row % 2 == 0
    if pattern == 2:
        return col % 3 == 0
    if pattern == 3:
        return (row + col) % 3 == 0
    if pattern == 4:
        return (row // 2 + col // 3) % 2 == 0
    if pattern == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if pattern == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


def _penalty(grid: list) -> int:
    """How bad a masked grid looks to a scanner. Lower is better."""
    size = len(grid)
    score = 0

    for line in list(grid) + [list(column) for column in zip(*grid)]:
        run, last = 0, None
        for module in line:
            if module == last:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run, last = 1, module
        if run >= 5:
            score += 3 + run - 5

    for row in range(size - 1):
        for col in range(size - 1):
            block = (grid[row][col], grid[row][col + 1],
                     grid[row + 1][col], grid[row + 1][col + 1])
            if len(set(block)) == 1:
                score += 3

    # The 1:1:3:1:1 ratio a scanner reads as a finder, wherever it is *not* a
    # finder. What makes it one is four light modules on one side or the other,
    # and the edge of the symbol counts as light - the quiet zone is out there.
    # Reading only for the eleven-module window misses every occurrence that
    # sits against an edge, which is a third of them, and a mask chosen on that
    # count is a symbol a camera has to be talked into reading.
    for line in list(grid) + [list(column) for column in zip(*grid)]:
        padded = [0] * 4 + list(line) + [0] * 4
        i = 0
        while i <= size - 7:
            if line[i:i + 7] != _RATIO:
                i += 1
            elif not any(padded[i:i + 4]) or not any(padded[i + 11:i + 15]):
                score += 40
                i += 7
            else:
                i += 4

    dark = sum(sum(row) for row in grid)
    score += 10 * int(abs(dark * 100 / (size * size) - 50) / 5)
    return score


def _format_bits(pattern: int) -> int:
    """The fifteen bits that say which mask and which level, BCH and all."""
    value = (_ECC_LEVEL_BITS << 3) | pattern
    remainder = value << 10
    while remainder.bit_length() >= 11:
        remainder ^= 0b10100110111 << (remainder.bit_length() - 11)
    return ((value << 10) | remainder) ^ 0b101010000010010


def _version_bits(version: int) -> int:
    remainder = version << 12
    while remainder.bit_length() >= 13:
        remainder ^= 0b1111100100101 << (remainder.bit_length() - 13)
    return (version << 12) | remainder


def _format_positions(size: int) -> tuple:
    """Where the fifteen format modules go, in bit order, for both copies.

    Written out rather than computed from a rule, because there is no rule:
    the sequence steps over the timing module at (8, 6) and the dark module at
    (size - 8, 8), and the two copies do not even start at the same end. Every
    attempt to be clever here is a symbol that scans as nothing at all.

    The first bit of each list is the **most significant** bit of the format
    string.
    """
    first = ([(8, col) for col in range(6)] + [(8, 7), (8, 8), (7, 8)]
             + [(row, 8) for row in range(5, -1, -1)])
    second = ([(size - 1 - i, 8) for i in range(7)]
              + [(8, size - 8 + i) for i in range(8)])
    return first, second


def _place_format(grid: list, pattern: int) -> None:
    bits = _format_bits(pattern)
    first, second = _format_positions(len(grid))
    for index, (a, b) in enumerate(zip(first, second)):
        bit = (bits >> (14 - index)) & 1
        grid[a[0]][a[1]] = bit
        grid[b[0]][b[1]] = bit


def encode(text: str) -> list:
    """`text` as a grid of 0 and 1, without its quiet zone.

    Raises `TooMuch` when it will not fit, which the caller answers by
    printing the link instead - a QR nobody can read is not an improvement on
    a URL somebody can copy.
    """
    payload = text.encode("utf-8")
    version = _pick_version(len(payload))
    stream = []
    for word in _interleave(_codewords(payload, version), version):
        stream += [(word >> shift) & 1 for shift in range(7, -1, -1)]

    functions = _functions(version)
    grid = [row[:] for row in functions]
    _place_data(grid, stream)

    best, best_score = None, None
    for pattern in range(8):
        candidate = [row[:] for row in grid]
        for row in range(len(candidate)):
            for col in range(len(candidate)):
                if functions[row][col] is None and _mask(pattern, row, col):
                    candidate[row][col] ^= 1
        _place_format(candidate, pattern)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best


# --- what it looks like -----------------------------------------------------

BLACK = "\x1b[38;2;0;0;0m"
WHITE_BG = "\x1b[48;2;255;255;255m"
RESET = "\x1b[0m"


def render(text: str, quiet: int = 4, colour: bool = True, indent: str = "  ") -> str:
    """The code as lines to print. Two rows of modules per line.

    `colour` paints the light modules white and the dark ones black, which is
    the way round a scanner expects however the terminal is themed. Without it
    the blocks take the terminal's own colours, which on a dark theme is an
    inverted code - readable by most phones, and worth saying so rather than
    pretending otherwise.
    """
    grid = encode(text)
    size = len(grid)
    width = size + 2 * quiet
    rows = ([[0] * width] * quiet
            + [[0] * quiet + row + [0] * quiet for row in grid]
            + [[0] * width] * quiet)
    if len(rows) % 2:
        rows.append([0] * width)

    lines = []
    for top, bottom in zip(rows[::2], rows[1::2]):
        out = []
        for upper, lower in zip(top, bottom):
            # The half block paints the *foreground* on top. With a black
            # foreground on a white background, a dark module is where the ink
            # is - which is the way a scanner reads it.
            out.append("█" if upper and lower else
                       "▀" if upper else
                       "▄" if lower else " ")
        body = "".join(out)
        lines.append(f"{indent}{WHITE_BG}{BLACK}{body}{RESET}" if colour
                     else indent + body)
    return "\n".join(lines)


def fits(text: str) -> bool:
    """Would `text` encode at all? Asked before offering to draw it."""
    try:
        _pick_version(len(text.encode("utf-8")))
        return True
    except TooMuch:
        return False
