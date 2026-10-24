import json
import os
from typing import Callable, Iterator

import requests

from kayden.events import (
    CoreEvent,
    CoreReasoningDelta,
    CoreTextDelta,
    CoreToolCall,
    CoreUsage,
)
from kayden.interfaces import CoreInterface
from kayden.klogger import get_logger

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class Module(CoreInterface):
    runtime_kind = "cloud"
    reports_usage = True

    def __init__(self, model="deepseek-flash", **kwargs):
        self.log = get_logger("DeepSeek")
        self.model = model
        self.thinking = "disabled"
        self._interrupted = False

        self.api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError(
                "DeepSeek API key not found. Set the DEEPSEEK_API_KEY "
                "environment variable."
            )

        self.log.info("Using model: %s (thinking=%s)", model, self.thinking)
        self._session = requests.Session()
        self.last_usage = None

    def interrupt(self):
        self._interrupted = True

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        should_interrupt: Callable[[], bool] | None = None,
        max_output_tokens: int | None = None,
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
            "temperature": 0.45,
            "top_p": 0.85,
            "max_tokens": max(1, int(max_output_tokens or 512)),
            "thinking": {"type": self.thinking},
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                stream=True,
                timeout=60,
            )
        except requests.RequestException as error:
            self.log.error("DeepSeek request failed: %s", error)
            yield CoreTextDelta("[request failed — check your network and logs]")
            return

        tool_calls = {}
        try:
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                self.log.error("DeepSeek API error: %s — %s", error, response.text[:500])
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
                    self.log.warning("Ignoring invalid UTF-8 from DeepSeek: %s", error)
                    continue
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as error:
                    self.log.warning("Ignoring malformed DeepSeek event: %s", error)
                    continue
                if not isinstance(chunk, dict):
                    self.log.warning("Ignoring non-object DeepSeek event")
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

                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    yield CoreReasoningDelta(reasoning)

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
        except requests.RequestException as error:
            self.log.error("DeepSeek connection dropped mid-response: %s", error)
            yield CoreTextDelta(" [connection dropped]")
            return
        finally:
            response.close()

        calls = [tool_calls[index] for index in sorted(tool_calls)]
        if any(not call["id"] or not call["name"] for call in calls):
            self.log.error("DeepSeek returned an incomplete tool call")
            yield CoreTextDelta(" [invalid tool request]")
            return
        for call in calls:
            yield CoreToolCall(call["id"], call["name"], call["arguments"])

    def stop(self) -> None:
        self._session.close()
