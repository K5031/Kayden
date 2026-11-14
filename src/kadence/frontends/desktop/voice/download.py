"""The speech models: which ones, whether they are there, and getting them. They are not
shipped with Kadence; the app asks before it downloads them."""

from __future__ import annotations

import asyncio
import shutil
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx2

_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class Model:
    name: str  # the file, or the folder its archive unpacks to, in the voice folder
    url: str
    size: int  # of the download, in bytes: for the question and the progress
    archive: bool = True  # a .tar.bz2 holding the folder `name`


MODELS = (
    Model("silero_vad.onnx", f"{_RELEASES}/asr-models/silero_vad.onnx", 643_854, archive=False),
    Model("sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
          f"{_RELEASES}/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
          482_440_461),
    Model("kokoro-multi-lang-v1_0", f"{_RELEASES}/tts-models/kokoro-multi-lang-v1_0.tar.bz2",
          349_000_000),
)


def missing(folder: Path, models: Sequence[Model] = MODELS) -> list[Model]:
    """The models not in `folder` yet. Blocking."""
    return [m for m in models if not (folder / m.name).exists()]


def megabytes(models: Sequence[Model]) -> int:
    return round(sum(m.size for m in models) / 1e6)


async def fetch(folder: Path, models: Sequence[Model], progress: Callable[[float], object],
                *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
    """Download `models` into `folder`, calling `progress` with the fraction done. A model
    is in place only once it is whole, so a download that was cut off starts again.
    httpx2.HTTPError or OSError when it fails."""
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    total, done = sum(m.size for m in models) or 1, 0
    async with httpx2.AsyncClient(transport=transport, follow_redirects=True,
                                  timeout=httpx2.Timeout(30, read=120)) as client:
        for model in models:
            part = folder / f"{model.name}.part"
            async with client.stream("GET", model.url) as response:
                response.raise_for_status()
                with await asyncio.to_thread(part.open, "wb") as out:
                    async for chunk in response.aiter_bytes(CHUNK):
                        await asyncio.to_thread(out.write, chunk)
                        done += len(chunk)
                        progress(min(done / total, 1.0))
            await asyncio.to_thread(_put_in_place, part, folder, model)
    progress(1.0)


def _put_in_place(part: Path, folder: Path, model: Model) -> None:
    if not model.archive:
        part.replace(folder / model.name)
        return
    unpacking = folder / f"{model.name}.unpacking"
    shutil.rmtree(unpacking, ignore_errors=True)
    with tarfile.open(part, "r:bz2") as archive:
        archive.extractall(unpacking, filter="data")
    (unpacking / model.name).replace(folder / model.name)
    shutil.rmtree(unpacking, ignore_errors=True)
    part.unlink()
