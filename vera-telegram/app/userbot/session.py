import os

from app.config import get_settings


def get_session_path() -> str:
    path = get_settings().session_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path
