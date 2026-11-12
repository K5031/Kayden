"""Voice mode in the window: gets the speech models, opens the microphone, and runs the
conversation (turns.Voice) on the open conversation's events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx2
import numpy as np
from PySide6.QtCore import QObject, Signal

from kayden.api import MessageCommitted, Role, RuntimeEvent, StateChanged, Text, TextDelta
from kayden.frontends.desktop.viewmodel import Controller
from kayden.frontends.desktop.voice import download
from kayden.frontends.desktop.voice.turns import Engine, Player, State, Voice

STATUS = {State.LISTENING: "Listening…", State.THINKING: "Thinking…",
          State.SPEAKING: "Speaking… Esc to interrupt"}


def _sherpa(folder: Path) -> Engine:
    from kayden.frontends.desktop.voice.engine import SherpaEngine
    return SherpaEngine(folder)


def _microphone(heard: Callable[[np.ndarray], object]) -> object:
    from kayden.frontends.desktop.voice.audio import Microphone
    return Microphone(heard)


def _speaker() -> Player:
    from kayden.frontends.desktop.voice.audio import Speaker
    return Speaker()


class VoiceMode(QObject):
    """`changed` is emitted whenever `on` or `status` changes. `ask` puts a question to
    the user and answers whether they agreed. Nothing of speech is loaded until voice
    mode is first turned on."""

    changed = Signal()

    def __init__(self, controller: Controller, folder: Path | None,
                 ask: Callable[[str, str], Awaitable[bool]], *,
                 engine: Callable[[Path], Engine] = _sherpa,
                 microphone: Callable[[Callable[[np.ndarray], object]], object] = _microphone,
                 speaker: Callable[[], Player] = _speaker,
                 models: tuple[download.Model, ...] = download.MODELS,
                 transport: httpx2.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.controller, self.folder, self._ask = controller, folder, ask
        self._make_engine, self._make_microphone, self._make_speaker = engine, microphone, speaker
        self._models, self._transport = models, transport
        self.on = False
        self.status = ""
        self._engine: Engine | None = None
        self._voice: Voice | None = None
        self._microphone: object | None = None
        self._starting: asyncio.Task[None] | None = None
        self._streamed = False  # the reply under way came as deltas
        controller.listeners.append(self._event)

    def toggle(self) -> None:
        if self.on:
            self.stop()
        else:
            self.on = True
            self._starting = asyncio.ensure_future(self._start())

    def stop(self) -> None:
        if self._starting is not None:
            self._starting.cancel()
        if self._voice is not None:
            self._voice.stop()
        if self._microphone is not None:
            self._microphone.stop()  # type: ignore[attr-defined]
        self._voice = self._microphone = self._starting = None
        self.on = False
        self._say("")

    def interrupt(self) -> bool:
        """Stop reading the reply and listen; False when nothing was being read."""
        if self._voice is None or self._voice.state is not State.SPEAKING:
            return False
        self._voice.interrupt()
        return True

    @property
    def spoken(self) -> bool:
        """Messages sent now are part of a voice conversation."""
        return self._voice is not None

    async def _start(self) -> None:
        try:
            if self.folder is None:
                raise RuntimeError("Voice needs Kayden's data folder, and there is none.")
            if self._engine is None:
                self._say("Getting voice ready…")
                needed = await asyncio.to_thread(download.missing, self.folder, self._models)
                if needed and not await self._get(needed):
                    self.stop()
                    return
                self._say("Getting voice ready…")
                self._engine = await asyncio.to_thread(self._make_engine, self.folder)
            voice = Voice(self._engine, self._make_speaker(), self._said, self._moved)
            microphone = self._make_microphone(voice.heard)
            microphone.start()  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - shown; voice mode stays off
            self.stop()
            self.controller.model.notify(f"Voice could not start: {e}")
            return
        self._voice, self._microphone, self._starting = voice, microphone, None
        voice.start(agent_busy=self.controller.model.busy)

    async def _get(self, needed: list[download.Model]) -> bool:
        size = download.megabytes(needed)
        if not await self._ask(
                "Download the speech models?",
                f"Voice conversation needs speech models ({size} MB). They run on this "
                "computer: what you say is not sent to a speech service."):
            return False
        assert self.folder is not None
        await download.fetch(
            self.folder, needed,
            lambda done: self._say(f"Downloading speech models… {round(done * 100)}%"),
            transport=self._transport)
        return True

    def _said(self, text: str) -> None:
        self.controller.send(text, spoken=True)

    def _moved(self) -> None:
        if self._voice is not None:
            self._say(STATUS.get(self._voice.state, ""))

    def _say(self, status: str) -> None:
        self.status = status
        self.changed.emit()

    def _event(self, event: RuntimeEvent) -> None:
        voice = self._voice
        if voice is None:
            return
        match event:
            case TextDelta(text=text):
                self._streamed = True
                voice.reply(text)
            case MessageCommitted(message=message) if message.role is Role.ASSISTANT:
                if not self._streamed:  # a Core that does not stream: read it now
                    voice.reply("".join(b.text for b in message.blocks if isinstance(b, Text)))
                self._streamed = False
                voice.reply_ended()
            case StateChanged():
                if not self.controller.model.busy:
                    voice.agent_idle()
