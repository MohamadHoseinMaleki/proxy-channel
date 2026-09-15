import os
from src.core.config import get_settings


def test_get_settings_defaults() -> None:
    """Tests that settings load default values when environment is empty."""
    os.environ.clear()
    settings = get_settings()
    assert settings.env == "dev"
    assert settings.log_level == "INFO"
    assert "localhost" in settings.database_url