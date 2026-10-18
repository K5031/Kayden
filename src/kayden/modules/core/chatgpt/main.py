"""ChatGPT core module entrypoint."""

import json
import os
from typing import Callable, Iterator

import requests

from kayden.events import CoreEvent, CoreTextDelta, CoreToolCall, CoreUsage
from kayden.interfaces import CoreInterface
from kayden.klogger import get_logger
from kayden.modules.core.chatgpt.auth import ChatGPTAuth, ChatGPTAuthError, RESOURCE


RESPONSES_URL = f"{RESOURCE}/responses"
MODELS_URL = f"{RESOURCE}/models"
USAGE_LIMIT_CODES = {
    "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable",
}


def _response_error(event: dict) -> tuple[str, str]:
    response = event.get("response")
    error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        error = event.get("error")
    if not isinstance(error, dict):
        return "unknown_error", "ChatGPT request failed"
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else "unknown_error",
        message if isinstance(message, str) else "ChatGPT request failed",
    )


class Module(CoreInterface):
    """Use a ChatGPT Plus/Pro plan through OpenAI's supported OAuth route."""

    runtime_kind = "cloud"
    reports_usage = True

    def __init__(self, model=None, auth=None, session=None, **kwargs):
        self.log = get_logger("ChatGPT")
        self._session = session or requests.Session()
        self._owns_session = session is None
        self.auth = auth or ChatGPTAuth(session=self._session)
        self._interrupted = False
        self.last_usage = None

        requested_model = model or os.environ.get("KAYDEN_CHATGPT_MODEL")
        available = self._list_models()
        self._available_models = available
        if requested_model:
            matching = [item for item in available if item["slug"] == requested_model]
            if not matching:
                raise ValueError(
                    f"ChatGPT model '{requested_model}' is unavailable for this account"
                )
            selected = matching[0]
        elif available:
            selected = available[0]
        else:
            raise ValueError("No ChatGPT models are available for this account")
        self.model = selected["slug"]
        self.model_display_name = selected["display_name"]
        self.log.info(
            "Using ChatGPT plan model: %s (%s)",
            self.model_display_name,
            self.model,
        )

    def available_models(self) -> list[dict[str, str]]:
        return [
            {"id": item["slug"], "name": item["display_name"]}
            for item in self._available_models
        ]

    def select_model(self, model: str) -> None:
        selected = next(
            (item for item in self._available_models if item["slug"] == model),
            None,
        )
        if selected is None:
            raise ValueError(
                f"ChatGPT model '{model}' is unavailable for this account"
            )
        self.model = selected["slug"]
        self.model_display_name = selected["display_name"]
        self.log.info(
            "Switched ChatGPT plan model: %s (%s)",
            self.model_display_name,
            self.model,
        )

    def _request(self, method: str, url: str, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.auth.access_token()}"
        response = getattr(self._session, method)(url, headers=headers, **kwargs)
        if getattr(response, "status_code", None) != 401:
            return response
        response.close()
        headers["Authorization"] = (
            f"Bearer {self.auth.access_token(force_refresh=True)}"
        )
        return getattr(self._session, method)(url, headers=headers, **kwargs)

    def _list_models(self) -> list[dict]:
        try:
            response = self._request("get", MODELS_URL, timeout=30)
            try:
                response.raise_for_status()
                data = response.json()
            finally:
                response.close()
        except (requests.RequestException, ChatGPTAuthError, ValueError) as error:
            raise ValueError(f"Could not list ChatGPT models: {error}") from error
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            raise ValueError("ChatGPT model list had an unexpected format")
        visible = []
        for item in models:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            slug = item.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            display_name = item.get("display_name")
            visible.append({
                "slug": slug,
                "display_name": (
                    display_name
                    if isinstance(display_name, str) and display_name
                    else slug
                ),
            })
        return visible

    @staticmethod
    def _function_tools(schemas: list[dict] | None) -> list[dict]:
        converted = []
        for schema in schemas or []:
            if not isinstance(schema, dict) or schema.get("type") != "function":
                continue
            function = schema.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            tool = {"type": "function", "name": name}
            for key in ("description", "parameters"):
                if key in function:
                    tool[key] = function[key]
            converted.append(tool)
        return converted

    @classmethod
    def _request_body(
        cls,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> dict:
        instructions = []
        input_items = []
        converted_tools = cls._function_tools(tools)
        if converted_tools:
            input_items.append({
                "type": "additional_tools",
                "role": "developer",
                "tools": converted_tools,
            })

        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                if isinstance(content, str) and content:
                    instructions.append(content)
                continue
            if role == "tool":
                call_id = message.get("tool_call_id")
                if isinstance(call_id, str) and isinstance(content, str):
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": content,
                    })
                continue
            if role == "assistant":
                if isinstance(content, str) and content:
                    input_items.append({"role": "assistant", "content": content})
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        continue
                    call_id = call.get("id")
                    name = function.get("name")
                    arguments = function.get("arguments")
                    if all(isinstance(value, str) for value in (call_id, name, arguments)):
                        input_items.append({
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments,
                        })
                continue
            if role in {"user", "developer"} and isinstance(content, str):
                input_items.append({"role": role, "content": content})

        body = {
            "model": model,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if instructions:
            body["instructions"] = "\n\n".join(instructions)
        return body

    @staticmethod
    def _usage(response: dict) -> dict | None:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return None
        normalized = dict(usage)
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, int):
            normalized["prompt_tokens"] = input_tokens
        if isinstance(output_tokens, int):
            normalized["completion_tokens"] = output_tokens
        return normalized

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

        payload = self._request_body(self.model, messages, tools)
        try:
            response = self._request(
                "post",
                RESPONSES_URL,
                headers={
                    "Accept": "text/event-stream",
                    "Content-Type": "application/json",
                },
                json=payload,
                stream=True,
                timeout=(15, 300),
            )
        except (requests.RequestException, ChatGPTAuthError) as error:
            self.log.error("ChatGPT request failed: %s", error)
            yield CoreTextDelta("[ChatGPT request failed — check your connection]")
            return

        completed = False
        terminal_error = False
        calls = []
        try:
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                self.log.error(
                    "ChatGPT API error: %s — %s", error, response.text[:500]
                )
                yield CoreTextDelta("[ChatGPT plan request was rejected — check logs]")
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
                    self.log.warning("Ignoring invalid ChatGPT stream data: %s", error)
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[len("data:"):].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as error:
                    self.log.warning("Ignoring malformed ChatGPT event: %s", error)
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")

                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        yield CoreTextDelta(delta)
                elif event_type == "response.output_item.done":
                    item = event.get("item")
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        call_id = item.get("call_id")
                        name = item.get("name")
                        arguments = item.get("arguments")
                        if all(
                            isinstance(value, str) and value
                            for value in (call_id, name, arguments)
                        ):
                            calls.append((call_id, name, arguments))
                        else:
                            self.log.error("ChatGPT returned an incomplete tool call")
                            terminal_error = True
                            yield CoreTextDelta(" [invalid tool request]")
                            break
                elif event_type == "response.completed":
                    completed = True
                    response_data = event.get("response")
                    if isinstance(response_data, dict):
                        usage = self._usage(response_data)
                        if usage:
                            self.last_usage = usage
                            yield CoreUsage(usage)
                    break
                elif event_type in {"response.failed", "response.incomplete", "error"}:
                    code, message = _response_error(event)
                    self.log.error("ChatGPT response failed: %s — %s", code, message)
                    if code in USAGE_LIMIT_CODES:
                        yield CoreTextDelta(
                            "[ChatGPT plan usage is currently unavailable — "
                            "check ChatGPT Settings → Usage]"
                        )
                    else:
                        yield CoreTextDelta(f"[ChatGPT response failed: {code}]")
                    terminal_error = True
                    break
        except requests.RequestException as error:
            self.log.error("ChatGPT connection dropped mid-response: %s", error)
            yield CoreTextDelta(" [connection dropped]")
            terminal_error = True
        finally:
            response.close()

        if not completed and not terminal_error and not self._interrupted:
            self.log.error("ChatGPT stream ended without response.completed")
            yield CoreTextDelta(" [incomplete ChatGPT response]")
            return
        if completed and not terminal_error:
            for call_id, name, arguments in calls:
                yield CoreToolCall(call_id, name, arguments)

    def stop(self) -> None:
        self.auth.close()
        if self._owns_session:
            self._session.close()
