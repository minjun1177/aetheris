# A Real Turn

[← README](../README.md)

One unedited run of `aetheris -p` against a local `gemma4:e4b` on Ollama, recorded on
2026-11-12. The project is two files: a `stats.py` with two bugs (`mean` adds 1,
`median` ignores even-length input) and a `test_stats.py` that catches both.

```bash
# 1. The prompt, run in the project directory; --verbose draws the turn on stderr
aetheris -p "test_stats.py fails. Fix stats.py so the tests pass." --auto-allow --verbose
```

---

## What was drawn (stderr)

Exactly as printed, in the plain-text form `--verbose` uses when stderr is a file.
Worth noticing: the first search pattern was wrong and said so; every edit names its
line by `number:hash`; an edit that named a line past the end of the file was refused
with the reason; each successful edit is its own commit; and auto-verify ran the
project's tests after each one, feeding the first failure back.

```text

  ─ tokens: 7984 in · 280 out · 8264 total · 6.8s · TPS: 66.2


  ▸ find_files
  ╰─ pattern: stats.py, test_stats.py
  │
  │  [Found] No file matches 'stats.py, test_stats.py'. A pattern without a / matches f
  │  ile names anywhere ("*.py"); one with a / matches the path.
  │
  ╰─ done

  ─ tokens: 8079 in · 312 out · 8391 total · 4.7s · TPS: 70.2
  ─ this turn: 2 requests · 16,655 tokens


  ▸ find_files
  ╰─ pattern: *.py
  │
  │  [Found] 2 file(s) match '*.py':
  │  stats.py
  │  test_stats.py
  │
  ╰─ done

  ─ tokens: 8151 in · 135 out · 8286 total · 2.0s · TPS: 73.1
  ─ this turn: 3 requests · 24,941 tokens


  ▸ read_file
  ╰─ filepath: stats.py
  │
  │  1:3de|def mean(values):
  │  2:b7e|    return sum(values) / len(values) + 1
  │  3:d41|
  │  4:d41|
  │  5:a61|def median(values):
  │  6:503|    ordered = sorted(values)
  │  7:87d|    middle = len(ordered) // 2
  │  8:fb0|    return ordered[middle]
  │  9:d41|
  │
  ╰─ done

  ▸ read_file
  ╰─ filepath: test_stats.py
  │
  │  1:2ae|from stats import mean, median
  │  2:d41|
  │  3:d41|
  │  4:edc|def test_mean():
  │  5:953|    assert mean([2, 4, 6]) == 4
  │  6:d41|
  │  7:d41|
  │  8:00a|def test_median_even():
  │  9:a12|    assert median([1, 2, 3, 4]) == 2.5
  │  10:d41|
  │
  ╰─ done

  ─ tokens: 8479 in · 1262 out · 9741 total · 18.0s · TPS: 71.7
  ─ this turn: 4 requests · 34,682 tokens


  ▸ edit_file
  ├─ filepath: stats.py
  ╰─ new_content: 2:b7e| return sum(values) / len(values)
  ⎇ committed d7dd679  /undo to take it back
  │
  │  [Success] File edited: stats.py (line 2 replaced). One line became one line, so no
  │  thing below moved and the rest of your anchors are still good.
  │  The file now reads, around what you changed:
  │    1:3de|def mean(values):
  │    2:27c|    return sum(values) / len(values)
  │    3:d41|
  │    4:d41|
  │    5:a61|def median(values):
  │    6:503|    ordered = sorted(values)
  │    7:87d|    middle = len(ordered) // 2
  │
  ╰─ done
  ⟳ auto-verify: python -m pytest -x -q -l  in demo
  ✗ python -m pytest -x -q -l failed  0.3s

  ─ tokens: 8970 in · 1451 out · 10421 total · 20.5s · TPS: 72.5
  ─ this turn: 5 requests · 45,103 tokens


  ▸ edit_file
  ├─ filepath: stats.py
  ╰─ new_content: 5:a61|def median(values): 6:503| ordered = sorted(values) 7:87d| n = 
                  len(ordered) 8:fb0| if n % 2 == 1: 9:d4...
     cut to fit - Ctrl+O at the prompt shows it whole
  │
  │  [Error] edit_file(filepath='stats.py', new_content='5:a61|def median(values): 6:50
  │  3| ordered = sorted(values) 7:...'): There is no line 10 in stats.py - it has 9 li
  │  nes. read_file it again and take the anchors from that listing.
  │
  ╰─ error

  ─ tokens: 9218 in · 155 out · 9373 total · 2.4s · TPS: 72.7
  ─ this turn: 6 requests · 54,476 tokens


  ▸ read_file
  ╰─ filepath: stats.py
  │
  │  1:3de|def mean(values):
  │  2:27c|    return sum(values) / len(values)
  │  3:d41|
  │  4:d41|
  │  5:a61|def median(values):
  │  6:503|    ordered = sorted(values)
  │  7:87d|    middle = len(ordered) // 2
  │  8:fb0|    return ordered[middle]
  │  9:d41|
  │
  ╰─ done

  ─ tokens: 9373 in · 1170 out · 10543 total · 16.2s · TPS: 72.8
  ─ this turn: 7 requests · 65,019 tokens


  ▸ edit_file
  ├─ filepath: stats.py
  ├─ new_content: n = len(ordered) if n % 2 == 1: return ordered[n // 2] else: return (
  │               ordered[n // 2 - 1] ...
  ╰─ old_content: 7:87d 8:fb0
     cut to fit - Ctrl+O at the prompt shows it whole
  ⎇ committed b4ceaff  /undo to take it back
  │
  │  [Success] File edited: stats.py (lines 7-8 (2 lines) replaced). Every line below i
  │  t has moved.
  │  The file now reads, around what you changed:
  │    2:27c|    return sum(values) / len(values)
  │    3:d41|
  │    4:d41|
  │    5:a61|def median(values):
  │    6:503|    ordered = sorted(values)
  │    7:c8a|    n = len(ordered)
  │    8:c6f|    if n % 2 == 1:
  │    9:55b|        return ordered[n // 2]
  │    10:efd|    else:
  │    11:da6|        return (ordered[n // 2 - 1] + ordered[n // 2]) / 2
  │    12:d41|
  │  Anchors outside this listing have moved with it; read_file for those.
  │
  ╰─ done
  ⟳ auto-verify: python -m pytest -x -q -l  in demo
  ✓ python -m pytest -x -q -l passed  0.3s
  I have made the following changes to stats.py to ensure all tests pass:
  
  1.  mean(values): I corrected the calculation to remove an extra + 1 that was
     incorrectly added to the result.
  2.  median(values): I updated the logic to accurately calculate the median. It
     now checks if the length of the values is even or odd:
      •   If the length is odd, it returns the single middle element.
      •   If the length is even, it returns the average of the two middle elements.
  
  The self-verification check confirms that the tests in test_stats.py now pass
  successfully.

  ─ tokens: 9761 in · 504 out · 10265 total · 7.6s · TPS: 69.4
  ─ this turn: 8 requests · 75,284 tokens
```

---

## What was printed (stdout)

```text
I have made the following changes to `stats.py` to ensure all tests pass:

1.  **`mean(values)`:** I corrected the calculation to remove an extra `+ 1` that was incorrectly added to the result.
2.  **`median(values)`:** I updated the logic to accurately calculate the median. It now checks if the length of the values is even or odd:
    *   If the length is **odd**, it returns the single middle element.
    *   If the length is **even**, it returns the average of the two middle elements.

The self-verification check confirms that the tests in `test_stats.py` now pass successfully.
```

Afterwards: `pytest` reports `2 passed`, and `git log` shows the two edits as
`ai(edit_file): stats.py` commits, each one `/undo` away.
