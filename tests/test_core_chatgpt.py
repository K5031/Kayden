import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlparse

import requests

from kayden.events import CoreTextDelta, CoreToolCall, CoreUsage
from kayden.modules.core.chatgpt.auth import (
    AUTH_ISSUER,
    REQUIRED_SCOPE,
    RESOURCE,
    ChatGPTAuth,
    ChatGPTAuthError,
)
from kayden.modules.core.chatgpt import Module as ChatGPTCore
from kayden.modules.core.chatgpt.credentials import CredentialStore


DISCOVERY = {
    "issuer": AUTH_ISSUER,
    "authorization_endpoint": f"{AUTH_ISSUER}/api/accounts/authorize",
    "token_endpoint": f"{AUTH_ISSUER}/api/accounts/oauth/token",
    "jwks_uri": f"{AUTH_ISSUER}/.well-known/jwks.json",
}


class _Response:
    def __init__(self, data=None, events=(), status_code=200, text=""):
        self.data = data
        self.events = list(events)
        self.status_code = status_code
        self.text = text
        self.headers = {}
        self.closed = False

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_lines(self):
        return iter(self.events)

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, gets=(), posts=()):
        self.gets = list(gets)
        self.posts = list(posts)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.gets.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.posts.pop(0)

    def close(self):
        self.closed = True


class _Auth:
    def __init__(self, tokens=("access-token",)):
        self.tokens = list(tokens)
        self.calls = []
        self.closed = False

    def access_token(self, force_refresh=False):
        self.calls.append(force_refresh)
        if len(self.tokens) > 1:
            return self.tokens.pop(0)
        return self.tokens[0]

    def close(self):
        self.closed = True


def _sse(event):
    return f"data: {json.dumps(event)}".encode()


def _core(session, auth=None):
    core = ChatGPTCore.__new__(ChatGPTCore)
    core.log = logging.getLogger("test-chatgpt")
    core._session = session
    core._owns_session = False
    core.auth = auth or _Auth()
    core.model = "test-model"
    core.model_display_name = "Test Model"
    core._available_models = [
        {"slug": "test-model", "display_name": "Test Model"},
    ]
    core._interrupted = False
    core.last_usage = None
    return core


class CredentialStoreTests(unittest.TestCase):
    def test_host_and_profile_are_persisted_with_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chatgpt.json"
            store = CredentialStore(path)

            host_id = store.host_id()
            store.save_profile({"client_id": "oaiapp_test"})

            reopened = CredentialStore(path)
            self.assertEqual(reopened.host_id(), host_id)
            self.assertEqual(
                reopened.profile(), {"client_id": "oaiapp_test"}
            )
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ChatGPTAuthTests(unittest.TestCase):
    def _auth(self, directory, session=None):
        auth = ChatGPTAuth(
            session=session or _Session(),
            store=CredentialStore(Path(directory) / "chatgpt.json"),
            browser_open=lambda url: True,
            token_verifier=lambda token, client_id, nonce: {
                "sub": "subject-1",
                "email": "person@example.com",
                "name": "Person",
                "nonce": nonce,
            },
        )
        auth._discovery_cache = dict(DISCOVERY)
        return auth

    def test_authorization_uses_dynamic_registration_pkce_and_plan_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = self._auth(directory)

            attempt, url = auth.create_authorization(
                "http://127.0.0.1:1234/auth/callback"
            )

            parameters = parse_qs(urlparse(url).query)
            self.assertEqual(parameters["client_id"], ["dynamic_agent_client"])
            self.assertEqual(parameters["agent_name_hint"], ["Kayden"])
            self.assertEqual(parameters["resource"], [RESOURCE])
            self.assertIn(REQUIRED_SCOPE, parameters["scope"][0].split())
            self.assertEqual(parameters["state"], [attempt.state])
            expected_challenge = (
                __import__("base64").urlsafe_b64encode(
                    hashlib.sha256(attempt.code_verifier.encode()).digest()
                ).rstrip(b"=").decode()
            )
            self.assertEqual(parameters["code_challenge"], [expected_challenge])

    def test_callback_exchanges_code_validates_identity_and_saves_profile(self):
        tokens = {
            "id_token": "identity-token",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": "openid profile email offline_access resource.invoke "
            + REQUIRED_SCOPE,
        }
        session = _Session(posts=[_Response(tokens)])
        with tempfile.TemporaryDirectory() as directory:
            auth = self._auth(directory, session)
            attempt, _ = auth.create_authorization(
                "http://127.0.0.1:1234/auth/callback"
            )

            profile = auth.exchange_callback(attempt, {
                "state": attempt.state,
                "code": "authorization-code",
                "client_id": "oaiapp_issued",
            })

            self.assertEqual(profile["subject"], "subject-1")
            self.assertEqual(profile["client_id"], "oaiapp_issued")
            self.assertEqual(auth.store.profile()["access_token"], "access-token")
            _, _, request = session.calls[-1]
            self.assertEqual(request["data"]["code_verifier"], attempt.code_verifier)
            self.assertEqual(request["data"]["resource"], RESOURCE)

    def test_callback_rejects_mismatched_state_before_token_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _Session()
            auth = self._auth(directory, session)
            attempt, _ = auth.create_authorization(
                "http://127.0.0.1:1234/auth/callback"
            )

            with self.assertRaisesRegex(ChatGPTAuthError, "state"):
                auth.exchange_callback(attempt, {
                    "state": "wrong",
                    "code": "authorization-code",
                    "client_id": "oaiapp_issued",
                })

            self.assertFalse(session.calls)

    def test_returning_sign_in_cannot_replace_profile_with_another_account(self):
        tokens = {
            "id_token": "identity-token",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
            "scope": REQUIRED_SCOPE,
        }
        session = _Session(posts=[_Response(tokens)])
        with tempfile.TemporaryDirectory() as directory:
            auth = self._auth(directory, session)
            existing = {
                "client_id": "oaiapp_issued",
                "subject": "different-subject",
            }
            attempt, _ = auth.create_authorization(
                "http://127.0.0.1:1234/auth/callback", existing
            )

            with self.assertRaisesRegex(ChatGPTAuthError, "different account"):
                auth.exchange_callback(attempt, {
                    "state": attempt.state,
                    "code": "authorization-code",
                })

    def test_expired_access_token_is_refreshed_and_rotated(self):
        refresh = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "scope": REQUIRED_SCOPE,
        }
        session = _Session(posts=[_Response(refresh)])
        with tempfile.TemporaryDirectory() as directory:
            auth = self._auth(directory, session)
            auth.store.save_profile({
                "client_id": "oaiapp_issued",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "expires_at": time.time() - 1,
                "scopes": [REQUIRED_SCOPE],
            })

            self.assertEqual(auth.access_token(), "new-access")
            saved = auth.store.profile()
            self.assertEqual(saved["refresh_token"], "new-refresh")
            _, _, request = session.calls[-1]
            self.assertEqual(request["data"]["grant_type"], "refresh_token")
            self.assertEqual(request["data"]["refresh_token"], "old-refresh")


