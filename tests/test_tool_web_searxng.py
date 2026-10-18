import json
import unittest

import requests

from kayden.modules.tools.web_searxng import Module as SearXNGTool
from kayden.runtime.tools import ToolRuntime


class _Response:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class _FailingSession(_Session):
    def get(self, url, **kwargs):
        raise requests.ConnectionError("DNS failed")


class SearXNGToolTests(unittest.TestCase):
    def test_requires_a_configured_instance(self):
        with self.assertRaisesRegex(ValueError, "requires an http"):
            SearXNGTool(base_url=None)

    def test_search_normalizes_and_limits_results(self):
        response = _Response(
            json.dumps(
                {
                    "results": [
                        {
                            "title": " First   result ",
                            "url": "https://example.com/1",
                            "content": " Useful   excerpt. ",
                        },
                        {
                            "title": "Second result",
                            "url": "https://example.com/2",
                            "content": "Another excerpt.",
                        },
                    ]
                }
            )
        )
        searxng = SearXNGTool(
            base_url="http://127.0.0.1:8888/",
            language="en-GB",
            safesearch=1,
        )
        session = _Session(response)
        searxng._session = session

        results = json.loads(
            searxng.execute("search_web", {"query": " latest news ", "limit": 1})
        )

        self.assertEqual(
            results,
            [
                {
                    "title": "First result",
                    "url": "https://example.com/1",
                    "snippet": "Useful excerpt.",
                }
            ],
        )
        url, request = session.calls[0]
        self.assertEqual(url, "http://127.0.0.1:8888/search")
        self.assertEqual(
            request["params"],
            {
                "q": "latest news",
                "format": "json",
                "language": "en-GB",
                "safesearch": 1,
            },
        )
        self.assertTrue(response.closed)

    def test_explains_when_json_format_is_disabled(self):
        searxng = SearXNGTool(base_url="https://search.example")
        searxng._session = _Session(_Response("<html>not json</html>"))
        result = ToolRuntime([searxng]).execute("search_web", {"query": "news"})
        self.assertTrue(result.is_error)
        self.assertIn("JSON format may be disabled", result.content)

    def test_http_and_empty_result_errors_are_concise(self):
        http = SearXNGTool(base_url="https://search.example")
        http._session = _Session(_Response(status_code=403))
        http_result = ToolRuntime([http]).execute("search_web", {"query": "news"})
        self.assertTrue(http_result.is_error)
        self.assertIn("HTTP 403", http_result.content)

        empty = SearXNGTool(base_url="https://search.example")
        empty._session = _Session(_Response('{"results": []}'))
        empty_result = ToolRuntime([empty]).execute(
            "search_web", {"query": "news"}
        )
        self.assertTrue(empty_result.is_error)
        self.assertIn("returned no results", empty_result.content)

        connection = SearXNGTool(base_url="https://search.example")
        connection._session = _FailingSession(_Response())
        connection_result = ToolRuntime([connection]).execute(
            "search_web", {"query": "news"}
        )
        self.assertTrue(connection_result.is_error)
        self.assertIn("request failed for SearXNG search", connection_result.content)

    def test_rejects_invalid_search_arguments(self):
        searxng = SearXNGTool(base_url="https://search.example")
        with self.assertRaisesRegex(ValueError, "non-empty query"):
            searxng.execute("search_web", {"query": ""})
        with self.assertRaisesRegex(ValueError, "integer from 1 to 10"):
            searxng.execute("search_web", {"query": "news", "limit": 0})
        with self.assertRaisesRegex(ValueError, "unexpected arguments"):
            searxng.execute("search_web", {"query": "news", "extra": 1})


if __name__ == "__main__":
    unittest.main()
