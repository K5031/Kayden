"""Voice conversation in the desktop app (frontends.desktop.voice): what is read aloud,
and who has the turn. No sound device and no speech model: the engine is scripted."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncio

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from kayden.frontends.desktop import theme
from kayden.frontends.desktop.voice.speech import Sentences, spoken
from kayden.frontends.desktop.voice.turns import State, Voice
from tests.conftest import eventually


def test_only_words_are_read_aloud():
    assert spoken("**Done!** I fixed `parse()` in [the parser](src/p.py).") == \
        "Done! I fixed parse in the parser."
    assert spoken("All set (ノ◕ヮ◕)ノ*:･ﾟ✧") == "All set"
    assert spoken("Nothing's left open (=^･ω･^=)") == "Nothing's left open"
    assert spoken("## Result\n- one\n- two") == "Result one two"
    assert spoken("See https://example.com/a?b=1 for more.") == "See for more."
    assert spoken("| a | b |\n|---|---|\n| 1 | 2 |") == ""
    assert spoken("Run this:\n```bash\nrm -rf /\n```\nThen wait.") == "Run this: Then wait."
    assert spoken("It costs $3.50 (about 20% more), café prices.") == \
        "It costs $3.50 (about 20% more), café prices."


def test_a_reply_is_cut_into_sentences_while_it_streams():
    sentences = Sentences()
    assert sentences.feed("Hello there. I am ") == ["Hello there."]
    assert sentences.feed("reading 3.5 files") == []
    assert sentences.feed("!\nNext: ") == ["I am reading 3.5 files!"]
    assert sentences.feed("```py\nx = 1. y = 2\n") == []  # code is waited out, not read
    assert sentences.feed("```\nDone") == ["Next:"]
    assert sentences.flush() == ["Done"]
    assert sentences.flush() == []


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    theme.apply(app, dark=False)
    return app


class ScriptedEngine:
    """Hears an utterance whenever a chunk holding a 1 arrives; says what `words` holds."""

    def __init__(self, *words: str) -> None:
        self.words = list(words)
        self.spoke: list[str] = []
        self.resets = 0

    def hear(self, samples: np.ndarray) -> list[np.ndarray]:
        return [samples] if samples.any() else []

    def reset(self) -> None:
        self.resets += 1

    def transcribe(self, samples: np.ndarray) -> str:
        return self.words.pop(0)

    def speak(self, text: str) -> tuple[np.ndarray, int]:
        self.spoke.append(text)
        return np.zeros(4, dtype=np.float32), 24000


class Speaker:
    def __init__(self) -> None:
        self.played = 0
        self.stopped = 0
        self.hold: asyncio.Event | None = None  # set to keep a sentence playing

    async def play(self, samples: np.ndarray, rate: int) -> None:
        self.played += 1
        if self.hold is not None:
            await self.hold.wait()

    def stop(self) -> None:
        self.stopped += 1
        if self.hold is not None:
            self.hold.set()


def make(*words: str) -> tuple[Voice, ScriptedEngine, Speaker, list[str], list[State]]:
    engine, speaker, sent, states = ScriptedEngine(*words), Speaker(), [], []
    voice = Voice(engine, speaker, sent.append, lambda: states.append(voice.state))
    return voice, engine, speaker, sent, states


QUIET, TALK = np.zeros(8, dtype=np.float32), np.ones(8, dtype=np.float32)


async def test_what_is_said_is_sent_and_the_reply_is_spoken_then_it_listens_again():
    voice, engine, speaker, sent, states = make("what time is it")
    assert voice.state is State.OFF
    voice.start()
    assert voice.state is State.LISTENING
    voice.heard(QUIET)
    assert sent == []
    voice.heard(TALK)
    await eventually(lambda: sent == ["what time is it"])
    assert voice.state is State.THINKING
    voice.heard(TALK)  # not its turn to listen
    voice.reply("It is noon. ")
    await eventually(lambda: engine.spoke == ["It is noon."])
    voice.reply("Lunch time (＾▽＾)")
    voice.reply_ended()
    voice.agent_idle()
    await eventually(lambda: voice.state is State.LISTENING)
    assert engine.spoke == ["It is noon.", "Lunch time"] and speaker.played == 2
    assert sent == ["what time is it"]
    assert states[:3] == [State.LISTENING, State.THINKING, State.SPEAKING]
    voice.stop()
    assert voice.state is State.OFF


async def test_nothing_understood_sends_nothing():
    voice, _engine, _speaker, sent, _states = make("  ")
    voice.start()
    voice.heard(TALK)
    await eventually(lambda: voice.state is State.LISTENING and not voice.busy)
    assert sent == []


async def test_interrupting_stops_the_speech_and_listens():
    voice, engine, speaker, sent, _states = make("tell me a story", "stop")
    speaker.hold = asyncio.Event()
    voice.start()
    voice.heard(TALK)
    await eventually(lambda: sent == ["tell me a story"])
    voice.reply("Once upon a time. There was a fox. ")
    await eventually(lambda: voice.state is State.SPEAKING)
    voice.interrupt()
    assert speaker.stopped == 1 and voice.state is State.LISTENING
    voice.reply("It lived in a den. ")  # the rest of that reply is not read
    voice.reply_ended()
    await asyncio.sleep(0.05)
    assert engine.spoke == ["Once upon a time."]
    speaker.hold = None
    voice.heard(TALK)
    await eventually(lambda: sent == ["tell me a story", "stop"])
    voice.reply("Okay. ")
    await eventually(lambda: engine.spoke[-1] == "Okay.")


async def test_turned_on_while_the_agent_works_it_waits_for_its_turn():
    voice, _engine, _speaker, sent, _states = make("hello")
    voice.start(agent_busy=True)
    assert voice.state is State.THINKING
    voice.heard(TALK)
    await asyncio.sleep(0.02)
    assert sent == []
    voice.agent_idle()
    assert voice.state is State.LISTENING


async def test_the_speech_models_are_downloaded_whole_or_not_at_all(tmp_path):
    import io
    import tarfile

    import httpx2

    from kayden.frontends.desktop.voice.download import Model, fetch, megabytes, missing

    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:bz2") as archive:
        data = b"weights"
        info = tarfile.TarInfo("listener/model.onnx")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    served = {"/vad.onnx": b"vad", "/listener.tar.bz2": packed.getvalue()}
    models = [Model("vad.onnx", "https://models.example/vad.onnx", 3, archive=False),
              Model("listener", "https://models.example/listener.tar.bz2",
                    len(served["/listener.tar.bz2"]))]
    folder = tmp_path / "voice"
    assert missing(folder, models) == models and megabytes(models) == 0

    def serve(request: httpx2.Request) -> httpx2.Response:
        if request.url.path not in served:
            return httpx2.Response(404)
        return httpx2.Response(200, content=served[request.url.path])

    seen: list[float] = []
    await fetch(folder, models, seen.append, transport=httpx2.MockTransport(serve))
    assert missing(folder, models) == []
    assert (folder / "vad.onnx").read_bytes() == b"vad"
    assert (folder / "listener" / "model.onnx").read_bytes() == b"weights"
    assert seen[-1] == 1.0 and seen == sorted(seen)
    assert sorted(p.name for p in folder.iterdir()) == ["listener", "vad.onnx"]  # no leftovers

    gone = [Model("other", "https://models.example/other.tar.bz2", 10)]
    with pytest.raises(httpx2.HTTPError):
        await fetch(folder, gone, seen.append, transport=httpx2.MockTransport(serve))
    assert missing(folder, gone) == gone


def test_microphone_sound_is_made_mono_at_the_engines_rate():
    from PySide6.QtMultimedia import QAudioFormat

    from kayden.frontends.desktop.voice.audio import as_heard
    from kayden.frontends.desktop.voice.engine import RATE

    stereo = np.array([[16384, -16384]] * 4800 + [[16384, 16384]] * 4800, dtype=np.int16)
    heard = as_heard(stereo.tobytes() + b"\x01", 48000, 2, QAudioFormat.SampleFormat.Int16)
    assert heard.dtype == np.float32 and len(heard) == RATE // 5  # 0.2 s
    assert abs(heard[0]) < 1e-6 and abs(heard[-1] - 0.5) < 1e-6  # the channels' mean
    same = as_heard(np.full(160, 0.25, np.float32).tobytes(), RATE, 1,
                    QAudioFormat.SampleFormat.Float)
    assert len(same) == 160 and abs(same[0] - 0.25) < 1e-6


class FakeMicrophone:
    def __init__(self, heard) -> None:
        self.heard, self.open = heard, False

    def start(self) -> None:
        self.open = True

    def stop(self) -> None:
        self.open = False


async def test_the_voice_button_starts_a_spoken_conversation(
        make, qapp, tmp_path, monkeypatch):
    """The first use asks, downloads the models and listens; what is said is sent, the
    reply is read aloud, and the agent is told it is being talked to."""
    import httpx2

    from kayden.api import Role, Text
    from kayden.frontends.desktop.viewmodel import Controller, ConversationModel
    from kayden.frontends.desktop.voice.download import Model
    from kayden.frontends.desktop.voice.mode import VoiceMode
    from kayden.frontends.desktop.window import MainWindow
    from kayden.runtime.config import VOICE_ON_NOTICE
    from tests.fakes.core import Step

    h = await make()
    h.core.add(Step(text="It is noon. Lunch time (＾▽＾)"))
    await h.start()
    controller = Controller(h.runtime, ConversationModel())
    engine, speaker, microphones, asked = ScriptedEngine("what time is it"), Speaker(), [], []

    async def ask(text: str, details: str) -> bool:
        asked.append(details)
        return len(asked) > 1  # declined the first time

    def microphone(heard) -> FakeMicrophone:
        microphones.append(FakeMicrophone(heard))
        return microphones[-1]

    release = asyncio.Event()  # holds the download partway
    monkeypatch.setattr("kayden.frontends.desktop.voice.download.CHUNK", 1)

    async def body():
        yield b"va"
        await release.wait()
        yield b"d"

    class Slow(httpx2.AsyncBaseTransport):  # MockTransport would read the body at once
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, content=body())

    served = Slow()
    folder = tmp_path / "voice"
    voice = VoiceMode(controller, folder, ask, engine=lambda _folder: engine,
                      microphone=microphone, speaker=lambda: speaker, transport=served,
                      models=(Model("vad.onnx", "https://models.example/vad.onnx", 3, False),))
    window = MainWindow(h.runtime, controller, voice=voice)
    task = asyncio.create_task(controller.run())
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))
    assert window.voice_button.toolTip() == "Talk with Kayden"

    window.voice_button.click()
    await eventually(lambda: asked and not voice.on)  # declined: off, nothing fetched
    assert "0 MB" in asked[0] and not folder.exists() and not microphones

    assert window.voice_progress.isHidden()
    window.voice_button.click()
    await eventually(lambda: voice.progress is not None and voice.progress > 0.5)
    assert not window.voice_progress.isHidden()  # a bar and how far it is, while it runs
    assert window.voice_progress_bar.value() == 667
    assert window.voice_progress_label.text().startswith("Downloading speech models: ")
    release.set()
    await eventually(lambda: voice.spoken)
    assert window.voice_progress.isHidden()
    assert (folder / "vad.onnx").read_bytes() == b"vad"
    assert window.input.placeholderText() == "Listening…"
    assert window.voice_button.property("active") is True
    assert window.voice_button.toolTip() == "End the voice conversation"
    [mic] = microphones
    assert mic.open
    mic.heard(TALK)
    await eventually(lambda: engine.spoke == ["It is noon.", "Lunch time"])
    await eventually(lambda: window.input.placeholderText() == "Listening…")
    said = [b.text for m in await h.transcript() if m.role is Role.USER
            for b in m.blocks if isinstance(b, Text)]
    assert said == ["what time is it", VOICE_ON_NOTICE]

    window.voice_button.click()
    assert not voice.on and not mic.open
    assert window.input.placeholderText() == "Message Kayden"
    assert window.voice_button.property("active") is False
    window.voice_button.click()  # on again: no question, the models are there
    await eventually(lambda: voice.spoken)
    assert len(asked) == 2
    window.close()
    assert not voice.on
    task.cancel()
