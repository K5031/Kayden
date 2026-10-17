import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

from config import CHATGPT_AUTH_PATH


class CredentialStore:
    """Persist one local ChatGPT registration and a stable host identifier."""

    def __init__(self, path=None):
        self.path = Path(path or CHATGPT_AUTH_PATH).expanduser()
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"ChatGPT credentials could not be read: {self.path}"
            ) from error
        if not isinstance(data, dict):
            raise ValueError("ChatGPT credential file must contain an object")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".chatgpt-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(handle.name, 0o600)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def host_id(self) -> str:
        with self._lock:
            state = self._read()
            host_id = state.get("ext_agent_host_id")
            if isinstance(host_id, str) and host_id.startswith("urn:uuid:"):
                return host_id
            host_id = f"urn:uuid:{uuid.uuid4()}"
            state["ext_agent_host_id"] = host_id
            self._write(state)
            return host_id

    def profile(self) -> dict | None:
        with self._lock:
            profile = self._read().get("profile")
            return dict(profile) if isinstance(profile, dict) else None

    def save_profile(self, profile: dict) -> None:
        with self._lock:
            state = self._read()
            state["ext_agent_host_id"] = self.host_id()
            state["profile"] = dict(profile)
            self._write(state)

    def clear_profile(self) -> None:
        with self._lock:
            state = self._read()
            state.pop("profile", None)
            self._write(state)
