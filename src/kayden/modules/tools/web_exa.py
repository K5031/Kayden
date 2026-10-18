import json
import os

import requests

from kayden.interfaces import ToolInterface

DEFAULT_ENDPOINT = "https://mcp.exa.ai/mcp"
USER_AGENT = "Kayden/0.1"


class Module(ToolInterface):
    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        api_key: str | None = None,
        timeout: float = 30,
        max_characters: int = 12_000,
    ):
        if not isinstance(endpoint, str) or not endpoint.startswith(
            ("http://", "https://")
        ):
            raise ValueError("Exa endpoint must use http or https")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a positive number")
        if (
            not isinstance(max_characters, int)
            or isinstance(max_characters, bool)
            or max_characters < 1_000
        ):
            raise ValueError("max_characters must be an integer of at least 1000")
        self.endpoint = endpoint
        self.api_key = (
            api_key
            or os.environ.get("KAYDEN_EXA_API_KEY")
            or os.environ.get("EXA_API_KEY")
        )
        self.timeout = timeout
        self.max_characters = max_characters
        self._session = requests.Session()

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": (
                        "Search the public web and return relevant sources with excerpts. "
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

        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = None
        try:
            response = self._session.post(
                self.endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "web_search_exa",
                        "arguments": {
                            "query": query.strip(),
                            "type": "auto",
                            "numResults": limit,
                            "livecrawl": "fallback",
                            "contextMaxCharacters": self.max_characters,
                        },
                    },
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = _decode_mcp_response(response.text)
        except requests.RequestException as error:
            raise _friendly_request_error(error, "Exa search") from error
        finally:
            if response is not None:
                response.close()
        return _extract_mcp_text(payload)

    def stop(self) -> None:
        self._session.close()


def _decode_mcp_response(source: str) -> dict:
    try:
        payload = json.loads(source)
    except json.JSONDecodeError:
        payload = None
        for line in source.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                candidate = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
        if payload is None:
            raise ValueError("Exa returned an invalid MCP response")
    if not isinstance(payload, dict):
        raise ValueError("Exa returned an invalid MCP response")
    return payload


def _extract_mcp_text(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        raise ValueError(f"Exa search failed: {message or 'unknown MCP error'}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Exa returned no search result")
    content = result.get("content")
    if result.get("isError"):
        detail = _first_text(content) or "unknown provider error"
        raise ValueError(f"Exa search failed: {detail}")
    text = _first_text(content)
    if not text:
        raise ValueError("Exa returned no search result")
    return text


def _first_text(content) -> str | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return None


def _friendly_request_error(
    error: requests.RequestException, target: str
) -> ValueError:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return ValueError(f"HTTP {status} from {target}")
    return ValueError(f"request failed for {target}: {error}")
