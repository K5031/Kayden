import unittest
from unittest.mock import patch

import requests

from kayden.modules.tools.web_fetch import Module as WebFetchTool
from kayden.runtime.tools import ToolRuntime


class _WebResponse:
    def __init__(self, content=b"", content_type="text/html", status_code=200):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self.status_code = status_code
        self.is_redirect = False
        self.is_permanent_redirect = False
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        self.closed = True


class _WebSession:
    def __init__(self, response):
        self.responses = response if isinstance(response, list) else [response]
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class WebFetchToolTests(unittest.TestCase):
    def test_only_exposes_fetch_webpage(self):
        web = WebFetchTool()
        self.assertEqual(
            [schema["function"]["name"] for schema in web.schemas()],
            ["fetch_webpage"],
        )
        with self.assertRaisesRegex(ValueError, "Unknown tool"):
            web.execute("search_web", {"query": "example"})

    def test_fetch_blocks_local_addresses(self):
        web = WebFetchTool()
        session = _WebSession(_WebResponse())
        web._session = session
        with self.assertRaisesRegex(ValueError, "private or local"):
            web.execute("fetch_webpage", {"url": "http://127.0.0.1/private"})
        self.assertEqual(session.calls, [])

    def test_fetch_extracts_visible_text(self):
        response = _WebResponse(
            content=(
                b"<html><script>ignore()</script><body>Hello <b>world</b></body></html>"
            )
        )
        web = WebFetchTool()
        web._session = _WebSession(response)
        with patch(
            "kayden.modules.tools.web_fetch.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ):
            result = web.execute(
                "fetch_webpage", {"url": "https://example.com/page"}
            )
        self.assertEqual(result, "URL: https://example.com/page\n\nHello\nworld")
        self.assertTrue(response.closed)

    def test_fetch_truncates_large_pages_instead_of_failing(self):
        response = _WebResponse(
            content=b"<html><body>abcdefghijklmnopqrstuvwxyz</body></html>"
        )
        web = WebFetchTool()
        web._session = _WebSession(response)
        with (
            patch(
                "kayden.modules.tools.web_fetch.socket.getaddrinfo",
                return_value=[(None, None, None, None, ("93.184.216.34", 443))],
            ),
            patch("kayden.modules.tools.web_fetch.MAX_DOWNLOAD_BYTES", 25),
        ):
            result = web.execute(
                "fetch_webpage", {"url": "https://example.com/large"}
            )
        self.assertIn("[content truncated]", result)

    def test_http_errors_become_concise_tool_errors(self):
        direct_response = _WebResponse(status_code=403)
        reader_response = _WebResponse(status_code=502)
        web = WebFetchTool()
        web._session = _WebSession([direct_response, reader_response])
        runtime = ToolRuntime([web])
        with patch(
            "kayden.modules.tools.web_fetch.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ):
            result = runtime.execute(
                "fetch_webpage", {"url": "https://example.com/blocked"}
            )
        self.assertTrue(result.is_error)
        self.assertIn("HTTP 403", result.content)
        self.assertIn("reader fallback", result.content)

    def test_fetch_uses_reader_fallback_when_site_blocks_direct_access(self):
        direct_response = _WebResponse(status_code=403)
        reader_response = _WebResponse(
            content=b"# Example article\n\nThis is the readable article body."
        )
        web = WebFetchTool()
        session = _WebSession([direct_response, reader_response])
        web._session = session
        with patch(
            "kayden.modules.tools.web_fetch.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ):
            result = web.execute(
                "fetch_webpage",
                {"url": "https://example.com/article#section"},
            )
        self.assertIn("Retrieval: readable-text fallback", result)
        self.assertIn("This is the readable article body.", result)
        self.assertEqual(
            session.calls[1][0], "https://r.jina.ai/https://example.com/article"
        )
        self.assertEqual(session.calls[1][1]["headers"]["X-Respond-With"], "markdown")
        self.assertEqual(session.calls[1][1]["headers"]["DNT"], "1")
        self.assertTrue(direct_response.closed)
        self.assertTrue(reader_response.closed)

    def test_fetch_uses_reader_fallback_for_javascript_challenge(self):
        direct_response = _WebResponse(
            content=b"<html><body>Just a moment... Verify you are human</body></html>"
        )
        reader_response = _WebResponse(content=b"Useful rendered article content")
        web = WebFetchTool()
        web._session = _WebSession([direct_response, reader_response])
        with patch(
            "kayden.modules.tools.web_fetch.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ):
            result = web.execute(
                "fetch_webpage", {"url": "https://example.com/challenge"}
            )
        self.assertIn("Useful rendered article content", result)


if __name__ == "__main__":
    unittest.main()
