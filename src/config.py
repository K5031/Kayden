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

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isfile(os.path.join(SOURCE_DIR, "pyproject.toml")):
    BASE_DIR = SOURCE_DIR
else:
    BASE_DIR = os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "k",
    )

RESOURCES = files("k_resources")
DEFAULT_CONFIG = RESOURCES.joinpath("config.example.toml")
DEFAULT_PROMPT = RESOURCES.joinpath("default_prompt.txt")


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
        return tomllib.load(f)


def load_system_prompt():
    ensure_config()
    with open(SYSTEM_PROMPT_PATH) as f:
        return f.read()
