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
  The runtime does not read them: it asks the Tools modules in order for each
  (`Tools.attach(path)`, optional) and puts the first answer into the message. With no
  module that implements it, a file goes as a `File` by its path alone. A path that
  cannot be read raises ValueError and nothing is sent. The text may be empty.
  `MessageQueued` carries the attachments of a message sent while the agent works.
- **The built-in Tools read them** (`tools/shell_files/attachments.py`, in a thread): an
  image (PNG, JPEG, GIF, WebP, up to 5 MB) becomes an `Image`; a PDF (up to 20 MB and 600
  pages, read with pypdf) a `Document`; a Word, Excel or PowerPoint file (.docx, .xlsx,
  .pptx, up to 20 MB) a `File` whose text is the document converted
  (`shell_files/office.py`); a file that decodes as UTF-8 (up to 200 kB) a `File` with its
  content; anything else a `File` without. Attaching is the user's own act, so no
  permission is asked and the file may lie outside the work folder; a relative path is
  taken from the work folder.
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
- **A tool can return pictures and PDFs** with its text: `ToolOutput.attachments`, kept on
  the `ToolResult` block (`Image` and `Document`). Anthropic takes them inside the tool
  result. For the OpenAI-compatible Core a tool message is text, so a PDF goes as its
  text there and a picture in a user message right after the tool results (or as a line
  of text for a profile without vision). A result that arrives late, from a backgrounded
  call, is a text block in a user message and only names them.
- **The file tool** uses that (`read_file`): a picture is shown to the model; a PDF is
  read as the text of its pages under `[Page n]` marks, and with `view` goes as a
  document of the pages asked for, at most 20 per call, with that text; Word, Excel and PowerPoint files are converted to text and read by
  lines like any text file. This is how the agent looks at what it finds by itself, and
  it is an ordinary tool call, under the usual permissions.
- **Two ways in, on purpose,** as in Claude's apps: what the user drops goes straight into
  their message (`Tools.attach`: no model step, no permission prompt), and what the agent
  opens comes back as a tool result.
- **`shell_files/office.py`** converts those three with python-docx, openpyxl and
  python-pptx, for both the attachment and the file tool.
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
- **The runtime reads the files itself.** Built first (`runtime/attachments.py`, with the
  Office conversion in a module beside `api.py` so that the file tool could share it).
  It works, but the runtime then knows file formats and size limits, which is not
  orchestration, and a Tools module that works somewhere else (another machine, a
  container) could not read attachments its own way.
- **The frontend reads the files and sends their bytes.** The headless channel would
  carry megabytes of base64 per message, and every frontend would repeat the reading and
  the limits.
- **A PDF as its extracted text only.** Works with every provider and needs no block of
  its own, but loses what Claude reads from the pages themselves: tables, charts, scans.
  The text is kept in the block as the fallback.
- **One way in: a dropped file as a path the model opens with `read_file`.** The runtime
  would treat attachments like any tool call, with no `Tools.attach`. But every attachment
  would cost a model step, a file outside the work folder would ask for permission
  (unless attached paths were pre-approved, which is special treatment again), and the
  model might not open it.
- **`read_file` shows a PDF's pages by default,** as Claude Code's Read does. Built first;
  but most PDFs read fine as text at a fraction of the cost, so the pages are on request.
- **Tool results that stay text.** No contract change, but the agent could not look at a
  picture in a project or a scanned PDF it found.
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
- What can be attached depends on the Tools module: one without `attach` leaves every
  file a bare path.
- A file that is attached by path only is read by the agent's tools under the usual
  permissions, so one outside the work folder still asks.
- Checked with Opus 5.5: asked to open a picture and a PDF with its file tool, it described
  the picture and quoted page 2 of the PDF, for about 8 cents.
- A PDF read by the tool costs only its text unless the agent asks to view the pages,
  which costs what they cost as images; a PDF without text says to view it.
- A PDF is stored twice over in the archive: its bytes and its text.
- pypdf, python-docx, openpyxl and python-pptx are dependencies.
- An Office file reaches the model as text only: its formatting, pictures and charts do
  not, and a formula comes as its last saved value.
- Not built: the old binary formats (.doc, .xls, .ppt), resizing a picture that is too
  large, attaching from the
  terminal app, and a file picker button.
