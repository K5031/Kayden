# Agent Note: Attachments

Status: implemented — the user drops files into a message: images the model sees, text files it reads

## Problem

A message was text only. To show the agent a screenshot the user had to describe it, and
to give it a file they typed its path and, when the file was outside the work folder,
answered a permission prompt. The owner wants to drop files into the chat, as in Claude's
apps (2026-11-12), with a small preview above the message box for images.

## Decision

- **Two blocks** in a user message (`api.py`): `Image(media_type, data, name)`, the
  picture itself, base64; and `File(path, text)`, a text file with its content, or with
  `text` None when it is binary or too large to include. `api.attachment_text` is how
  either reads as text.
- **`RuntimeAPI.send(text, files=())`** takes the paths of the files the user attached.
  The runtime reads them in a thread (`runtime/attachments.py`): an image (PNG, JPEG, GIF,
  WebP, up to 5 MB) becomes an `Image`; a file that decodes as UTF-8 (up to 200 kB) a
  `File` with its content; anything else a `File` without. A path that cannot be read
  raises ValueError and nothing is sent. The text may be empty. `MessageQueued` carries
  the attachments of a message sent while the agent works.
- **Cores.** Anthropic sends an `Image` as a base64 image block and a `File` as text. The
  OpenAI-compatible Core sends an image part, or `attachment_text` in its place for a
  profile whose models take no images (`Profile.images`, off for DeepSeek).
- **Memory** stores both through the generic codec, without a schema change. Image data is
  left out of the search index; a file's text is in it. Summaries and search hits show
  `attachment_text`, shortened like tool output.
- **The context budget** counts an image as about 1500 tokens (`budget._IMAGE_TOKENS`).
- **The desktop app** takes files dropped anywhere on the window or pasted into the
  message box, and a picture pasted from the clipboard, which it saves to a temporary
  folder first. They wait in a strip above the text (`AttachmentStrip`): a 56 px preview
  for what Qt can load as a picture, the name otherwise, each with a ✕. A sent message
  shows its pictures (at most 260 px) and its files' names.
- **The terminal app** names what was attached (`common.with_attachments`); it cannot
  attach.

## Alternatives considered

- **Send only the path and let the agent open it.** No contract change, but the model
  cannot look at an image through a file tool, a file outside the work folder asks for
  permission, and a sandboxed shell cannot see it at all.
- **A `Tools.attach` grant** that makes reading a dropped file count as confined. Keeps
  the content out of the transcript, but ties the feature to one Tools module's file tool
  and still does not show images.
- **The frontend reads the files and sends their bytes.** The headless channel would
  carry megabytes of base64 per message, and every frontend would repeat the reading and
  the limits.
- **One `Attachment` block for both.** Cores and frontends would switch on the media type
  everywhere; two blocks say what each is.

## Consequences

- Checked with Opus 5.5: a dropped picture and a text file were both described correctly
  in one reply, for about 6 cents.
- **Cost.** An image is billed as tokens with every request of the conversation; the
  provider's cache softens it. A text file's content is in the transcript once, like any
  message.
- **The archive grows** by every attached image, kept in full as the archive is.
- **Models without vision** only learn that an image was attached.
- A file that is attached by path only is read by the agent's tools under the usual
  permissions, so one outside the work folder still asks.
- Not built: PDFs as documents, resizing a picture that is too large, attaching from the
  terminal app, and a file picker button.
