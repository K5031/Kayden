import json
import unittest

import requests

from kayden.modules.tools.web_exa import Module as ExaTool
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

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class _FailingSession(_Session):
    def post(self, url, **kwargs):
        raise requests.ConnectionError("DNS failed")


def _success_payload(text="Search results"):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}]},
    }


class ExaToolTests(unittest.TestCase):
    def test_search_calls_hosted_mcp_with_expected_arguments(self):
        response = _Response(json.dumps(_success_payload("Useful results")))
        exa = ExaTool(api_key="secret", max_characters=9000)
        session = _Session(response)
        exa._session = session

        result = exa.execute("search_web", {"query": " latest news ", "limit": 3})

        self.assertEqual(result, "Useful results")
        url, request = session.calls[0]
        self.assertEqual(url, "https://mcp.exa.ai/mcp")
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(request["json"]["method"], "tools/call")
        self.assertEqual(request["json"]["params"]["name"], "web_search_exa")
        self.assertEqual(
            request["json"]["params"]["arguments"],
            {
                "query": "latest news",
                "type": "auto",
                "numResults": 3,
                "livecrawl": "fallback",
                "contextMaxCharacters": 9000,
            },
        )
        self.assertTrue(response.closed)

    def test_anonymous_search_does_not_send_authorization(self):
        response = _Response(json.dumps(_success_payload()))
        exa = ExaTool(api_key=None)
        session = _Session(response)
        exa._session = session

        exa.execute("search_web", {"query": "example"})

        self.assertNotIn("Authorization", session.calls[0][1]["headers"])

    def test_search_accepts_sse_mcp_response(self):
        payload = json.dumps(_success_payload("SSE results"))
        exa = ExaTool()
        exa._session = _Session(_Response(f"event: message\ndata: {payload}\n\n"))

        self.assertEqual(
            exa.execute("search_web", {"query": "example"}), "SSE results"
        )

    def test_provider_and_http_errors_are_concise_tool_errors(self):
        provider = ExaTool()
        provider._session = _Session(
            _Response(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "rate limited"}],
                        },
                    }
                )
            )
        )
        provider_result = ToolRuntime([provider]).execute(
            "search_web", {"query": "news"}
        )
        self.assertTrue(provider_result.is_error)
        self.assertIn("rate limited", provider_result.content)

        http = ExaTool()
        http._session = _Session(_Response(status_code=429))
        http_result = ToolRuntime([http]).execute("search_web", {"query": "news"})
        self.assertTrue(http_result.is_error)
        self.assertIn("HTTP 429", http_result.content)

        connection = ExaTool()
        connection._session = _FailingSession(_Response())
        connection_result = ToolRuntime([connection]).execute(
            "search_web", {"query": "news"}
        )
        self.assertTrue(connection_result.is_error)
        self.assertIn("request failed for Exa search", connection_result.content)

    def test_rejects_invalid_search_arguments(self):
        exa = ExaTool()
        with self.assertRaisesRegex(ValueError, "non-empty query"):
            exa.execute("search_web", {"query": ""})
        with self.assertRaisesRegex(ValueError, "integer from 1 to 10"):
            exa.execute("search_web", {"query": "news", "limit": True})
        with self.assertRaisesRegex(ValueError, "unexpected arguments"):
            exa.execute("search_web", {"query": "news", "extra": 1})


if __name__ == "__main__":
    unittest.main()
