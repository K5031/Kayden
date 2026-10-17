import base64
import hashlib
import hmac
import queue
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from klogger import get_logger
from modules.core.chatgpt.credentials import CredentialStore


AUTH_ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{AUTH_ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + REQUIRED_SCOPE


class ChatGPTAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorizationAttempt:
    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    registering: bool
    expected_subject: str | None


def _callback_handler(results):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_error(404)
                return
            values = {
                key: items[0]
                for key, items in parse_qs(
                    parsed.query, keep_blank_values=True
                ).items()
                if items
            }
            try:
                results.put_nowait(values)
            except queue.Full:
                self.send_error(409)
                return
            body = (
                b"<!doctype html><title>Kayden connected</title>"
                b"<h1>Sign-in received</h1>"
                b"<p>You can close this tab and return to Kayden.</p>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    return CallbackHandler


class ChatGPTAuth:
    """OpenAI's local-app OAuth flow for ChatGPT plan usage."""

    def __init__(
        self,
        session=None,
        store=None,
        browser_open=None,
        token_verifier=None,
    ):
        self.log = get_logger("ChatGPT")
        self._session = session or requests.Session()
        self._owns_session = session is None
        self.store = store or CredentialStore()
        self._browser_open = browser_open or webbrowser.open
        self._token_verifier = token_verifier
        self._discovery_cache = None
        self._jwks_cache = None
        self._refresh_lock = threading.Lock()

    @staticmethod
    def _response_json(response, operation: str) -> dict:
        try:
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                request_id = response.headers.get(
                    "openai-request-id"
                ) or response.headers.get("x-request-id")
                suffix = f" (request {request_id})" if request_id else ""
                raise ChatGPTAuthError(
                    f"ChatGPT {operation} failed{suffix}"
                ) from error
            try:
                data = response.json()
            except ValueError as error:
                raise ChatGPTAuthError(
                    f"ChatGPT {operation} returned invalid JSON"
                ) from error
        finally:
            response.close()
        if not isinstance(data, dict):
            raise ChatGPTAuthError(f"ChatGPT {operation} returned invalid data")
        return data

    def discovery(self) -> dict:
        if self._discovery_cache is None:
            response = self._session.get(DISCOVERY_URL, timeout=15)
            data = self._response_json(response, "discovery")
            if data.get("issuer") != AUTH_ISSUER:
                raise ChatGPTAuthError("ChatGPT discovery returned an unexpected issuer")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                value = data.get(key)
                if not isinstance(value, str) or not value.startswith(AUTH_ISSUER + "/"):
                    raise ChatGPTAuthError(
                        f"ChatGPT discovery returned an invalid {key}"
                    )
            self._discovery_cache = data
        return dict(self._discovery_cache)

    @staticmethod
    def _random_value(bytes_count=32) -> str:
        return secrets.token_urlsafe(bytes_count)

    def create_authorization(
        self,
        redirect_uri: str,
        profile: dict | None = None,
    ) -> tuple[AuthorizationAttempt, str]:
        verifier = self._random_value(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        registering = profile is None
        client_id = DYNAMIC_CLIENT_ID if registering else profile.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise ChatGPTAuthError("Saved ChatGPT registration has no client ID")
        attempt = AuthorizationAttempt(
            state=self._random_value(),
            nonce=self._random_value(),
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=client_id,
            registering=registering,
            expected_subject=(
                None
                if registering or not isinstance(profile.get("subject"), str)
                else profile["subject"]
            ),
        )
        parameters = {
            "client_id": client_id,
            "ext_agent_host_id": self.store.host_id(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if registering:
            parameters["agent_name_hint"] = "Kayden"
        authorization_endpoint = self.discovery()["authorization_endpoint"]
        return attempt, authorization_endpoint + "?" + urlencode(parameters)

    def _jwks(self, refresh=False) -> dict:
        if refresh or self._jwks_cache is None:
            response = self._session.get(self.discovery()["jwks_uri"], timeout=15)
            data = self._response_json(response, "key discovery")
            if not isinstance(data.get("keys"), list):
                raise ChatGPTAuthError("ChatGPT key discovery returned no keys")
            self._jwks_cache = data
        return self._jwks_cache

    def _verify_id_token(self, token: str, client_id: str, nonce: str) -> dict:
        if self._token_verifier is not None:
            return self._token_verifier(token, client_id, nonce)
        try:
            import jwt
        except ModuleNotFoundError as error:
            raise ChatGPTAuthError(
                "ChatGPT sign-in dependencies are missing; reinstall Kayden"
            ) from error

        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            key_id = header.get("kid")
            if algorithm not in {"RS256", "ES256"} or not isinstance(key_id, str):
                raise ChatGPTAuthError("ChatGPT ID token uses an unsupported key")
            keys = self._jwks()["keys"]
            matches = [key for key in keys if key.get("kid") == key_id]
            if not matches:
                matches = [
                    key for key in self._jwks(refresh=True)["keys"]
                    if key.get("kid") == key_id
                ]
            if len(matches) != 1:
                raise ChatGPTAuthError("ChatGPT ID token signing key was not found")
            signing_key = jwt.PyJWK.from_dict(matches[0], algorithm=algorithm).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=[algorithm],
                audience=client_id,
                issuer=self.discovery()["issuer"],
                leeway=5,
                options={"require": ["sub", "exp", "iat", "nonce"]},
            )
        except ChatGPTAuthError:
            raise
        except Exception as error:
            raise ChatGPTAuthError("ChatGPT ID token validation failed") from error
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ChatGPTAuthError("ChatGPT ID token nonce did not match")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ChatGPTAuthError("ChatGPT ID token contained no subject")
        return claims

    @staticmethod
    def _scopes(value) -> list[str]:
        if isinstance(value, str):
            return sorted(set(value.split()))
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return sorted(set(value))
        return []

    def exchange_callback(
        self,
        attempt: AuthorizationAttempt,
        parameters: dict,
    ) -> dict:
        returned_state = parameters.get("state")
        if not isinstance(returned_state, str) or not hmac.compare_digest(
            returned_state, attempt.state
        ):
            raise ChatGPTAuthError("ChatGPT sign-in state did not match")
        if parameters.get("error"):
            raise ChatGPTAuthError("ChatGPT sign-in was declined or could not complete")
        code = parameters.get("code")
        if not isinstance(code, str) or not code:
            raise ChatGPTAuthError("ChatGPT callback contained no authorization code")

        returned_client_id = parameters.get("client_id")
        if attempt.registering:
            client_id = returned_client_id
            if (
                not isinstance(client_id, str)
                or not client_id
                or client_id == DYNAMIC_CLIENT_ID
            ):
                raise ChatGPTAuthError("ChatGPT registration returned no client ID")
        else:
            if returned_client_id and returned_client_id != attempt.client_id:
                raise ChatGPTAuthError("ChatGPT callback changed the registered client ID")
            client_id = attempt.client_id

        response = self._session.post(
            self.discovery()["token_endpoint"],
            headers={"Accept": "application/json"},
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": attempt.code_verifier,
                "redirect_uri": attempt.redirect_uri,
                "resource": RESOURCE,
            },
            timeout=30,
        )
        tokens = self._response_json(response, "token exchange")
        required = ("id_token", "access_token", "refresh_token")
        if any(not isinstance(tokens.get(key), str) or not tokens[key] for key in required):
            raise ChatGPTAuthError("ChatGPT token response was incomplete")
        claims = self._verify_id_token(tokens["id_token"], client_id, attempt.nonce)
        if (
            attempt.expected_subject is not None
            and not hmac.compare_digest(
                str(claims.get("sub", "")), attempt.expected_subject
            )
        ):
            raise ChatGPTAuthError(
                "ChatGPT sign-in returned a different account than expected"
            )
        scopes = self._scopes(tokens.get("scope"))
        expires_in = tokens.get("expires_in", 3600)
        if not isinstance(expires_in, int) or isinstance(expires_in, bool):
            raise ChatGPTAuthError("ChatGPT token expiry was invalid")
        now = time.time()
        profile = {
            "issuer": self.discovery()["issuer"],
            "subject": claims["sub"],
            "email": claims.get("email"),
            "name": claims.get("name"),
            "client_id": client_id,
            "ext_agent_host_id": self.store.host_id(),
            "id_token": tokens["id_token"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": tokens.get("token_type", "Bearer"),
            "expires_in": expires_in,
            "expires_at": now + expires_in,
            "earliest_refresh_at": tokens.get("earliest_refresh_at"),
            "scopes": scopes,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.save_profile(profile)
        if REQUIRED_SCOPE not in scopes:
            raise ChatGPTAuthError("ChatGPT plan usage permission was not granted")
        return profile

    def sign_in(self, profile: dict | None = None, timeout=300) -> dict:
        if self._token_verifier is None:
            try:
                import jwt  # noqa: F401
            except ModuleNotFoundError as error:
                raise ChatGPTAuthError(
                    "ChatGPT sign-in dependencies are missing; reinstall Kayden"
                ) from error

        results = queue.Queue(maxsize=1)
        server = HTTPServer(("127.0.0.1", 0), _callback_handler(results))
        server.timeout = 1
        try:
            redirect_uri = (
                f"http://127.0.0.1:{server.server_port}/auth/callback"
            )
            attempt, url = self.create_authorization(redirect_uri, profile)
            self.log.info("Opening Continue with ChatGPT in your browser")
            try:
                opened = self._browser_open(url)
            except Exception as error:
                self.log.warning("Could not open the system browser: %s", error)
                opened = False
            if not opened:
                self.log.info("Open this URL to continue: %s", url)
            deadline = time.monotonic() + timeout
            while results.empty() and time.monotonic() < deadline:
                server.handle_request()
            if results.empty():
                raise ChatGPTAuthError("ChatGPT sign-in timed out")
            return self.exchange_callback(attempt, results.get_nowait())
        finally:
            server.server_close()

    def ensure_profile(self) -> dict:
        profile = self.store.profile()
        if profile is None:
            return self.sign_in()
        if REQUIRED_SCOPE not in self._scopes(profile.get("scopes")):
            return self.sign_in(profile)
        return profile

    def refresh(self, force=False) -> dict:
        with self._refresh_lock:
            profile = self.ensure_profile()
            expires_at = profile.get("expires_at", 0)
            if not force and isinstance(expires_at, (int, float)) and expires_at > time.time() + 60:
                return profile
            refresh_token = profile.get("refresh_token")
            client_id = profile.get("client_id")
            if not isinstance(refresh_token, str) or not isinstance(client_id, str):
                return self.sign_in(profile)
            response = self._session.post(
                self.discovery()["token_endpoint"],
                headers={"Accept": "application/json"},
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "resource": RESOURCE,
                },
                timeout=30,
            )
            try:
                tokens = self._response_json(response, "token refresh")
            except ChatGPTAuthError:
                if getattr(response, "status_code", None) in {400, 401}:
                    return self.sign_in(profile)
                raise
            access_token = tokens.get("access_token")
            replacement = tokens.get("refresh_token")
            if not isinstance(access_token, str) or not isinstance(replacement, str):
                raise ChatGPTAuthError("ChatGPT refresh response was incomplete")
            scopes = self._scopes(tokens.get("scope") or profile.get("scopes"))
            if REQUIRED_SCOPE not in scopes:
                raise ChatGPTAuthError("ChatGPT plan usage permission is no longer active")
            expires_in = tokens.get("expires_in", 3600)
            if not isinstance(expires_in, int) or isinstance(expires_in, bool):
                raise ChatGPTAuthError("ChatGPT refresh expiry was invalid")
            updated = dict(profile)
            updated.update({
                "access_token": access_token,
                "refresh_token": replacement,
                "token_type": tokens.get("token_type", profile.get("token_type", "Bearer")),
                "expires_in": expires_in,
                "expires_at": time.time() + expires_in,
                "earliest_refresh_at": tokens.get("earliest_refresh_at"),
                "scopes": scopes,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            })
            if isinstance(tokens.get("id_token"), str):
                updated["id_token"] = tokens["id_token"]
            self.store.save_profile(updated)
            return updated

    def access_token(self, force_refresh=False) -> str:
        profile = self.refresh(force=force_refresh)
        token = profile.get("access_token")
        if not isinstance(token, str) or not token:
            raise ChatGPTAuthError("ChatGPT account has no access token")
        return token

    def close(self) -> None:
        if self._owns_session:
            self._session.close()
