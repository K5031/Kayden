import json
import os

import requests

from kayden.interfaces import ToolInterface

USER_AGENT = "Kayden/0.1"


class Module(ToolInterface):
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 20,
        language: str = "all",
        safesearch: int = 0,
    ):
        base_url = base_url or os.environ.get("KAYDEN_SEARXNG_URL")
        if not isinstance(base_url, str) or not base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError(
                "web_searxng requires an http(s) base_url or KAYDEN_SEARXNG_URL"
            )
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive number")
        if not isinstance(language, str) or not language:
            raise ValueError("language must be a non-empty string")
        if safesearch not in {0, 1, 2}:
            raise ValueError("safesearch must be 0, 1, or 2")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.language = language
        self.safesearch = safesearch
        self._session = requests.Session()

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": (
                        "Search the public web through the configured SearXNG instance. "
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
            }
        ]

    def describe_activity(self, name, arguments, state, result=None):
        query = " ".join(str(arguments.get("query", "")).split())[:72]
        subject = f" for “{query}”" if query else ""
        if state == "running":
            return f"Searching the web{subject}"
        if state == "failed":
            return "Web search failed"
        return f"Searched the web{subject}"

    def execute(self, name: str, arguments: dict) -> str:
        if name != "search_web":
            raise ValueError(f"Unknown tool: {name}")
        if not set(arguments).issubset({"query", "limit"}):
            raise ValueError("unexpected arguments")
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search_web requires a non-empty query")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            raise ValueError("limit must be an integer from 1 to 10")

        response = None
        try:
            response = self._session.get(
                f"{self.base_url}/search",
                params={
                    "q": query.strip(),
                    "format": "json",
                    "language": self.language,
                    "safesearch": self.safesearch,
                },
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=self.timeout,
            )
            response.raise_for_status()
            try:
                payload = json.loads(response.text)
            except json.JSONDecodeError as error:
                raise ValueError(
                    "SearXNG returned invalid JSON; its JSON format may be disabled"
                ) from error
        except requests.RequestException as error:
            raise _friendly_request_error(error, "SearXNG search") from error
        finally:
            if response is not None:
                response.close()
        return json.dumps(_normalize_results(payload, limit), ensure_ascii=False)

    def stop(self) -> None:
        self._session.close()


def _normalize_results(payload, limit: int) -> list[dict]:
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("SearXNG returned an unexpected response")
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        snippet = item.get("content") or ""
        if not isinstance(snippet, str):
            snippet = ""
        results.append(
            {
                "title": " ".join(title.split()),
                "url": url,
                "snippet": " ".join(snippet.split())[:1_000],
            }
        )
        if len(results) == limit:
            break
    if not results:
        raise ValueError("SearXNG returned no results")
    return results


def _friendly_request_error(
    error: requests.RequestException, target: str
) -> ValueError:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return ValueError(f"HTTP {status} from {target}")
    return ValueError(f"request failed for {target}: {error}")
