"""HEGO configuration module.

Loads environment variables from the project root .env file and exposes
them as module-level constants. Provides a logging setup helper.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Locate and load the .env file from the project root (two levels up from
# this file: ingestors/common/config.py -> ingestors/ -> hego/).
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=_ENV_PATH)

# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------
ES_HOST: str = os.getenv("ES_HOST", "localhost")
ES_PORT: int = int(os.getenv("ES_PORT", "9200"))
ES_USER: str = os.getenv("ES_USER", "elastic")
ES_PASSWORD: str = os.getenv("ELASTIC_PASSWORD", "")
ES_SCHEME: str = os.getenv("ES_SCHEME", "https")
ES_VERIFY_CERTS: bool = os.getenv("ES_VERIFY_CERTS", "false").lower() in (
    "true",
    "1",
    "yes",
)

# ---------------------------------------------------------------------------
# OpenCTI
# ---------------------------------------------------------------------------
OPENCTI_URL: str = os.getenv("OPENCTI_URL", "http://localhost:8080")
OPENCTI_TOKEN: str = os.getenv("OPENCTI_ADMIN_TOKEN", "")

# ---------------------------------------------------------------------------
# ACLED
# ---------------------------------------------------------------------------
ACLED_API_KEY: str = os.getenv("ACLED_API_KEY", "")
ACLED_EMAIL: str = os.getenv("ACLED_EMAIL", "")

# ---------------------------------------------------------------------------
# Alerting — Discord
# ---------------------------------------------------------------------------
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

# ---------------------------------------------------------------------------
# Alerting — Email / SMTP
# ---------------------------------------------------------------------------
ALERT_EMAIL_SMTP_HOST: str = os.getenv("ALERT_EMAIL_SMTP_HOST", "")
ALERT_EMAIL_SMTP_PORT: int = int(os.getenv("ALERT_EMAIL_SMTP_PORT", "587"))
ALERT_EMAIL_FROM: str = os.getenv("ALERT_EMAIL_FROM", "")
ALERT_EMAIL_TO: str = os.getenv("ALERT_EMAIL_TO", "")
ALERT_EMAIL_PASSWORD: str = os.getenv("ALERT_EMAIL_PASSWORD", "")

# ---------------------------------------------------------------------------
# Index naming
# ---------------------------------------------------------------------------
INDEX_PREFIX: str = "hego"

# ---------------------------------------------------------------------------
# Retry defaults (used by tenacity decorators across the project)
# ---------------------------------------------------------------------------
RETRY_MAX_ATTEMPTS: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
RETRY_WAIT_MIN: int = int(os.getenv("RETRY_WAIT_MIN", "1"))
RETRY_WAIT_MAX: int = int(os.getenv("RETRY_WAIT_MAX", "30"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def setup_logging(name: str | None = None, level: str | None = None) -> logging.Logger:
    """Configure and return a logger.

    When called without arguments it configures the **root** logger so that
    every module inherits the same format and level.  When *name* is given a
    child logger is returned instead.

    Args:
        name: Optional logger name.  ``None`` means root logger.
        level: Override log level (e.g. ``"DEBUG"``).  Defaults to the
            ``LOG_LEVEL`` environment variable or ``INFO``.

    Returns:
        Configured :class:`logging.Logger` instance.
    """
    effective_level = getattr(logging, (level or LOG_LEVEL), logging.INFO)

    if name is None:
        # Configure the root logger once.
        logging.basicConfig(level=effective_level, format=LOG_FORMAT)
        return logging.getLogger()

    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(effective_level)
    return logger
