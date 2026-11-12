"""Who has the turn in a voice conversation: plain logic over an engine that hears and
speaks, a player for sound, and a function that sends what was said."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

import numpy as np

from kayden.frontends.desktop.voice.speech import Sentences


class State(StrEnum):
    OFF = "off"
    LISTENING = "listening"  # the microphone is heard
    THINKING = "thinking"  # what was said is being understood, or the agent works
    SPEAKING = "speaking"  # the reply is read aloud


class Engine(Protocol):
    """Speech on this machine. `transcribe` and `speak` block and run on a thread."""

    def hear(self, samples: np.ndarray) -> list[np.ndarray]:
        """Take microphone sound (float32, mono, at the engine's rate); return each
        utterance that has ended: the user spoke, then fell silent."""

    def reset(self) -> None:
        """Forget sound heard so far: a new turn to listen begins."""

    def transcribe(self, samples: np.ndarray) -> str: ...

    def speak(self, text: str) -> tuple[np.ndarray, int]:
        """`text` as sound: float32 mono samples and their rate."""


class Player(Protocol):
    async def play(self, samples: np.ndarray, rate: int) -> None:
        """Returns when the sound has been heard or was stopped."""

    def stop(self) -> None: ...


class Voice:
    """One voice conversation. The owner feeds it the microphone (`heard`), the agent's
    reply as it streams (`reply`, `reply_ended`) and when the agent stops (`agent_idle`)."""

    def __init__(self, engine: Engine, player: Player, send: Callable[[str], object],
                 on_change: Callable[[], object]) -> None:
        self.engine, self.player = engine, player
        self._send, self._on_change = send, on_change
        self.state = State.OFF
        self._sentences = Sentences()
        self._to_say: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: set[asyncio.Task[None]] = set()
        self._speaker: asyncio.Task[None] | None = None
        self._agent_busy = False
        self._muted = False  # interrupted: the rest of this reply is not read

    @property
    def busy(self) -> bool:
        """Something said is still being understood."""
        return bool(self._tasks)

    def start(self, *, agent_busy: bool = False) -> None:
        self._agent_busy, self._muted = agent_busy, False
        self._speaker = asyncio.ensure_future(self._speak())
        self._listen() if not agent_busy else self._set(State.THINKING)

    def stop(self) -> None:
        for task in (*self._tasks, self._speaker):
            if task is not None:
                task.cancel()
        self._tasks.clear()
        self._speaker = None
        self._drop_speech()
        self._set(State.OFF)

    def heard(self, samples: np.ndarray) -> None:
        if self.state is not State.LISTENING:
            return
        for utterance in self.engine.hear(samples):
            self._set(State.THINKING)
            task = asyncio.ensure_future(self._said(utterance))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return  # one turn: what follows is heard when listening again

    def reply(self, text: str) -> None:
        if self.state is State.OFF or self._muted:
            return
        for sentence in self._sentences.feed(text):
            self._to_say.put_nowait(sentence)

    def reply_ended(self) -> None:
        if self.state is State.OFF or self._muted:
            return
        for sentence in self._sentences.flush():
            self._to_say.put_nowait(sentence)

    def agent_idle(self) -> None:
        self._agent_busy = False
        if self.state is State.THINKING and self._to_say.empty() and not self.busy:
            self._listen()

    def interrupt(self) -> None:
        """Stop reading and listen; what is left of the reply stays unread."""
        if self.state is State.OFF:
            return
        self._muted = self._agent_busy
        self._drop_speech()
        self._listen()

    def _drop_speech(self) -> None:
        self._sentences = Sentences()
        while not self._to_say.empty():
            self._to_say.get_nowait()
        self.player.stop()

    def _listen(self) -> None:
        self.engine.reset()
        self._set(State.LISTENING)

    def _set(self, state: State) -> None:
        if state is not self.state:
            self.state = state
            self._on_change()

    async def _said(self, utterance: np.ndarray) -> None:
        text = (await asyncio.to_thread(self.engine.transcribe, utterance)).strip()
        if self.state is not State.THINKING:
            return
        if not text:
            self._listen()
            return
        self._agent_busy, self._muted = True, False
        self._send(text)

    async def _speak(self) -> None:
        while True:
            sentence = await self._to_say.get()
            self._set(State.SPEAKING)
            samples, rate = await asyncio.to_thread(self.engine.speak, sentence)
            if self.state is State.SPEAKING:  # not interrupted meanwhile
                await self.player.play(samples, rate)
            if self.state is State.SPEAKING and self._to_say.empty():
                self._listen() if not self._agent_busy else self._set(State.THINKING)
