# Kayden

Kayden is an AI agent that runs on your computer. You talk to it, and it can run commands
and work with files to get things done, asking your permission first. It works with
Claude, DeepSeek or OpenAI models, using your own API key, and keeps a spending limit.

Under the hood, a runtime drives four swappable modules (model, memory, tools, interface)
through the contracts in `src/kayden/api.py`; `SPEC.md` is the product spec.

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
