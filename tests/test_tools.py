import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from modules.tools.calculator import Module as CalculatorTool
from modules.tools.filesystem import Module as FilesystemTool
from modules.tools.web import Module as WebTool
from tool_runtime import ToolRuntime


class _Tool:
    def __init__(self, name="echo", result="ok", error=None):
        self.name = name
        self.result = result
        self.error = error
        self.stopped = False

    def schemas(self):
        return [{
            "type": "function",
            "function": {"name": self.name, "parameters": {"type": "object"}},
        }]

    def execute(self, name, arguments):
        if self.error:
            raise self.error
        return self.result

    def stop(self):
        self.stopped = True


class ToolRuntimeTests(unittest.TestCase):
    def test_combines_schemas_and_routes_execution(self):
        first = _Tool("first", "one")
        second = _Tool("second", "two")
        runtime = ToolRuntime([first, second])

        self.assertEqual(
            [schema["function"]["name"] for schema in runtime.schemas()],
            ["first", "second"],
        )
        self.assertEqual(runtime.execute("second", {}).content, "two")

    def test_rejects_duplicate_names(self):
        with self.assertRaisesRegex(ValueError, "duplicate tool name"):
            ToolRuntime([_Tool("same"), _Tool("same")])

    def test_unknown_and_failed_tools_become_error_results(self):
        runtime = ToolRuntime([_Tool(error=ValueError("bad input"))])

        self.assertTrue(runtime.execute("missing", {}).is_error)
        result = runtime.execute("echo", {})
        self.assertTrue(result.is_error)
        self.assertIn("bad input", result.content)

    def test_stop_reaches_every_module(self):
        first = _Tool("first")
        second = _Tool("second")
        runtime = ToolRuntime([first, second])

        runtime.stop()

        self.assertTrue(first.stopped and second.stopped)

    def test_add_exposes_tools_without_transferring_stop_ownership(self):
        runtime = ToolRuntime([])
        borrowed = _Tool("borrowed")

        runtime.add(borrowed)
        runtime.stop()

        self.assertEqual(runtime.execute("borrowed", {}).content, "ok")
        self.assertFalse(borrowed.stopped)


class CalculatorToolTests(unittest.TestCase):
    def test_evaluates_arithmetic_and_functions(self):
        calculator = CalculatorTool()

        self.assertEqual(
            calculator.execute("calculate", {"expression": "sqrt(81) + 2 ** 3"}),
            "17.0",
        )

    def test_rejects_code_and_excessive_calculations(self):
        calculator = CalculatorTool()

        with self.assertRaisesRegex(ValueError, "unsupported expression"):
            calculator.execute(
                "calculate",
                {"expression": "__import__('os').getcwd()"},
            )
        with self.assertRaisesRegex(ValueError, "exponent is too large"):
            calculator.execute("calculate", {"expression": "2 ** 1001"})


class FilesystemToolTests(unittest.TestCase):
    def test_lists_reads_and_searches_within_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            (root / "docs" / "Guide.txt").write_text("hello")
            (root / ".secret").write_text("hidden")
            filesystem = FilesystemTool(root=root)

            self.assertEqual(filesystem.execute("list_directory", {}), "docs/")
            self.assertEqual(
                filesystem.execute("read_text_file", {"path": "docs/Guide.txt"}),
                "hello",
            )
            self.assertEqual(
                filesystem.execute("search_files", {"query": "guide"}),
                "docs/Guide.txt",
            )

    def test_rejects_traversal_and_hidden_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            filesystem = FilesystemTool(root=root)

            with self.assertRaisesRegex(ValueError, "escapes"):
                filesystem.execute("read_text_file", {"path": "../outside.txt"})
            with self.assertRaisesRegex(ValueError, "hidden"):
                filesystem.execute("read_text_file", {"path": ".env"})


class _WebResponse:
    def __init__(
        self,
        text="",
        content=b"",
        content_type="text/html",
        status_code=200,
    ):
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
            raise requests.HTTPError(
                f"HTTP {self.status_code}",
                response=self,
            )

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
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">
                Example Result
            </a>
            <a class="result__snippet">A useful result snippet.</a>
        """)
        web = WebTool()
        web._session = _WebSession(response)

        result = json.loads(web.execute("search_web", {"query": "example"}))

        self.assertEqual(result, [{
            "title": "Example Result",
            "url": "https://example.com",
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
        response = _WebResponse(
            content=b"<html><script>ignore()</script><body>Hello <b>world</b></body></html>"
        )
        web = WebTool()
        web._session = _WebSession(response)

        with patch(
            "modules.tools.web.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ):
            result = web.execute(
                "fetch_webpage", {"url": "https://example.com/page"}
            )

        self.assertEqual(result, "URL: https://example.com/page\n\nHello\nworld")
        self.assertTrue(response.closed)

    def test_fetch_truncates_large_pages_instead_of_failing(self):
        response = _WebResponse(content=b"<html><body>abcdefghijklmnopqrstuvwxyz</body></html>")
        web = WebTool()
        web._session = _WebSession(response)

        with patch(
            "modules.tools.web.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ), patch("modules.tools.web.MAX_DOWNLOAD_BYTES", 25):
            result = web.execute(
                "fetch_webpage", {"url": "https://example.com/large"}
            )

        self.assertIn("[content truncated]", result)

    def test_http_errors_become_concise_tool_errors(self):
        response = _WebResponse(status_code=403)
        web = WebTool()
        web._session = _WebSession(response)
        runtime = ToolRuntime([web])

        with patch(
            "modules.tools.web.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ):
            result = runtime.execute(
                "fetch_webpage", {"url": "https://example.com/blocked"}
            )

        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "Tool error: HTTP 403 from https://example.com/blocked")


if __name__ == "__main__":
    unittest.main()
