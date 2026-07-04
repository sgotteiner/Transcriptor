"""Structured JSON logging.

Every line is JSON with stable keys, so a platform like Splunk can index and
filter on `job_id` / `chunk_id` / `worker_id` out of the box. Use `bind()` to
attach context that then rides along on every subsequent log call:

    log = get_logger().bind(job_id=job.job_id)
    log.info("job.created", mode=job.mode)

Logs are emitted through the stdlib `StreamHandler`, which flushes after every
record — so lines appear in real time even when stdout is a pipe or file (Python
block-buffers non-TTY stdout otherwise, which made logs show up only at the end).
Shipping to Splunk (HEC) is a Stage B concern and deliberately not wired here.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Render every log as one JSON line to stdout, flushed per record."""
    handler = logging.StreamHandler(sys.stdout)  # emit() flushes after each record
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "transcriptor") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
