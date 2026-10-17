import json
import unittest
from unittest.mock import patch

import requests

from kayden.modules.tools.web import Module as WebTool
from kayden.runtime.tools import ToolRuntime


class _WebResponse:
    def __init__(self, text="", content=b"", content_type="text/html", status_code=200):
        self.text = text
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
        self.response = response
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class WebToolTests(unittest.TestCase):
    def test_search_parses_results(self):
        response = _WebResponse(text="""
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">Example Result</a>
            <a class="result__snippet">A useful result snippet.</a>
        """)
        web = WebTool()
        web._session = _WebSession(response)
        result = json.loads(web.execute("search_web", {"query": "example"}))
        self.assertEqual(result, [{
            "title": "Example Result", "url": "https://example.com",
            "snippet": "A useful result snippet.",
        }])
        self.assertTrue(response.closed)

    def test_fetch_blocks_local_addresses(self):
        web = WebTool()
        session = _WebSession(_WebResponse())
        web._session = session
        with self.assertRaisesRegex(ValueError, "private or local"):
            web.execute("fetch_webpage", {"url": "http://127.0.0.1/private"})
        self.assertEqual(session.calls, [])

    def test_fetch_extracts_visible_text(self):
        response = _WebResponse(content=b"<html><script>ignore()</script><body>Hello <b>world</b></body></html>")
        web = WebTool()
        web._session = _WebSession(response)
        with patch("kayden.modules.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
            result = web.execute("fetch_webpage", {"url": "https://example.com/page"})
        self.assertEqual(result, "URL: https://example.com/page\n\nHello\nworld")
        self.assertTrue(response.closed)

    def test_fetch_truncates_large_pages_instead_of_failing(self):
        response = _WebResponse(content=b"<html><body>abcdefghijklmnopqrstuvwxyz</body></html>")
        web = WebTool()
        web._session = _WebSession(response)
        with patch("kayden.modules.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]), patch("kayden.modules.tools.web.MAX_DOWNLOAD_BYTES", 25):
            result = web.execute("fetch_webpage", {"url": "https://example.com/large"})
        self.assertIn("[content truncated]", result)

    def test_http_errors_become_concise_tool_errors(self):
        response = _WebResponse(status_code=403)
        web = WebTool()
        web._session = _WebSession(response)
        runtime = ToolRuntime([web])
        with patch("kayden.modules.tools.web.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 443))]):
            result = runtime.execute("fetch_webpage", {"url": "https://example.com/blocked"})
        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "Tool error: HTTP 403 from https://example.com/blocked")


if __name__ == "__main__":
    unittest.main()
