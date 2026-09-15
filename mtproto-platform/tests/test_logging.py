import structlog
from src.core.logger import setup_logging


def test_setup_logging() -> None:
    """Verifies that logging initialization does not throw errors."""
    setup_logging(env="dev", log_level="DEBUG", worker_name="test-worker")
    logger = structlog.get_logger()
    logger.info("test_log_message")