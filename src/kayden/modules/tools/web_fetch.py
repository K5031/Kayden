import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

import requests

from kayden.interfaces import ToolInterface

MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 50_000
READER_URL = "https://r.jina.ai/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Kayden/0.1"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,"
        "text/plain;q=0.8,*/*;q=0.5"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache",
}


class _UseReaderFallback(Exception):
    pass


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.focused_parts = []
        self._ignored_depth = 0
        self._focused_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        elif tag in {"article", "main"}:
            self._focused_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in {"article", "main"} and self._focused_depth:
            self._focused_depth -= 1

    def handle_data(self, data):
        if not self._ignored_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)
                if self._focused_depth:
                    self.focused_parts.append(text)

    def text(self) -> str:
        focused = "\n".join(self.focused_parts)
        return focused if len(focused) >= 200 else "\n".join(self.parts)


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https")
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or default_port)
    except socket.gaierror as error:
        raise ValueError("URL hostname could not be resolved") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("private or local URLs are not accessible")


class Module(ToolInterface):
    def __init__(self):
        self._session = requests.Session()

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "fetch_webpage",
                    "description": (
                        "Fetch readable text from a specific public HTTP or HTTPS page. "
                        "Prefer article URLs over large site homepages. If a site blocks "
                        "direct access or requires JavaScript, a public reader service may "
                        "be used as a fallback."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def describe_activity(self, name, arguments, state, result=None):
        url = arguments.get("url")
        host = urlparse(url).hostname if isinstance(url, str) else None
        target = host or "the page"
        if state == "running":
            return f"Reading {target}"
        if state == "failed":
            return f"Couldn’t read {target}"
        return f"Read {target}"

    def execute(self, name: str, arguments: dict) -> str:
        if name != "fetch_webpage":
            raise ValueError(f"Unknown tool: {name}")
        if not set(arguments).issubset({"url"}):
            raise ValueError("unexpected arguments")
        url = arguments.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("fetch_webpage requires a URL")
        try:
            final_url, text, truncated = self._fetch_direct(url)
            retrieval = ""
        except _UseReaderFallback as direct_error:
            try:
                text, truncated = self._fetch_with_reader(url)
            except (requests.RequestException, ValueError) as reader_error:
                raise ValueError(
                    f"could not fetch {url} directly ({direct_error}) or through "
                    f"the reader fallback ({reader_error})"
                ) from reader_error
            final_url = url
            retrieval = "Retrieval: readable-text fallback\n"

        text_was_truncated = len(text) > MAX_TEXT_CHARS
        text = text[:MAX_TEXT_CHARS]
        if truncated or text_was_truncated:
            text += "\n\n[content truncated]"
        return f"URL: {final_url}\n{retrieval}\n{text}"

    def stop(self) -> None:
        self._session.close()

    def _fetch_direct(self, url: str) -> tuple[str, str, bool]:
        for _ in range(6):
            _validate_public_url(url)
            try:
                response = self._session.get(
                    url,
                    headers=REQUEST_HEADERS,
                    timeout=15,
                    stream=True,
                    allow_redirects=False,
                )
                try:
                    if response.is_redirect or response.is_permanent_redirect:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("redirect has no destination")
                        url = urljoin(url, location)
                        continue
                    try:
                        response.raise_for_status()
                    except requests.RequestException as error:
                        raise _UseReaderFallback(
                            str(_friendly_request_error(error, url))
                        ) from error
                    content_type = response.headers.get("Content-Type", "").lower()
                    if not any(
                        kind in content_type for kind in ("text/", "json", "xml")
                    ):
                        raise _UseReaderFallback(
                            "response was not directly readable text"
                        )
                    text, truncated = _read_response(response)
                finally:
                    response.close()
            except _UseReaderFallback:
                raise
            except requests.RequestException as error:
                raise _UseReaderFallback(
                    str(_friendly_request_error(error, url))
                ) from error

            if "html" in content_type:
                parser = _TextParser()
                parser.feed(text)
                text = parser.text()
                if _looks_unreadable(text):
                    raise _UseReaderFallback("page contained no useful readable text")
            return url, text, truncated
        raise _UseReaderFallback("too many redirects")

    def _fetch_with_reader(self, url: str) -> tuple[str, bool]:
        _validate_public_url(url)
        parsed = urlparse(url)
        source_url = urlunparse(parsed._replace(fragment=""))
        response = self._session.get(
            f"{READER_URL}{source_url}",
            headers={
                **REQUEST_HEADERS,
                "Accept": "text/plain,text/markdown;q=0.9,*/*;q=0.5",
                "X-Respond-With": "markdown",
                "DNT": "1",
            },
            timeout=30,
            stream=True,
            allow_redirects=False,
        )
        try:
            response.raise_for_status()
            text, truncated = _read_response(response)
        except requests.RequestException as error:
            raise _friendly_request_error(error, "reader fallback") from error
        finally:
            response.close()
        if _looks_unreadable(text):
            raise ValueError("reader returned no useful text")
        return text, truncated


def _read_response(response) -> tuple[str, bool]:
    content = bytearray()
    truncated = False
    for chunk in response.iter_content(8192):
        if not chunk:
            continue
        remaining = MAX_DOWNLOAD_BYTES - len(content)
        if len(chunk) > remaining:
            content.extend(chunk[:remaining])
            truncated = True
            break
        content.extend(chunk)
    return content.decode(response.encoding or "utf-8", errors="replace"), truncated


def _looks_unreadable(text: str) -> bool:
    normalized = " ".join(text.split()).lower()
    if not normalized:
        return True
    challenge_markers = (
        "enable javascript and cookies to continue",
        "checking your browser before accessing",
        "just a moment...",
        "verify you are human",
    )
    return len(normalized) < 2_000 and any(
        marker in normalized for marker in challenge_markers
    )


def _friendly_request_error(
    error: requests.RequestException, target: str
) -> ValueError:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return ValueError(f"HTTP {status} from {target}")
    return ValueError(f"request failed for {target}: {error}")
