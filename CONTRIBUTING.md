# Contributing to Kayden

Thanks for wanting to help. Kayden is young: the module contracts may still change,
so small, focused changes are the easiest to land.

## Getting set up

Clone the repository, install [uv](https://docs.astral.sh/uv/), and follow
[Development](docs/development.md): it covers running Kayden from your checkout, the
architecture and the tests.

## Proposing something

- **Bugs and ideas:** open an issue. Steps to reproduce, or what you would like Kayden to
  do and why, help most.
- **Small fixes:** open a pull request directly.
- **Anything bigger** (a new feature, a change to a module contract in
  `src/kayden/api.py`, a new dependency): open an issue first, so we can agree on the
  direction before you spend time on it.

## What a pull request needs

- **Tests** for the new or changed behaviour.
- **Modules talk only through the runtime.** Core, Memory, Tools and Frontend never call
  each other; see [Development](docs/development.md#architecture).
- **Docs:** if the change is something users see (a command, a setting, a default), update
  the page in `docs/` it belongs to.
- **Green checks:** `uv run pytest` and `uv run ruff check` pass.

## Good places to start

Writing a module is the cleanest way in: a new Core (model provider), Tools or Frontend
needs no change to the runtime. See [Writing a module](docs/development.md#writing-a-module).

## Using an AI agent

Agent-written pull requests are welcome. Point your agent at [AGENTS.md](AGENTS.md), which
holds the full working rules, and review what it wrote: you are responsible for the pull
request you open.

## License

Kayden is MIT licensed. By contributing, you agree that your contribution is released under
the same license.

## Be kind

Be patient and respectful with everyone here.
