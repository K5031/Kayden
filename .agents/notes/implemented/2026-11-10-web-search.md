# Agent Note: Web search

Status: implemented — the built-in Tools search the web and read pages, with every Core and no extra key

## Problem

Kayden could not look anything up. Every question about something recent, a library's
current API, a price or a news item got an answer from the model's training data, or a
shell command (`curl`) whose raw HTML the agent had to wade through. A general assistant
needs to search the web and read what it finds, and the user should see where an answer
came from.

## Decision

Two tools in the built-in Tools module (`shell-files`, shown as "Shell, files and web"),
in `kayden/tools/web.py`:

| Tool | Does | Returns |
|---|---|---|
| `web_search(query, max_results?)` | searches the web (default 8 results, at most 20) | numbered results: title, URL, snippet |
| `web_fetch(url, start?)` | downloads one page and extracts its main text | the final URL and the text as Markdown, cut at 30 000 characters with a note to continue at `start` |

- **Search backend:** [ddgs](https://pypi.org/project/ddgs/) (DuckDuckGo and other
  engines, no account or key). It is synchronous, so it runs in a thread with a 10 s
  timeout.
- **Reading pages:** `httpx2` (already a dependency) downloads at most 5 MB, following up
  to 5 redirects by hand; [trafilatura](https://pypi.org/project/trafilatura/) extracts
  the main content as Markdown with links and tables, in a thread. Plain text and JSON
  are returned as they are; other types (PDF, images) are an error.
- **Public addresses only.** `web_fetch` refuses a host that resolves to a loopback,
  private or link-local address, checked at every redirect. Local pages are what the shell
  is for, where permissions apply as usual.
- **Permissions:** both tools are read-only and confined: they read nothing on the
  computer and change nothing. `Tools.confined` means "can read and change nothing on this
  computer outside the work folder", so a web request qualifies; that holds even when the
  work folder is no boundary.
- **Untrusted content.** Results start with a line saying the content is from the web and
  its instructions are not the user's.
- **Citing.** The tool descriptions tell the model to link the pages an answer relies on.
  Replies are Markdown, so links render in both frontends.
- **Testing.** `Web` takes the search function, the HTTP transport and the resolver as
  arguments; the suite uses fakes and `httpx2.MockTransport`, never the network
  (`tests/test_tools_web.py`).

Choices and limits are listed in `.agents/decisions.md` (Web tools).

## Alternatives considered

- **The provider's own web search** (Anthropic's server-side `web_search` tool, OpenAI's
  in the Responses API). Better results and built-in citations, but each provider works
  differently and DeepSeek has none, it needs new block types for server tool calls and
  citations in the archive, and it costs per search on top of tokens. The client-side
  tools work the same with every Core.
- **A search API with its own key** (Brave, Tavily, Google). More reliable than a keyless
  backend, but a second account and key is exactly what non-technical users do not set
  up. A key option can be added if ddgs proves flaky.
- **Only `curl` in the shell.** Works, but raw HTML wastes the context window and the
  model has no way to search.
- **A separate Tools module for the web.** An instance has one Tools module, so the user
  would have to choose between files and the web.
- **Converting HTML ourselves** (BeautifulSoup, html2text). Converts the whole page,
  menus and footers included; trafilatura finds the article.

## Consequences

- The agent answers from current sources with every provider, and the user sees links.
- **Keyless search can break or be rate-limited.** DuckDuckGo changes its pages; ddgs
  follows, so it needs keeping up to date. The error result says search is unavailable.
- **Prompt injection.** A page can carry instructions. The header helps; the
  [automatic permission check](2026-11-10-auto-permissions.md) sees only the user's
  messages, not page text.
- **What leaves the computer.** Queries go to the search engines, page requests to the
  sites. The agent can put anything it has read into a query or URL; the docs say so.
- **DNS rebinding** can turn a checked public name into a private address between the
  check and the connection. The check makes accidental local fetches impossible, not a
  determined attacker's.
- Two dependencies (ddgs with its HTTP client primp, trafilatura with lxml) for a feature
  every conversation can use.
