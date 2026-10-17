import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import chromadb
import requests
import torch
from sentence_transformers import SentenceTransformer

from kayden.config import BASE_DIR
from kayden.interfaces import MemoryInterface
from kayden.klogger import get_logger
from kayden.modules.memory.remis.transcript import EphemeralTranscript
from kayden.utils import resolve_model_path

DEFAULT_MODEL = "gemma-4-E4B-it-Q4_K_M.gguf"
DEFAULT_N_GPU_LAYERS = 5
DEFAULT_N_CTX = 4096

EXTRACTION_PROMPT = """Extract only facts the user explicitly stated that would still be worth knowing weeks from now — their name, job, relationships, preferences, or ongoing plans.

Skip greetings, small talk, questions, and anything about the assistant itself. If nothing qualifies, return an empty list.

Always split distinct facts into separate entries in the list — never combine multiple facts into one sentence, even if they were stated together.

Examples:
Input: hi
Output: {"facts": []}
Input: my name is Alex and I work as a nurse
Output: {"facts": ["User's name is Alex", "User works as a nurse"]}
Input: I'm K5031 and I go to UCL
Output: {"facts": ["User's name is K5031", "User attends UCL"]}
Input: what's your name?
Output: {"facts": []}

Conversation:
{conversation}

Return only valid JSON: {"facts": [...]}"""

CONFLICT_CHECK_PROMPT = """You will see an EXISTING fact and a NEW fact about the user. Decide the relationship between them:

SAME     — they express the same fact, just worded differently (same subject, same value)
CONFLICT — they're about the same specific subject, but the value changed (a correction)
DISTINCT — they're about different subjects or facts entirely, even if superficially similar wording

Answer with exactly one word: SAME, CONFLICT, or DISTINCT.

Examples:
EXISTING: User's name is K5031
NEW: User is called K5031
Answer: SAME

EXISTING: User's name is K5031
NEW: User's name is Alex
Answer: CONFLICT

EXISTING: User works as a teacher
NEW: User works as a software engineer
Answer: CONFLICT

EXISTING: User goes to UCL
NEW: User studies computer science at UCL
Answer: DISTINCT

EXISTING: User has a dog named Rex
NEW: User has a cat named Milo
Answer: DISTINCT

EXISTING: User's name is Alex
NEW: Friend's name is Alex
Answer: DISTINCT

EXISTING: {existing}
NEW: {new}
Answer:"""


