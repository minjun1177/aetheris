"""An image reaching a model that can see it, and not reaching one that cannot.

Vision is the one capability here that fails *quietly* when it is missing. A
hosted API rejects an image it cannot take and says so; Ollama drops it on the
way in, and the model then answers about the sentence alone - fluently, and
about a picture it was never shown. So most of what is checked below is about
refusing early and saying why, rather than about the encoding.

What the checks are actually protecting:

* the four wire formats. Each provider wants images somewhere different, and
  `media_type` has to travel with the bytes - a resized GIF that leaves here
  still labelled `image/gif` arrives as garbage.
* `content` stays a string. Invariant 5.3 wants the stored history plain text,
  so an image rides beside it as a path; `merge_runs` must not throw the
  attachment away when a tool result lands on top of it, and a session must not
  carry base64 around forever.
* a model that cannot see is told before the request, not after.
* `@shot.png` is not read as text. A PNG read as UTF-8 is a screenful of
  nothing that costs a thousand tokens to say it.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="images-home-")
os.environ["AETHERIS_HOME"] = HOME

from aetheris import config          # noqa: E402
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False

from aetheris import images          # noqa: E402
from aetheris import mentions        # noqa: E402
from aetheris import providers       # noqa: E402
from aetheris import toolspec        # noqa: E402

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


WORK = tempfile.mkdtemp(prefix="images-work-")
started_in = os.getcwd()


def picture(name, size=(40, 30), colour=(200, 30, 30), **save):
    path = os.path.join(WORK, name)
    from PIL import Image
    Image.new("RGB", size, colour).save(path, **save)
    return path


try:
    check("Pillow is installed, so resizing is on", images.PILLOW_AVAILABLE)
    if not images.PILLOW_AVAILABLE:
        raise SystemExit("cannot test images without Pillow")

    os.chdir(WORK)
    png = picture("shot.png")
    jpg = picture("photo.jpg", size=(60, 40))
    gif = picture("anim.gif")
    text = os.path.join(WORK, "notes.txt")
    with open(text, "w", encoding="utf-8") as f:
        f.write("not a picture\n")

    # ----------------------------------------------------------------------
    print("--- what counts as an image ---")
    check("a png is one", images.media_type(png) == "image/png")
    check("a jpg is one", images.media_type(jpg) == "image/jpeg")
    check("a gif is one", images.media_type(gif) == "image/gif")
    check("a text file is not", images.media_type(text) == "", images.media_type(text))
    # The extension answers before the file is opened, so a path that is not
    # there still reads as an image. That is deliberate: `encode` is what
    # reports a missing file, and it reports it as missing rather than as
    # "not an image", which is the sentence a person can act on.
    check("the extension decides before the file is even opened",
          images.media_type(os.path.join(WORK, "gone.png")) == "image/png")

    nameless = os.path.join(WORK, "screenshot")
    shutil.copyfile(png, nameless)
    check("a file with no extension is read by its first bytes",
          images.media_type(nameless) == "image/png")

    # ----------------------------------------------------------------------
    print("\n--- encoding, and what it says it did ---")
    data, kind, note = images.encode(png)
    check("a small image encodes", bool(data) and kind == "image/png", kind)
    check("with nothing to report about it", note == "", note)
    import base64
    check("and the bytes are the file's own",
          base64.b64decode(data) == open(png, "rb").read())

    data, kind, note = images.encode(text)
    check("a text file is refused", not data and "not an image" in note, note)
    data, kind, note = images.encode(os.path.join(WORK, "gone.png"))
    check("and so is a file that is not there", not data and "could not read" in note, note)

    empty = os.path.join(WORK, "empty.png")
    open(empty, "wb").close()
    data, _k, note = images.encode(empty)
    check("an empty file is refused by name", not data and "is empty" in note, note)

    # ----------------------------------------------------------------------
    print("\n--- a big image is made smaller, not refused ---")
    big = picture("big.jpg", size=(4000, 3000), quality=95)
    before = os.path.getsize(big)
    data, kind, note = images.encode(big)
    check("it still encodes", bool(data), note)
    check("having been resized on the way", "->" in note, note)
    check("to the long-edge cap",
          f"{config.IMAGE_MAX_EDGE}x" in note or f"x{config.IMAGE_MAX_EDGE}" in note, note)
    check("and it is smaller than it was", len(base64.b64decode(data)) < before,
          f"{before} -> {len(base64.b64decode(data))}")
    check("a jpeg stays a jpeg through the resize", kind == "image/jpeg", kind)

    wide_gif = picture("wide.gif", size=(3000, 200))
    _d, kind, note = images.encode(wide_gif)
    check("a resized gif is relabelled as what it actually became",
          kind == "image/png" and note, f"{kind} {note}")

    # ----------------------------------------------------------------------
    print("\n--- an image travels beside the text, never inside it ---")
    message = {"role": "user", "content": "what is wrong here?", "images": [png]}
    check("the paths come back off the message",
          providers.carried_images(message) == [png])
    check("a message with none says so", providers.carried_images(
        {"role": "user", "content": "hello"}) == [])
    check("and a conversation knows whether it carries any",
          providers.has_images([message]) and not providers.has_images(
              [{"role": "user", "content": "hi"}]))

    many = {"role": "user", "content": "x", "images": [png] * 9}
    check("more than the cap are not all sent",
          len(providers.carried_images(many)) == config.IMAGE_MAX_PER_MESSAGE,
          str(len(providers.carried_images(many))))

    conversation = [message, {"role": "user", "content": "[Tool Result for 'x']: ok"}]
    merged = providers.merge_runs_with_images(conversation)
    check("merging two user messages keeps the image", len(merged) == 1
          and merged[0].get("images") == [png], str(merged[0].get("images")))
    check("and still joins the text the way merge_runs does",
          "what is wrong here?" in merged[0]["content"]
          and "Tool Result" in merged[0]["content"])
    check("a merged run with no image grows no empty key",
          "images" not in providers.merge_runs_with_images(
              [{"role": "user", "content": "a"}])[0])

    # ----------------------------------------------------------------------
    print("\n--- each provider is handed the shape it asks for ---")
    ollama_shaped = providers._ollama_messages([message])[0]
    check("ollama gets base64 under the key it already uses",
          ollama_shaped["images"] and not ollama_shaped["images"][0].startswith("/"),
          ollama_shaped["images"][0][:12])
    check("and its content is still the plain string",
          ollama_shaped["content"] == "what is wrong here?")
    check("a conversation with no images is handed back untouched",
          providers._ollama_messages([{"role": "user", "content": "hi"}])
          == [{"role": "user", "content": "hi"}])

    blocks = providers._anthropic_messages([message])[0]["content"]
    check("anthropic gets blocks, image first then the text",
          [b["type"] for b in blocks] == ["image", "text"], str([b["type"] for b in blocks]))
    check("with the media type beside the data",
          blocks[0]["source"]["media_type"] == "image/png"
          and blocks[0]["source"]["type"] == "base64")
    marked = providers._cache_tail(providers._anthropic_messages([message]))
    check("and the cache breakpoint lands on the text, not the picture",
          "cache_control" in marked[0]["content"][-1]
          and marked[0]["content"][-1]["type"] == "text")
    check("a message without an image keeps its string content",
          isinstance(providers._anthropic_messages(
              [{"role": "user", "content": "hi"}])[0]["content"], str))

    parts = providers._openai_messages([message])[0]["content"]
    check("openai gets a data: URL carrying the media type",
          any(p.get("type") == "image_url"
              and p["image_url"]["url"].startswith("data:image/png;base64,")
              for p in parts), str(parts)[:60])
    check("openai is handed plain strings when nothing has an image",
          providers._openai_messages([{"role": "user", "content": "hi"}])
          == [{"role": "user", "content": "hi"}])

    gemini = providers._gemini_parts(message)
    check("gemini gets inline_data then the text",
          "inline_data" in gemini[0] and "text" in gemini[-1], str(gemini)[:50])
    check("with its own spelling of the media type",
          gemini[0]["inline_data"]["mime_type"] == "image/png")

    gone = {"role": "user", "content": "x", "images": [os.path.join(WORK, "vanished.png")]}
    check("an image that has since been deleted is skipped, not fatal",
          providers._ollama_messages([gone])[0].get("images") is None)
    check("and the message it was on still goes",
          providers._openai_messages([gone])[0]["content"] == "x")

    # ----------------------------------------------------------------------
    print("\n--- a model that cannot see is not sent one ---")
    hosted = providers.build("anthropic")
    check("the hosted providers are assumed able", hosted.sees_images())
    local = providers.build("ollama")
    local.settings["model"] = "definitely-not-installed:0b"
    providers._ollama_capability_lists[(local.host, local.model)] = ["completion"]
    check("a local model without vision says no", not local.sees_images())
    providers._ollama_capability_lists[(local.host, local.model)] = ["completion", "vision"]
    check("and one with it says yes", local.sees_images())

    # ----------------------------------------------------------------------
    print("\n--- @shot.png attaches a picture, not a wall of bytes ---")
    out, notes, pictures = mentions.expand("what is wrong with @shot.png ?")
    check("the image comes back as a path to hang on the message",
          pictures == [png] or pictures == ["shot.png"], str(pictures))
    check("nothing of it was pasted into the text", out.strip().endswith("?"), out[-40:])
    check("the mention stays in the sentence", "@shot.png" in out)
    check("and it is reported as an image", notes and notes[0][1]
          and "png" in notes[0][2], str(notes))

    out, notes, pictures = mentions.expand("read @notes.txt")
    check("a text file still arrives as text", not pictures
          and "not a picture" in out, str(pictures))

    out, notes, pictures = mentions.expand("@shot.png and @notes.txt together")
    check("one of each goes the way it should",
          len(pictures) == 1 and "not a picture" in out, str(pictures))

    # ----------------------------------------------------------------------
    print("\n--- the tool table, and read_file sending it on ---")
    check("view_image is a tool", toolspec.get("view_image") is not None)
    check("taking a path under any of its names",
          all(toolspec.get("view_image").bind({key: png})[0] == png
              for key in ("filepath", "path", "image", "file")))

    from aetheris import tools        # noqa: E402
    refused = tools.handle_read_file(png)
    check("read_file refuses an image rather than returning broken bytes",
          refused.startswith(config.TOOL_ERROR_PREFIX), refused[:60])
    check("and names the tool that would have worked", "view_image" in refused)
    check("while it still reads an ordinary text file",
          "not a picture" in tools.handle_read_file(text))

    check("view_image refuses a text file", tools.handle_view_image(text)
          .startswith(config.TOOL_ERROR_PREFIX))
    check("and a path that is not there",
          "No such file" in tools.handle_view_image(os.path.join(WORK, "nope.png")))

    # ----------------------------------------------------------------------
    print("\n--- what the model looked at rides on the next result ---")
    images.forget_pending()
    check("nothing is waiting to start with", images.take_pending() == [])
    images.want(png)
    images.want(png)
    check("asking twice for the same file queues it once",
          images.take_pending() == [png])
    check("and taking it empties the queue", images.take_pending() == [])
    images.want(png)
    images.forget_pending()
    check("a turn that died leaves nothing behind for the next one",
          images.take_pending() == [])

finally:
    os.chdir(started_in)
    shutil.rmtree(HOME, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("image checks passed")
