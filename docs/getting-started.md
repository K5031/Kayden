# Getting started

## What you need

- Linux. macOS should work but is untested; Windows is not supported.
- Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/).
- An API key from one provider: [Anthropic](https://platform.claude.com/) (Claude),
  [DeepSeek](https://platform.deepseek.com/) or [OpenAI](https://platform.openai.com/).

## Install

```bash
git clone https://github.com/K5031/Kayden.git
uv tool install --editable Kayden
```

This puts the `kayden` command on your PATH. To also get the [desktop app](desktop-app.md)
and switch between the two in Settings, install with its extra instead (Python 3.13):

```bash
uv tool install --python 3.13 --editable "Kayden[desktop]"
```

Without uv, use a virtual environment (systems such as Arch refuse a system-wide
`pip install`):

```bash
python -m venv ~/.venvs/kayden
~/.venvs/kayden/bin/pip install git+https://github.com/K5031/Kayden.git
~/.venvs/kayden/bin/kayden
```

## Add your API key

Either export it in your shell profile (`~/.bashrc`, `~/.zshrc`):

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # or DEEPSEEK_API_KEY / OPENAI_API_KEY
```

or start Kayden without one: it opens the settings screen, where you choose the provider
and enter the key, which is stored in your system's keychain. Then choose **Restart now**.
Kayden uses Claude unless you choose another provider.

## First run

Kayden's tools work in the folder you start it from, its **work folder**. By default the
agent does what it likes inside that folder, and a quick automatic check decides about
anything outside it, asking you when it is unsure (see
[Permissions](settings.md#permissions)). Start in a folder made for it:

```bash
mkdir ~/kayden-work && cd ~/kayden-work
kayden
```

The start screen opens with a new conversation selected; press Enter to start it. (If you
chose the [desktop app](desktop-app.md) as the interface in Settings, `kayden` opens that
instead.) Type a
message and press Enter. The agent can run commands, work with files and search the web;
when it wants to do something it should not decide alone, it asks you first.

Next: [Using Kayden](using-kayden.md).

## Spending

Every message costs money with your provider. Set a spending limit in the settings screen
(F2) before longer sessions; Kayden stops when it is reached. See [Settings](settings.md).
