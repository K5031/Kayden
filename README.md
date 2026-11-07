# Kayden

A terminal AI agent that runs as a chat or as a long-running background agent. It asks
before using tools and stops at hard spending limits. Works with Anthropic, DeepSeek and
OpenAI-compatible models.

A runtime drives four swappable modules (Core, Memory, Tools, Frontend); see
`src/kayden/api.py` and `SPEC.md`.

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
