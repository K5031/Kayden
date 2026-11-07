# Kayden

An AI agent for your terminal that can keep working on its own. Use it as a chat
assistant, or start it in continuous mode: it runs long tasks in the background and can
pick up again on its own when they finish.

It asks before running a command or touching a file, stops at the spending limit you
set, and remembers past conversations. Bring your own key for Claude, DeepSeek or
OpenAI.

Kayden is a small runtime plus four swappable modules (model, memory, tools,
interface). The contracts are in `src/kayden/api.py`; the product spec is `SPEC.md`.

## Quick start

Requires Linux, Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/K5031/Kayden.git
uv tool install --editable Kayden
export ANTHROPIC_API_KEY=sk-ant-...   # or enter a key in the settings screen (F2)
mkdir ~/kayden-work && cd ~/kayden-work
kayden
```

Kayden's tools work in the folder you start it from and are **not a sandbox**: start it in
a folder made for it.

## Documentation

The [documentation](docs/README.md) covers getting started, using Kayden, settings, the
concepts behind it and development.

## Status

Early MVP, version 0.0.0. No release has been made and the API may still change.

## License

MIT; see `LICENSE`.
