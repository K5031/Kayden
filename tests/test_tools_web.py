from pathlib import Path
from typing import Any

import httpx2
import pytest

from kadence.api import ToolCall
from kadence.tools.shell_files import ShellFilesTools, web
from tests.conformance.tools_suite import RecordingContext

ARTICLE = """<html><head><title>Asyncio notes</title></head><body>
<nav>Home | About | Contact</nav>
<article><h1>Asyncio notes</h1>
<p>TaskGroup waits for every task it started and cancels the rest when one fails.
It was added in Python 3.11 and replaces most uses of gather.</p>
<p>A second paragraph with a <a href="https://docs.python.org/3/">link to the docs</a>,
long enough that the extractor keeps it as part of the main content of the page.</p>
</article><footer>Copyright footer</footer></body></html>"""

PUBLIC = {"example.com": ["93.184.215.14"], "other.example": ["93.184.215.15"],
          "sneaky.example": ["93.184.215.16"], "intranet.example": ["10.0.0.7"],
          "v6local.example": ["::1"]}


async def fake_resolve(host: str) -> list[str]:
    if host not in PUBLIC:
        raise OSError("Name or service not known")
    return PUBLIC[host]


connections: list[tuple[str, str, str | None]] = []  # address, Host header, TLS name


def handler(request: httpx2.Request) -> httpx2.Response:
    host, path = request.headers["host"], request.url.path
    connections.append((request.url.host, host, request.extensions.get("sni_hostname")))
    if host == "sneaky.example":
        return httpx2.Response(302, headers={"location": "http://127.0.0.1:8000/admin"})
    if path == "/moved":
        return httpx2.Response(301, headers={"location": "https://other.example/article"})
    if path == "/forbidden":
        return httpx2.Response(403, text="blocked")
    if path == "/loop":
        return httpx2.Response(302, headers={"location": "/loop"})
    if path in ("/article", "/"):
        return httpx2.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
    if path == "/data.json":
        return httpx2.Response(200, text='{"a": [1, 2]}',
                               headers={"content-type": "application/json"})
    if path == "/long.txt":
        return httpx2.Response(200, text="x" * (web.MAX_PAGE_CHARS + 500),
                               headers={"content-type": "text/plain"})
    if path == "/huge.txt":
        return httpx2.Response(200, content=b"y" * (web.MAX_DOWNLOAD_BYTES + 1),
                               headers={"content-type": "text/plain"})
    if path == "/file.pdf":
        return httpx2.Response(200, content=b"%PDF-1.7",
                               headers={"content-type": "application/pdf"})
    return httpx2.Response(404, text="not found")


searches: list[tuple[str, int]] = []


def fake_search(query: str, max_results: int) -> list[dict]:
    searches.append((query, max_results))
    if query == "nothing":
        return []
    if query == "broken":
        raise web.WebError("Web search is unavailable right now: rate limited")
    return [{"title": f"Result {n}", "href": f"https://example.com/{n}",
             "body": f"Snippet  number\n{n}"} for n in range(1, 30)]


@pytest.fixture
def tools(tmp_path: Path) -> ShellFilesTools:
    (tmp_path / "ws").mkdir()
    return ShellFilesTools(tmp_path / "ws", web_tools=web.Web(
        search=fake_search, transport=httpx2.MockTransport(handler), resolve=fake_resolve,
        retry_delay=0))


async def run(tools: ShellFilesTools, tmp_path: Path, name: str, **args):
    return await tools.execute(ToolCall("c", name, args), RecordingContext("c"))


async def test_web_tools_are_offered_read_only_and_confined(tools):
    specs = {s.name: s for s in await tools.list_tools()}
    assert specs["web_search"].read_only and specs["web_fetch"].read_only
    assert await tools.confined(ToolCall("c", "web_search", {"query": "x"}))
    assert await tools.confined(ToolCall("c", "web_fetch", {"url": "https://example.com"}))


async def test_web_tools_are_confined_even_when_the_folder_is_no_boundary():
    tools = ShellFilesTools(Path.home(), web_tools=web.Web(search=fake_search))
    assert await tools.confined(ToolCall("c", "web_search", {"query": "x"}))
    assert not await tools.confined(ToolCall("c", "read_file", {"path": "a"}))


async def test_search_lists_results_with_links(tools, tmp_path):
    out = await run(tools, tmp_path, "web_search", query="asyncio taskgroup", max_results=3)
    assert not out.is_error
    assert searches[-1] == ("asyncio taskgroup", 3)
    assert out.content.startswith(web.UNTRUSTED)
    assert "1. Result 1\n   https://example.com/1\n   Snippet number 1" in out.content
    assert "3. Result 3" in out.content and "4. Result 4" not in out.content


async def test_search_defaults_and_failures(tools, tmp_path):
    await run(tools, tmp_path, "web_search", query="defaults")
    assert searches[-1] == ("defaults", web.DEFAULT_RESULTS)
    empty = await run(tools, tmp_path, "web_search", query="nothing")
    assert not empty.is_error and empty.content.startswith("Nothing found for 'nothing'")
    assert f"after {web.SEARCH_ATTEMPTS} tries" in empty.content
    assert "refuse" in empty.content  # it may be the engines, not the query
    broken = await run(tools, tmp_path, "web_search", query="broken")
    assert broken.is_error and "unavailable" in broken.content


