# Agent Note: Attachments

Status: implemented — the user drops files into a message: images and PDFs the model sees, text files it reads

## Problem

A message was text only. To show the agent a screenshot the user had to describe it, and
to give it a file they typed its path and, when the file was outside the work folder,
answered a permission prompt. The owner wants to drop files into the chat, as in Claude's
apps (2026-11-12), with a small preview above the message box for images.

## Decision

- **Three blocks** in a user message (`api.py`): `Image(media_type, data, name)`, the
  picture itself, base64; `Document(path, data, text, pages)`, a PDF with the text of its
  pages; and `File(path, text)`, a text file with its content, or with `text` None when
  it is binary or too large to include. `api.attachment_text` is how each reads as text.
- **`RuntimeAPI.send(text, files=())`** takes the paths of the files the user attached.
  The runtime reads them in a thread (`runtime/attachments.py`): an image (PNG, JPEG, GIF,
  WebP, up to 5 MB) becomes an `Image`; a PDF (up to 20 MB and 600 pages, read with pypdf) a `Document`; a Word, Excel
  or PowerPoint file (.docx, .xlsx, .pptx, up to 20 MB) a `File` whose text is the
  document converted (`kayden.office`); a file that decodes as UTF-8 (up to 200 kB) a
  `File` with its content; anything else a `File` without. A path that cannot be read
  raises ValueError and nothing is sent. The text may be empty. `MessageQueued` carries
  the attachments of a message sent while the agent works.
- **Cores.** Anthropic sends an `Image` as a base64 image block, a `Document` as a
  base64 document block ahead of the message's text (the model reads its text and sees
  each page), and a `File` as text. Other providers get a `Document`'s extracted text. The
  OpenAI-compatible Core sends an image part, or `attachment_text` in its place for a
  profile whose models take no images (`Profile.images`, off for DeepSeek).
- **Memory** stores both through the generic codec, without a schema change. Image data is
  left out of the search index; a file's text is in it. Summaries and search hits show
  `attachment_text`, shortened like tool output.
- **The context budget** counts an image as about 1500 tokens (`budget._IMAGE_TOKENS`),
  and a PDF as its text plus that much per page.
- **The file tool** reads a PDF too (`read_file` on a `.pdf`, with an optional `pages`
  range): the text of its pages, as a tool result, with any provider. This is how the
  agent reads a PDF it finds by itself. It reads Word, Excel and PowerPoint files the same
  way, converted to text and then by lines like any text file.
- **`kayden.office`** converts those three with python-docx, openpyxl and python-pptx. It
  sits beside `api.py`, outside any module, because the runtime (attachments) and a Tools
  module (the file tool) both need it and neither may call the other.
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
- **A PDF as its extracted text only.** Works with every provider and needs no block of
  its own, but loses what Claude reads from the pages themselves: tables, charts, scans.
  The text is kept in the block as the fallback.
- **A file tool that returns the PDF's pages to the model as a document.** What Claude
  Code's own Read does, and it lets the agent choose pages; but a tool result here is
  text, so it would change the Tools contract. The tool returns text for now.
- **Office files through a skill**, the agent running a converter in its shell. No code
  and no dependencies, but it needs the converter installed and the sandbox to see the
  file, and a dropped file would still arrive as a bare path. The owner asked for them to
  work like the rest.
- **markitdown**, one library for many formats. It brings pandas for spreadsheets; the
  three libraries used are what it wraps, without that.
- **One `Attachment` block for both.** Cores and frontends would switch on the media type
  everywhere; two blocks say what each is.

## Consequences

- Checked with Opus 5.5: a dropped picture and a text file were both described correctly
  in one reply, for about 6 cents; a two-page PDF was answered from correctly, with its
  page count, for about 7 cents.
- **Cost.** An image is billed as tokens with every request of the conversation; the
  provider's cache softens it. A text file's content is in the transcript once, like any
  message.
- **The archive grows** by every attached image, kept in full as the archive is.
- **Models without vision** only learn that an image was attached.
- A file that is attached by path only is read by the agent's tools under the usual
  permissions, so one outside the work folder still asks.
- A PDF is stored twice over in the archive: its bytes and its text.
- pypdf, python-docx, openpyxl and python-pptx are dependencies.
- An Office file reaches the model as text only: its formatting, pictures and charts do
  not, and a formula comes as its last saved value.
- Not built: the old binary formats (.doc, .xls, .ppt), resizing a picture that is too
  large, attaching from the
  terminal app, and a file picker button.
