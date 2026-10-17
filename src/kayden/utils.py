import os
from kayden.config import BASE_DIR


def resolve_model_path(filename: str) -> str:
    return os.path.join(BASE_DIR, "models", filename)