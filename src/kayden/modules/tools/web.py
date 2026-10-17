import ipaddress
import json
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

from kayden.interfaces import ToolInterface

MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 50_000
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Kayden/0.1"
)


class _SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._capture = None
        self._capture_tag = None
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            self._capture = "title"
            self._capture_tag = tag
            self._href = attributes.get("href")
            self._text = []
        elif "result__snippet" in classes and self.results:
            self._capture = "snippet"
            self._capture_tag = tag
            self._text = []

    def handle_data(self, data):
        if self._capture is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != self._capture_tag:
            return
        text = " ".join("".join(self._text).split())
        if self._capture == "title" and text and self._href:
            self.results.append({
                "title": text,
                "url": _decode_search_url(self._href),
                "snippet": "",
            })
        elif self._capture == "snippet" and text and self.results:
            self.results[-1]["snippet"] = text
        self._capture = None
        self._capture_tag = None
        self._href = None
        self._text = []


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


def _decode_search_url(url: str) -> str:
    parsed = urlparse(url)
    redirected = parse_qs(parsed.query).get("uddg")
    return unquote(redirected[0]) if redirected else url


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
                    "name": "search_web",
                    "description": (
                        "Search the public web and return titles, URLs, and snippets. "
                        "For current news, use specific queries, verify important claims "
                        "with multiple independent sources, and cite their URLs."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_webpage",
                    "description": (
                        "Fetch readable text from a specific public HTTP or HTTPS page. "
                        "Prefer article URLs over large site homepages."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    @staticmethod
    def _validate_arguments(arguments: dict, allowed: set[str]) -> None:
        if not set(arguments).issubset(allowed):
            raise ValueError("unexpected arguments")

    def execute(self, name: str, arguments: dict) -> str:
        if name == "search_web":
            self._validate_arguments(arguments, {"query", "limit"})
            query = arguments.get("query")
            limit = arguments.get("limit", 5)
            if not isinstance(query, str) or not query.strip():
                raise ValueError("search_web requires a non-empty query")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
                raise ValueError("limit must be an integer from 1 to 10")
            try:
                response = self._session.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=15,
                )
                try:
                    response.raise_for_status()
                    parser = _SearchResultParser()
                    parser.feed(response.text)
                    return json.dumps(parser.results[:limit], ensure_ascii=False)
                finally:
                    response.close()
            except requests.RequestException as error:
                raise _friendly_request_error(error, "web search") from error

        if name == "fetch_webpage":
            self._validate_arguments(arguments, {"url"})
            url = arguments.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError("fetch_webpage requires a URL")
            for _ in range(6):
                _validate_public_url(url)
                try:
                    response = self._session.get(
                        url,
                        headers={"User-Agent": USER_AGENT},
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
                        response.raise_for_status()
                        content_type = response.headers.get("Content-Type", "").lower()
                        if not any(kind in content_type for kind in ("text/", "json", "xml")):
                            raise ValueError("URL did not return readable text")
                        content = bytearray()
                        truncated = False
                        for chunk in response.iter_content(8192):
                            remaining = MAX_DOWNLOAD_BYTES - len(content)
                            if len(chunk) > remaining:
                                content.extend(chunk[:remaining])
                                truncated = True
                                break
                            content.extend(chunk)
                        try:
                            text = content.decode(response.encoding or "utf-8")
                        except UnicodeDecodeError as error:
                            raise ValueError("page is not readable text") from error
                    finally:
                        response.close()
                except requests.RequestException as error:
                    raise _friendly_request_error(error, url) from error
                if "html" in content_type:
                    parser = _TextParser()
                    parser.feed(text)
                    text = parser.text()
                text_was_truncated = len(text) > MAX_TEXT_CHARS
                text = text[:MAX_TEXT_CHARS]
                if truncated or text_was_truncated:
                    text += "\n\n[content truncated]"
                return f"URL: {url}\n\n{text}"
            raise ValueError("too many redirects")

        raise ValueError(f"Unknown tool: {name}")

    def stop(self) -> None:
        self._session.close()


def _friendly_request_error(error: requests.RequestException, target: str) -> ValueError:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return ValueError(f"HTTP {status} from {target}")
    return ValueError(f"request failed for {target}: {error}")
