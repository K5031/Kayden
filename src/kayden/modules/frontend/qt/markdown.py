import html
import re


_BULLET = re.compile(r"^\s*[-+*]\s+(.+)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_HEADING = re.compile(r"^(#{1,4})\s+(.+)$")


def _plain_inline(text):
    escaped = html.escape(text)
    escaped = re.sub(
        r"\[([^]]+)]\((https?://[^ )]+)\)",
        r'<a href="\2" style="color:#8ab4f8;">\1</a>',
        escaped,
    )
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def _inline(text):
    rendered = []
    for part in re.split(r"(`[^`\n]*`)", text):
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            rendered.append(
                '<span style="font-family:monospace; font-size:13px; '
                'color:#d4d4d4;">'
                f"{html.escape(part[1:-1])}</span>"
            )
        else:
            rendered.append(_plain_inline(part))
    return "".join(rendered)


def render_markdown(source):
    """Render Kayden's conversational Markdown with stable Qt-friendly styles."""
    lines = source.splitlines()
    output = [
        '<div style="color:#ececec; font-family:sans-serif; font-size:15px;">'
    ]
    paragraph = []
    list_kind = None
    code_lines = None

    def close_paragraph():
        if paragraph:
            output.append(
                '<p style="margin:0 0 8px 0;">'
                + "<br>".join(_inline(line) for line in paragraph)
                + "</p>"
            )
            paragraph.clear()

    def close_list():
        nonlocal list_kind
        if list_kind:
            output.append(f"</{list_kind}>")
            list_kind = None

    for line in lines:
        if code_lines is not None:
            if line.strip().startswith("```"):
                output.append(
                    '<pre style="margin:6px 0 10px 0; padding:10px; '
                    'background-color:#111111; color:#ececec; '
                    'font-family:monospace; font-size:13px;">'
                    + html.escape("\n".join(code_lines))
                    + "</pre>"
                )
                code_lines = None
            else:
                code_lines.append(line)
            continue

        if line.strip().startswith("```"):
            close_paragraph()
            close_list()
            code_lines = []
            continue

        if not line.strip():
            close_paragraph()
            close_list()
            continue

        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if heading:
            close_paragraph()
            close_list()
            size = {1: 22, 2: 19, 3: 17, 4: 15}[len(heading.group(1))]
            output.append(
                f'<p style="margin:6px 0 8px 0; font-size:{size}px; '
                f'font-weight:600;">{_inline(heading.group(2))}</p>'
            )
        elif bullet or numbered:
            close_paragraph()
            required_kind = "ul" if bullet else "ol"
            if list_kind != required_kind:
                close_list()
                list_kind = required_kind
                output.append(
                    f'<{list_kind} style="margin:3px 0 8px 18px; padding:0;">'
                )
            content = (bullet or numbered).group(1)
            output.append(
                f'<li style="margin:1px 0;">{_inline(content)}</li>'
            )
        elif line.lstrip().startswith(">"):
            close_paragraph()
            close_list()
            content = line.lstrip()[1:].lstrip()
            output.append(
                '<p style="margin:4px 0 8px 8px; color:#b3b3b3;">'
                f"{_inline(content)}</p>"
            )
        else:
            close_list()
            paragraph.append(line)

    close_paragraph()
    close_list()
    if code_lines is not None:
        output.append(
            '<pre style="margin:6px 0 10px 0; padding:10px; '
            'background-color:#111111; color:#ececec; '
            'font-family:monospace; font-size:13px;">'
            + html.escape("\n".join(code_lines))
            + "</pre>"
        )
    output.append("</div>")
    return "".join(output)
