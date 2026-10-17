import json
import os
from typing import Callable, Iterator

import requests

from events import CoreEvent, CoreTextDelta, CoreToolCall, CoreUsage
from interfaces import CoreInterface
from klogger import get_logger

OPENAI_BASE_URL = "https://api.openai.com/v1"


class Module(CoreInterface):
    """Stream one OpenAI Chat Completions request at a time."""

    runtime_kind = "cloud"
    reports_usage = True

    def __init__(self, model="gpt-6-luna", **kwargs):
        self.log = get_logger("OpenAI")
        self.model = model
        self._interrupted = False
        self.last_usage = None

        self.api_key = os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OpenAI API key not found. Set the OPENAI_API_KEY "
                "environment variable."
            )

        self.log.info("Using model: %s", model)
        self._session = requests.Session()

    def interrupt(self) -> None:
        self._interrupted = True

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> Iterator[CoreEvent]:
        self._interrupted = False
        self.last_usage = None

        if should_interrupt and should_interrupt():
            self._interrupted = True
            return

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": 512,
            "reasoning_effort": "none",
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                stream=True,
                timeout=60,
            )
        except requests.RequestException as error:
            self.log.error("OpenAI request failed: %s", error)
            yield CoreTextDelta("[request failed — check your network and logs]")
            return

        tool_calls = {}
        try:
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                self.log.error("OpenAI API error: %s — %s", error, response.text[:500])
                yield CoreTextDelta("[API error — check logs]")
                return

            for line in response.iter_lines():
                if self._interrupted or (should_interrupt and should_interrupt()):
                    self._interrupted = True
                    return
                if not line:
                    continue
                try:
                    line = line.decode("utf-8") if isinstance(line, bytes) else line
                except UnicodeDecodeError as error:
                    self.log.warning("Ignoring invalid UTF-8 from OpenAI: %s", error)
                    continue
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as error:
                    self.log.warning("Ignoring malformed OpenAI event: %s", error)
                    continue
                if not isinstance(chunk, dict):
                    self.log.warning("Ignoring non-object OpenAI event")
                    continue

                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    self.last_usage = usage
                    yield CoreUsage(usage)

                choices = chunk.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                delta = choices[0].get("delta") or {}
                if not isinstance(delta, dict):
                    continue

                for part in delta.get("tool_calls") or []:
                    if not isinstance(part, dict) or not isinstance(part.get("index"), int):
                        self.log.warning("Ignoring malformed tool-call fragment")
                        continue
                    call = tool_calls.setdefault(part["index"], {
                        "id": "",
                        "name": "",
                        "arguments": "",
                    })
                    call_id = part.get("id")
                    if isinstance(call_id, str):
                        call["id"] += call_id
                    function = part.get("function") or {}
                    if not isinstance(function, dict):
                        self.log.warning("Ignoring malformed tool-call function fragment")
                        continue
                    name = function.get("name")
                    arguments = function.get("arguments")
                    if isinstance(name, str):
                        call["name"] += name
                    if isinstance(arguments, str):
                        call["arguments"] += arguments

                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield CoreTextDelta(content)
                refusal = delta.get("refusal")
                if isinstance(refusal, str) and refusal:
                    yield CoreTextDelta(refusal)
        except requests.RequestException as error:
            self.log.error("OpenAI connection dropped mid-response: %s", error)
            yield CoreTextDelta(" [connection dropped]")
            return
        finally:
            response.close()

        calls = [tool_calls[index] for index in sorted(tool_calls)]
        if any(not call["id"] or not call["name"] for call in calls):
            self.log.error("OpenAI returned an incomplete tool call")
            yield CoreTextDelta(" [invalid tool request]")
            return
        for call in calls:
            yield CoreToolCall(call["id"], call["name"], call["arguments"])

    def stop(self) -> None:
        self._session.close()