def _format_conversation(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        if m["role"] in ("user", "assistant"):
            lines.append(f"{m['role']}: {m['content']}")
    return "\n".join(lines)


def _extract_json_facts(raw_text: str) -> list[str]:
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
        facts = data.get("facts", [])
        return [f for f in facts if isinstance(f, str) and f.strip()]
    except (json.JSONDecodeError, AttributeError):
        return []


class Module(EphemeralTranscript, MemoryInterface):
    def __init__(self, model_path=DEFAULT_MODEL, n_gpu_layers=DEFAULT_N_GPU_LAYERS,
                 n_ctx=DEFAULT_N_CTX,
                 collection_name="k_memory",
                 embed_model_name="Qwen/Qwen3-Embedding-0.6B",
                 dedup_distance: float = 0.05,
                 conflict_band_upper: float = 0.35,
                 port: int = 8080):
        self.name = "Remis"
        self.log = get_logger(self.name)
        self.user_id = "user"
        self.port = port
        self.base_url = f"http://localhost:{port}"
        self.dedup_distance = dedup_distance
        self.conflict_band_upper = conflict_band_upper
        self.server = None
        self._queue = None
        self._worker = None
        self._stopped = False
        self._initialize_transcript()

        resolved_path = resolve_model_path(model_path)
        self.model_path = resolved_path

        try:
            self.log.info("Extraction model: %s", resolved_path)
            self.server = subprocess.Popen(
                [sys.executable, "-m", "llama_cpp.server",
                 "--model", resolved_path,
                 "--port", str(port),
                 "--n_gpu_layers", str(n_gpu_layers),
                 "--n_ctx", str(n_ctx),
                 "--flash_attn", "True",
                 "--verbose", "False"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._wait_for_server()

            self.client = chromadb.PersistentClient(path=os.path.join(BASE_DIR, "data", "chroma"))
            self.collection = self.client.get_or_create_collection(
                collection_name, metadata={"hnsw:space": "cosine"}
            )
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self.embedder = SentenceTransformer(embed_model_name, device=device)

            self._queue = queue.Queue()
            self._worker = threading.Thread(target=self._process_queue, daemon=True)
            self._worker.start()
        except Exception:
            self._stop_server()
            raise

    def _wait_for_server(self, timeout: int = 60):
        start = time.time()
        while time.time() - start < timeout:
            if self.server.poll() is not None:
                raise RuntimeError(
                    f"{self.name} llama-server exited with code {self.server.returncode}"
                )
            try:
                r = requests.get(f"{self.base_url}/v1/models", timeout=1)
                if r.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(1)
        raise TimeoutError(f"{self.name} llama-server failed to start within timeout")

    def _stop_server(self):
        if self.server is None or self.server.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(self.server.pid), signal.SIGTERM)
            else:
                self.server.terminate()
            self.server.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if self.server.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(os.getpgid(self.server.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    self.server.kill()
                self.server.wait()

    def _embed(self, text: str) -> list[float]:
        return self.embedder.encode(text, show_progress_bar=False).tolist()

    def _find_nearest(self, embedding: list[float]):
        if self.collection.count() == 0:
            return None
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=1,
            where={"user_id": self.user_id},
        )
        ids = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        if not ids:
            return None
        return ids[0], metadatas[0].get("data", ""), distances[0]

    def _check_conflict(self, existing_text: str, new_text: str) -> str:
        prompt = CONFLICT_CHECK_PROMPT.replace("{existing}", existing_text).replace("{new}", new_text)
        try:
            resp = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": "local-model",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10,
                    "temperature": 0.0,
                },
                timeout=30,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip().upper()
            if "SAME" in raw:
                return "SAME"
            if "CONFLICT" in raw:
                return "CONFLICT"
            return "DISTINCT"
        except Exception as e:
            self.log.warning("conflict check failed, treating as distinct: %s", e)
            return "DISTINCT"

    def _process_queue(self):
        while True:
            messages = self._queue.get()
            try:
                if messages is None:
                    return
                facts = self._extract_and_save(messages)
                if facts:
                    self.log.info("stored: %s", facts)
            except Exception as e:
                self.log.error("store failed: %s", e)
            finally:
                self._queue.task_done()

    def _extract_and_save(self, messages: list[dict]) -> list[str]:
        conversation = _format_conversation(messages)
        if not conversation.strip():
            return []

        prompt = EXTRACTION_PROMPT.replace("{conversation}", conversation)
        resp = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json={
                "model": "local-model",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0.0,
            },
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        facts = _extract_json_facts(raw)
        if not facts:
            return []

        stored_facts = []
        for f in facts:
            emb = self._embed(f)
            nearest = self._find_nearest(emb)

            if nearest is None:
                self._add_fact(f, emb)
                stored_facts.append(f)
                continue

            nearest_id, nearest_text, distance = nearest

            if distance <= self.dedup_distance:
                self.log.info("skipped duplicate: %s", f)
                continue

            if distance <= self.conflict_band_upper:
                verdict = self._check_conflict(nearest_text, f)
                if verdict == "SAME":
                    self.log.info("skipped duplicate (paraphrase): %s", f)
                elif verdict == "CONFLICT":
                    self.log.info("conflict detected — replacing '%s' with '%s'",
                                   nearest_text, f)
                    self.collection.delete(ids=[nearest_id])
                    self._add_fact(f, emb)
                    stored_facts.append(f)
                else:
                    self._add_fact(f, emb)
                    stored_facts.append(f)
                continue

            self._add_fact(f, emb)
            stored_facts.append(f)

        return stored_facts

    def _add_fact(self, text: str, embedding: list[float]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.collection.add(
            ids=[str(uuid.uuid4())],
            embeddings=[embedding],
            metadatas=[{"data": text, "user_id": self.user_id, "created_at": now}],
        )

    def append(self, messages: list[dict]) -> None:
        super().append(messages)
        if self._stopped:
            return
        if any(message.get("role") == "user" for message in messages):
            self._queue.put(messages)

    def recall(self, query: str, max_distance: float = 0.6) -> str:
        if self.collection.count() == 0:
            return ""
        query_embedding = self._embed(query)
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(3, self.collection.count()),
            where={"user_id": self.user_id},
        )
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        facts = [
            m.get("data", "")
            for m, d in zip(metadatas, distances)
            if m.get("data") and d <= max_distance
        ]
        if not facts:
            return ""
        return "\n".join(f"- {f}" for f in facts)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        if self._queue is not None and self._worker is not None:
            self._queue.put(None)
            self._worker.join(timeout=5)
            if self._worker.is_alive():
                self.log.warning("memory worker did not stop promptly; terminating local server")
        self._stop_server()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2)
