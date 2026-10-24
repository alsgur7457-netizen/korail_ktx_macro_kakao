"""
Application configuration management.

This module centralizes all configuration variables from environment
and provides type-safe access to settings throughout the application.
Values from a ``.env`` file at the project root (or ``ENV_FILE``) are loaded
first; variables already set in the environment win.
"""
import os
from pathlib import Path
from typing import Optional


def _load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader so `.env` works without mise/docker env_file."""
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (FileNotFoundError, IsADirectoryError):
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().removeprefix('export ').strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        elif ' #' in value:
            value = value.split(' #', 1)[0].rstrip()
        os.environ.setdefault(key, value)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_load_dotenv(Path(os.environ.get('ENV_FILE', PROJECT_ROOT / '.env')))


class Settings:
    """Application settings loaded from environment variables."""

    # Korail Configuration
    KORAIL_ID: Optional[str] = os.environ.get('KORAIL_ID')
    KORAIL_PW: Optional[str] = os.environ.get('KORAIL_PW')
    KORAIL_SEARCH_INTERVAL: int = int(os.environ.get('SEARCH_INTERVAL', '3'))  # seconds between searches
    # DynaPath device identity. Pin explicitly or let the service persist a generated one to a file
    # so every run presents the same device to Korail.
    KORAIL_DEVICE_PROFILE_ID: Optional[str] = os.environ.get('KORAIL_DEVICE_PROFILE_ID')
    KORAIL_ANDROID_ID: Optional[str] = os.environ.get('KORAIL_ANDROID_ID')
    KORAIL_DEVICE_FILE: str = os.path.expanduser(os.environ.get('KORAIL_DEVICE_FILE', '~/.korail_bot_device.json'))
    KORAIL_PAYMENT_URL: str = "https://www.letskorail.com/ebizprd/EbizPrdTicketpr13500W_pr13510.do"

    # Kakao "나에게 보내기" (talk memo) Configuration
    KAKAO_REST_API_KEY: Optional[str] = os.environ.get('KAKAO_REST_API_KEY')
    KAKAO_CLIENT_SECRET: Optional[str] = os.environ.get('KAKAO_CLIENT_SECRET') or None
    KAKAO_REDIRECT_URI: str = os.environ.get('KAKAO_REDIRECT_URI', 'http://localhost:8080/kakao')
    KAKAO_TOKEN_FILE: str = os.path.expanduser(os.environ.get('KAKAO_TOKEN_FILE', '~/.korail_bot_kakao.json'))

    # Payment follow-up after a successful reservation
    PAYMENT_TIMEOUT_MINUTES: int = int(os.environ.get('PAYMENT_TIMEOUT_MINUTES', '10'))  # fallback deadline
    PAYMENT_REMINDER_MINUTES: int = int(os.environ.get('PAYMENT_REMINDER_MINUTES', '3'))
    PAYMENT_CHECK_SECONDS: int = int(os.environ.get('PAYMENT_CHECK_SECONDS', '60'))

    # Logging Configuration
    LOG_LEVEL: str = os.environ.get('LOG_LEVEL', 'INFO').upper()
    LOG_FORMAT: str = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'


# Singleton instance
settings = Settings()
