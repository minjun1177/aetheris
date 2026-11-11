"""A file that is not UTF-8 is read, edited and written back as what it is.

Korean source and notes saved by older Windows editors are CP949, and read as
UTF-8 they were "Cannot read file" and nothing else - the model could not even
look at them. Reading them is half of it: an edit that wrote such a file back
as UTF-8 would change every Korean line in it, and the user's editor would show
mojibake. What is checked: a CP949 file reads under the same anchors its text
has, says what it is, is edited and replaced in CP949, a character CP949 cannot
hold is refused with nothing written, and a file that is neither is still
refused rather than read as noise.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AETHERIS_HOME"] = tempfile.mkdtemp(prefix="encoding-home-")

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.CHANNEL_ENABLED = False
config.GIT_AUTO_COMMIT = False
config.AUTO_ALLOW = True

from aetheris import tools           # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra and not ok else ''}")


def raw(path):
    with open(path, "rb") as handle:
        return handle.read()


folder = tempfile.mkdtemp(prefix="encoding-")
korean = "# 인사말\nprint('안녕하세요')\nprint('끝')\n"

print("--- a CP949 file ---")
path = os.path.join(folder, "hello.py")
with open(path, "wb") as handle:
    handle.write(korean.encode("cp949"))
shown = tools.handle_read_file(path)
check("is read, not refused", "안녕하세요" in shown and "[Error]" not in shown, shown)
check("under the anchors its text has",
      shown.split("\n")[1] == f"2:{tools._line_hash(korean.split(chr(10))[1])}|print('안녕하세요')",
      shown.split("\n")[1])
check("and says what it is", shown.rstrip().endswith("Edits keep it cp949.]"), shown[-80:])

anchor = shown.split("\n")[2].split("|")[0]
result = tools.handle_edit_file(path, "", f"{anchor}|print('잘 가요')")
check("an edit from those anchors lands", "[Error]" not in result, result)
check("and the file stays CP949",
      raw(path) == "# 인사말\nprint('안녕하세요')\nprint('잘 가요')\n".encode("cp949"),
      repr(raw(path)))

result = tools.handle_edit_file(path, "print('잘 가요')", "print('bye 👋')")
check("a character CP949 cannot hold is refused", "cp949" in result
      and "Nothing was written" in result, result)
check("with nothing written", b"bye" not in raw(path))

tools.handle_write_file(path, "# 새 파일\nprint('다시')\n")
check("write_file over it keeps it CP949 too",
      raw(path) == "# 새 파일\nprint('다시')\n".encode("cp949"), repr(raw(path)))

print("\n--- CRLF and UTF-8 are as they were ---")
crlf = os.path.join(folder, "crlf.txt")
with open(crlf, "wb") as handle:
    handle.write("하나\r\n둘\r\n".encode("cp949"))
shown = tools.handle_read_file(crlf)
check("CRLF reads as lines, like any other file", shown.split("\n")[1].endswith("|둘"), shown)
anchor = shown.split("\n")[1].split("|")[0]
tools.handle_edit_file(crlf, "", f"{anchor}|셋")
check("and is written back CRLF and CP949", raw(crlf) == "하나\r\n셋\r\n".encode("cp949"),
      repr(raw(crlf)))

utf8 = os.path.join(folder, "utf8.py")
with open(utf8, "wb") as handle:
    handle.write(korean.encode("utf-8"))
shown = tools.handle_read_file(utf8)
check("a UTF-8 file says nothing about its encoding", "UTF-8" not in shown, shown)
tools.handle_edit_file(utf8, "print('끝')", "print('👋')")
check("and takes any character", raw(utf8).endswith("print('👋')\n".encode("utf-8")),
      repr(raw(utf8)))
new = os.path.join(folder, "new.py")
tools.handle_write_file(new, "print('새로')\n")
check("a new file is UTF-8", raw(new) == "print('새로')\n".encode("utf-8"), repr(raw(new)))

print("\n--- neither ---")
noise = os.path.join(folder, "noise.bin.txt")
with open(noise, "wb") as handle:
    handle.write(b"\xff\xfe\x00\xff abc \x80\x80\x80")
shown = tools.handle_read_file(noise)
check("a file that is neither is still refused", shown.startswith("[Error] Cannot read file"),
      shown[:120])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("encoding checks passed")