async def test_an_empty_or_failed_search_is_tried_again(tmp_path):
    """Search engines refuse some requests for a while: the same query may work a moment
    later, so an empty or failed search is retried before Kadence reports it."""
    answers: list[Any] = [[], web.WebError("Web search is unavailable right now: busy"),
                          [{"title": "Found", "href": "https://example.com/", "body": "it"}]]
    calls = []

    def flaky(query: str, max_results: int) -> list[dict]:
        calls.append(query)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    tools = ShellFilesTools(tmp_path, web_tools=web.Web(search=flaky, retry_delay=0))
    out = await run(tools, tmp_path, "web_search", query="worlds")
    assert not out.is_error and "1. Found" in out.content and len(calls) == 3


async def test_fetch_extracts_the_main_text_as_markdown(tools, tmp_path):
    out = await run(tools, tmp_path, "web_fetch", url="https://example.com/article")
    assert not out.is_error, out.content
    assert out.content.startswith("URL: https://example.com/article\n" + web.UNTRUSTED)
    assert "TaskGroup waits for every task" in out.content
    assert "[link to the docs](https://docs.python.org/3/)" in out.content
    assert "Copyright footer" not in out.content


async def test_fetch_follows_redirects_and_reports_the_final_url(tools, tmp_path):
    out = await run(tools, tmp_path, "web_fetch", url="https://example.com/moved")
    assert out.content.startswith("URL: https://other.example/article")


async def test_fetch_connects_to_the_address_it_checked(tools, tmp_path):
    """No second lookup at connect time, so a name cannot be switched to a local address
    between the check and the request (DNS rebinding)."""
    connections.clear()
    await run(tools, tmp_path, "web_fetch", url="https://example.com/data.json")
    assert connections == [("93.184.215.14", "example.com", "example.com")]
    await run(tools, tmp_path, "web_fetch", url="http://[2606:4700::1111]:8080/data.json")
    assert connections[-1] == ("2606:4700::1111", "[2606:4700::1111]:8080", None)


async def test_fetch_returns_text_and_json_as_they_are(tools, tmp_path):
    out = await run(tools, tmp_path, "web_fetch", url="https://example.com/data.json")
    assert '"a": [\n    1,' in out.content


async def test_fetch_cuts_long_pages_and_continues_at_start(tools, tmp_path):
    first = await run(tools, tmp_path, "web_fetch", url="https://example.com/long.txt")
    assert f"call again with start={web.MAX_PAGE_CHARS}" in first.content
    rest = await run(tools, tmp_path, "web_fetch", url="https://example.com/long.txt",
                     start=web.MAX_PAGE_CHARS)
    assert rest.content.endswith("\n" + "x" * 500)
    past = await run(tools, tmp_path, "web_fetch", url="https://example.com/long.txt",
                     start=10**6)
    assert past.is_error and "past the end" in past.content


@pytest.mark.parametrize("url, message", [
    ("http://127.0.0.1:8000/", "local or private"),
    ("http://localhost/", "could not be found"),  # the fake resolver knows no localhost
    ("https://intranet.example/", "local or private"),
    ("https://v6local.example/", "local or private"),
    ("http://[::1]/", "local or private"),
    ("https://sneaky.example/", "local or private"),  # through a redirect
    ("file:///etc/passwd", "not an http or https address"),
    ("ftp://example.com/x", "not an http or https address"),
])
async def test_fetch_refuses_local_addresses_and_other_schemes(tools, tmp_path, url, message):
    out = await run(tools, tmp_path, "web_fetch", url=url)
    assert out.is_error and message in out.content


@pytest.mark.parametrize("path, message", [
    ("/missing", "HTTP 404"),
    ("/forbidden", "refuses programs"),
    ("/loop", "redirected more than"),
    ("/huge.txt", "larger than 5 MB"),
    ("/file.pdf", "application/pdf"),
])
async def test_fetch_failures_are_error_results(tools, tmp_path, path, message):
    out = await run(tools, tmp_path, "web_fetch", url=f"https://example.com{path}")
    assert out.is_error and message in out.content


async def test_a_network_error_is_an_error_result(tmp_path):
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    tools = ShellFilesTools(tmp_path, web_tools=web.Web(
        transport=httpx2.MockTransport(fail), resolve=fake_resolve))
    out = await run(tools, tmp_path, "web_fetch", url="https://example.com/")
    assert out.is_error and "could not be read" in out.content


async def test_a_missing_library_says_to_reinstall(tmp_path):
    def missing(query: str, max_results: int) -> list[dict]:
        raise ModuleNotFoundError("No module named 'ddgs'", name="ddgs")

    tools = ShellFilesTools(tmp_path, web_tools=web.Web(search=missing))
    out = await run(tools, tmp_path, "web_search", query="x")
    assert out.is_error and "'ddgs' is not installed" in out.content
    assert "reinstall Kadence" in out.content
