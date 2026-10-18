import shutil
import os
import sys
from importlib.resources import files

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

CONFIG_HOME = os.environ.get(
    "XDG_CONFIG_HOME",
    os.path.expanduser("~/.config"),
)
CONFIG_DIR = os.path.join(CONFIG_HOME, "kayden")
LEGACY_CONFIG_DIR = os.path.expanduser("~/.config/k")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")
SYSTEM_PROMPT_PATH = os.path.join(CONFIG_DIR, "system_prompt.txt")
CHATGPT_AUTH_PATH = os.path.join(CONFIG_DIR, "chatgpt.json")

DATA_HOME = os.environ.get(
    "XDG_DATA_HOME",
    os.path.expanduser("~/.local/share"),
)
DATA_DIR = os.environ.get("KAYDEN_DATA_DIR", os.path.join(DATA_HOME, "kayden"))
CONTINUITY_PATH = os.path.join(DATA_DIR, "continuity.sqlite3")

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if os.path.isfile(os.path.join(PROJECT_ROOT, "pyproject.toml")):
    BASE_DIR = PROJECT_ROOT
else:
    BASE_DIR = os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "kayden",
    )

RESOURCES = files("kayden.resources")
DEFAULT_CONFIG = RESOURCES.joinpath("config.example.toml")
DEFAULT_PROMPT = RESOURCES.joinpath("default_prompt.txt")


def resolve_model_path(filename: str) -> str:
    return os.path.join(BASE_DIR, "models", filename)


def ensure_config():
    if not os.path.exists(CONFIG_DIR) and os.path.isdir(LEGACY_CONFIG_DIR):
        os.makedirs(os.path.dirname(CONFIG_DIR), exist_ok=True)
        shutil.move(LEGACY_CONFIG_DIR, CONFIG_DIR)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        shutil.copyfile(DEFAULT_CONFIG, CONFIG_PATH)
    if not os.path.exists(SYSTEM_PROMPT_PATH):
        shutil.copyfile(DEFAULT_PROMPT, SYSTEM_PROMPT_PATH)


def load_config():
    ensure_config()
    with open(CONFIG_PATH, "rb") as f:
        return _normalize_legacy_frontend(tomllib.load(f))


def _without_terminal(value):
    names = value if isinstance(value, list) else [value]
    remaining = [name for name in names if name != "terminal"]
    if not remaining:
        return None
    return remaining if isinstance(value, list) else remaining[0]


def _normalize_legacy_frontend(values):
    """Keep legacy controller/terminal configs working with current modules."""
    needs_frontend = "frontend" not in values and bool(
        {"input", "output"} & values.keys()
    )

    normalized = {}
    for key, value in values.items():
        if key == "controller":
            continue
        normalized[key] = value

    if needs_frontend:
        normalized = {"frontend": "tui", **normalized}

    for capability in ("input", "output"):
        if capability not in normalized:
            continue
        remaining = _without_terminal(normalized[capability])
        if remaining is None:
            del normalized[capability]
        else:
            normalized[capability] = remaining

    legacy_frontend = normalized.get("frontend")
    if legacy_frontend in {"gui", "floaty"}:
        normalized["frontend"] = "tauri"
        settings = normalized.setdefault("settings", {})
        frontend_settings = settings.setdefault("frontend", {})
        tauri_settings = frontend_settings.setdefault("tauri", {})
        tauri_settings.setdefault("mode", legacy_frontend)

    tools = normalized.get("tools")
    tool_names = tools if isinstance(tools, list) else [tools]
    if "web" in tool_names:
        has_search_provider = any(
            name in {"web_exa", "web_searxng"} for name in tool_names
        )
        migrated_tools = []
        for name in tool_names:
            if name == "web":
                if "web_fetch" not in migrated_tools:
                    migrated_tools.append("web_fetch")
                if not has_search_provider and "web_exa" not in migrated_tools:
                    migrated_tools.append("web_exa")
            elif name not in migrated_tools:
                migrated_tools.append(name)
        normalized["tools"] = migrated_tools
    return normalized


def load_system_prompt():
    ensure_config()
    with open(SYSTEM_PROMPT_PATH) as f:
        return f.read()