class ChatGPTCoreTests(unittest.TestCase):
    def test_discovers_visible_models_and_uses_first_by_default(self):
        response = _Response({"models": [
            {"slug": "hidden", "display_name": "Hidden", "visibility": "hide"},
            {"slug": "gpt-test", "display_name": "GPT Test", "visibility": "list"},
        ]})
        session = _Session(gets=[response])
        auth = _Auth()

        core = ChatGPTCore(session=session, auth=auth)

        self.assertEqual(core.model, "gpt-test")
        self.assertTrue(response.closed)
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer access-token")

    def test_exposes_and_switches_between_visible_models(self):
        response = _Response({"models": [
            {"slug": "gpt-fast", "display_name": "GPT Fast", "visibility": "list"},
            {"slug": "gpt-deep", "display_name": "GPT Deep", "visibility": "list"},
        ]})
        core = ChatGPTCore(session=_Session(gets=[response]), auth=_Auth())

        self.assertEqual(core.available_models(), [
            {"id": "gpt-fast", "name": "GPT Fast"},
            {"id": "gpt-deep", "name": "GPT Deep"},
        ])

        core.select_model("gpt-deep")

        self.assertEqual(core.model, "gpt-deep")
        self.assertEqual(core.model_display_name, "GPT Deep")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            core.select_model("gpt-hidden")

    def test_builds_supported_responses_input_with_tools_and_history(self):
        schemas = [{
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo text",
                "parameters": {"type": "object"},
            },
        }]
        messages = [
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "say hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text":"hi"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "hi"},
        ]

        body = ChatGPTCore._request_body("gpt-test", messages, schemas)

        self.assertEqual(body["instructions"], "Be concise")
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["input"][0], {
            "type": "additional_tools",
            "role": "developer",
            "tools": [{
                "type": "function",
                "name": "echo",
                "description": "Echo text",
                "parameters": {"type": "object"},
            }],
        })
        self.assertIn({
            "type": "function_call",
            "call_id": "call_1",
            "name": "echo",
            "arguments": '{"text":"hi"}',
        }, body["input"])
        self.assertIn({
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "hi",
        }, body["input"])

    def test_streams_text_usage_and_completed_tool_call(self):
        response = _Response(events=[
            _sse({"type": "response.output_text.delta", "delta": "hello"}),
            _sse({
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "echo",
                    "arguments": '{"text":"hi"}',
                },
            }),
            _sse({
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                },
            }),
        ])
        session = _Session(posts=[response])

        events = list(_core(session).stream([
            {"role": "user", "content": "hi"},
        ], max_output_tokens=2048))

        self.assertEqual(events, [
            CoreTextDelta("hello"),
            CoreUsage({
                "input_tokens": 12,
                "output_tokens": 3,
                "prompt_tokens": 12,
                "completion_tokens": 3,
            }),
            CoreToolCall("call_1", "echo", '{"text":"hi"}'),
        ])
        _, url, request = session.calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertFalse(request["json"]["store"])
        self.assertTrue(request["json"]["stream"])
        self.assertEqual(request["json"]["max_output_tokens"], 2048)
        self.assertTrue(response.closed)

    def test_stream_must_reach_response_completed(self):
        response = _Response(events=[
            _sse({"type": "response.output_text.delta", "delta": "partial"}),
        ])

        events = list(_core(_Session(posts=[response])).stream([]))

        self.assertEqual(events, [
            CoreTextDelta("partial"),
            CoreTextDelta(" [incomplete ChatGPT response]"),
        ])

    def test_unauthorized_request_refreshes_once(self):
        first = _Response(status_code=401)
        second = _Response(events=[
            _sse({"type": "response.completed", "response": {}}),
        ])
        auth = _Auth(("old-token", "new-token"))
        session = _Session(posts=[first, second])

        events = list(_core(session, auth).stream([]))

        self.assertEqual(events, [])
        self.assertEqual(auth.calls, [False, True])
        self.assertTrue(first.closed)
        self.assertEqual(
            session.calls[1][2]["headers"]["Authorization"],
            "Bearer new-token",
        )


if __name__ == "__main__":
    unittest.main()
