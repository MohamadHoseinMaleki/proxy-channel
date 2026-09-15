import logging
import structlog


def setup_logging(env: str, log_level: str, worker_name: str) -> None:
    """Configures structured logging for the application."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors for all environments
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Environment-specific formatting
    if env == "prod":
        processors.append(structlog.processors.JSONRenderer())
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
        )
    else:
        processors.append(structlog.dev.ConsoleRenderer())
        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(),
        )

    # Configure standard library logging handler
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    
    # Clear existing handlers to prevent duplicates during testing
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Configure structlog
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Bind the worker identity to all log entries globally
    structlog.contextvars.bind_contextvars(worker=worker_name)