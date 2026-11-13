# Agent Note: Voice conversation

Status: implemented — the desktop app has a voice mode: it listens, sends what was said, and reads the reply aloud, with speech running on the user's machine

## Problem

Kayden is used by typing and reading. The user wants a conversation by voice in the
desktop app: speak, hear the answer, speak again, without touching the keyboard. Claude's
API takes and gives text only, so the listening and the speaking have to come from
somewhere else.

## Decision

### Voice belongs to the desktop frontend

The runtime and the other modules keep working in text. In voice mode the desktop app
turns what the user says into a message (`RuntimeAPI.send`) and reads the reply aloud
from the events it already gets (`TextDelta`, `MessageCommitted`). The conversation in
the archive is the same as a typed one, and stays readable on screen while it is spoken.

The one thing the runtime learns is that a message was said aloud: `send(..., spoken=True)`.
When that differs from the conversation's message before, the runtime appends a runtime
notice after the message (`config.VOICE_ON_NOTICE`: write only what sounds right when
spoken, without emoji, kaomoji, symbols or markup, whatever else its instructions say
about style; `VOICE_OFF_NOTICE` when typing is back). Both tell the agent not to mention
them and are not shown to the user (`api.HIDDEN_NOTICE_PREFIX`): the first version was a
visible line, and the agent greeted the user back to typing. This is the usual pair for
voice agents: the model is told its words will be spoken, and what still is not a word
is taken out before the speech engine (`voice/speech.py`). A notice is part of the conversation,
so the start of every request stays the same and a provider's prompt cache stays valid.
It follows the message, so that a conversation still begins with the user's words, which
its title is made of. A reopened conversation finds what the agent was last told in its
newest voice notice (`Runtime._was_spoken`).

### Speech runs on the user's machine

One library, sherpa-onnx, does all three jobs on the CPU (`voice/engine.py`):

| Job | Model | Download |
|---|---|---|
| Hear when the user starts and stops talking | Silero VAD | under 1 MB |
| Speech to text | NVIDIA Parakeet TDT 0.6b v2 (English), 8-bit | 482 MB |
| Text to speech | Kokoro multi-lang v1.0, voice `af_heart` | 350 MB |

The models are not shipped with Kayden. The first time voice mode is turned on, the app
says what it needs and how large it is, and downloads them into `<data dir>/voice/` when
the user agrees (`voice/download.py`); a model is in place only once it is whole. Sound
goes in and out through Qt Multimedia (`voice/audio.py`). Nothing of this is imported
until voice mode is first turned on.

sherpa-onnx is held below version 2: version 2.0.1 speaks Kokoro only from a word list
and leaves out every word that is not in it (plurals, names), where 1.13 falls back to
espeak-ng for them.

### Taking turns

`voice/turns.py` is plain logic over an engine, a player and a send function; `voice/mode.py`
connects it to the window, the controller's events, the microphone and the download.

- The sound-wave button in the message box turns voice mode on and off. While it is on,
  the message box shows what is happening: Listening, Thinking, Speaking.
- Kayden listens until the user has been silent for 0.9 s, then sends what was said. It
  listens only when it is its turn to.
- The reply is spoken sentence by sentence while it streams (`voice/speech.py`). Code
  blocks, tables, links' addresses, markup and symbols that are not words are left out of
  the speech and stay on screen.
- While Kayden speaks, the microphone is not listened to: without echo cancellation it
  would hear itself. Esc stops the speech and listens; what is left of that reply stays
  unread. With nothing being read, Esc stops the agent as before.
- Tool steps and permission prompts work as in a typed conversation; a prompt waits for
  a click. A message typed in voice mode is answered aloud too.

## Alternatives considered

- **A cloud speech service** (OpenAI, ElevenLabs, Deepgram). Better voices and nothing
  to download, but a second API key, a cost per minute, and everything said goes to
  another company. Kayden's direction is an agent that runs by itself on the user's
  machine.
- **Qt's own text to speech** (speech-dispatcher). Nothing to download, but the voice is
  robotic, and it does not listen, so a speech library is needed anyway.
- **Separate libraries** (faster-whisper to listen, Piper to speak, a third for voice
  activity). Three dependencies and three model formats where one covers it.
- **Whisper for speech to text.** Multilingual, but slower on a CPU and less accurate in
  English than Parakeet at the same size. Another language is a model swap later.
- **Kokoro v0.19, English only.** Smaller (304 MB), but an older voice, and sherpa-onnx 2
  does not load it at all.
- **Voice as a module kind of its own**, or audio blocks in messages. The spec leaves room
  for audio content, but the model takes text: a new module kind would carry sound
  through the runtime only to turn it into text at the other end.
- **A system prompt section while voice is on.** Simpler, but every switch rewrites the
  provider's cache for the whole conversation.
- **The notice in front of the message.** Built first; the conversation's title was then
  made of the notice ("Voice Chat Response Style Instructions").
- **`RuntimeAPI.set_spoken(on)`**, a mode on the runtime. A flag on each message says the
  same without state a frontend has to keep in step.

## Consequences

- Checked with the real models, no sound device: a question spoken by the engine and fed
  in as microphone sound was heard as one utterance, transcribed word for word, sent, and
  the scripted reply read in three sentences, its code block and kaomoji left out. On the
  user's laptop the models load in under 2 s, 4 s of speech is understood in 0.1 s and
  spoken in 0.5 s.
- Checked with Opus 5.5: asked aloud how to list files on Linux, it answered in plain
  sentences without a list or a code block.
- Not checked: a real microphone, and speech from the speakers; the speaker was run with
  silence only.
- No talking over Kayden: interrupting takes a key, not the voice.
- English only, and about 1 GB on disk once unpacked.
- The desktop extra grows by Qt's add-ons (about 300 MB installed), sherpa-onnx (40 MB)
  and numpy, also for those who never talk to it.
- A pause of more than 0.9 s in the middle of a sentence ends the turn; what is said
  while Kayden thinks or speaks is not heard.
- A noisy room can be taken for speech and send nonsense, or keep a turn from ending.
