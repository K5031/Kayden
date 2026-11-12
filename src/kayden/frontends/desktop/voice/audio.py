"""Sound in and out through Qt Multimedia: the microphone as the engine hears it, and a
speaker that plays what the engine says."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QAudioSource, QMediaDevices

from kayden.frontends.desktop.voice.engine import RATE

_DTYPES = {QAudioFormat.SampleFormat.Int16: (np.int16, 32768.0),
           QAudioFormat.SampleFormat.Int32: (np.int32, 2147483648.0),
           QAudioFormat.SampleFormat.Float: (np.float32, 1.0),
           QAudioFormat.SampleFormat.UInt8: (np.uint8, None)}


def as_heard(data: bytes, rate: int, channels: int,
             sample_format: QAudioFormat.SampleFormat) -> np.ndarray:
    """Raw microphone sound as the engine takes it: float32, mono, at engine.RATE."""
    dtype, scale = _DTYPES[sample_format]
    frame = np.dtype(dtype).itemsize * channels
    raw = np.frombuffer(data[: len(data) - len(data) % frame], dtype=dtype)
    samples = ((raw.astype(np.float32) - 128.0) / 128.0 if scale is None
               else raw.astype(np.float32) / scale)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != RATE and len(samples):
        at = np.arange(0, len(samples), rate / RATE)
        samples = np.interp(at, np.arange(len(samples)), samples).astype(np.float32)
    return samples


class NoMicrophone(Exception):
    """There is no microphone to listen to."""


class Microphone(QObject):
    """Hands what the default microphone hears to `heard`, while started."""

    def __init__(self, heard: Callable[[np.ndarray], object]) -> None:
        super().__init__()
        self._heard = heard
        self._source: QAudioSource | None = None
        self._io: QIODevice | None = None
        self._format = QAudioFormat()

    def start(self) -> None:
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise NoMicrophone("No microphone was found.")
        wanted = QAudioFormat()
        wanted.setSampleRate(RATE)
        wanted.setChannelCount(1)
        wanted.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._format = wanted if device.isFormatSupported(wanted) else device.preferredFormat()
        if self._format.sampleFormat() not in _DTYPES:
            raise NoMicrophone("The microphone's sound format is not supported.")
        self._source = QAudioSource(device, self._format, self)
        self._io = self._source.start()
        if self._io is None or self._source.error() != QAudio.Error.NoError:
            self.stop()
            raise NoMicrophone("The microphone could not be opened.")
        self._io.readyRead.connect(self._read)

    def stop(self) -> None:
        if self._source is not None:
            self._source.stop()
            self._source.deleteLater()
        self._source = self._io = None

    def _read(self) -> None:
        if self._io is None:
            return
        data = self._io.readAll().data()
        if data:
            self._heard(as_heard(data, self._format.sampleRate(), self._format.channelCount(),
                                 self._format.sampleFormat()))


class Speaker(QObject):
    """turns.Player on the default sound output."""

    def __init__(self) -> None:
        super().__init__()
        self._sink: QAudioSink | None = None
        self._done: asyncio.Future[None] | None = None

    async def play(self, samples: np.ndarray, rate: int) -> None:
        self.stop()
        if not len(samples):
            return
        sound = QAudioFormat()
        sound.setSampleRate(rate)
        sound.setChannelCount(1)
        sound.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        buffer = QBuffer(self)
        buffer.setData(QByteArray(pcm))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        sink = self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), sound, self)
        done = self._done = asyncio.get_running_loop().create_future()

        def changed(state: QAudio.State) -> None:
            # Idle: all of it was played. Stopped: stop(), or the device failed.
            if state in (QAudio.State.IdleState, QAudio.State.StoppedState) and not done.done():
                done.set_result(None)

        sink.stateChanged.connect(changed)
        sink.start(buffer)
        try:
            await done
        finally:
            sink.stop()
            sink.deleteLater()
            buffer.deleteLater()
            if self._sink is sink:
                self._sink = None

    def stop(self) -> None:
        if self._done is not None and not self._done.done():
            self._done.set_result(None)
