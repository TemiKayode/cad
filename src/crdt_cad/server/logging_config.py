"""Logging setup for the server process.

Plain text by default (unchanged from before this existed) -- opt into
structured JSON logs via ``CRDT_CAD_LOG_FORMAT=json`` for a hosted
deployment that ships logs to something that parses JSON (CloudWatch,
Loki, Datadog, ...). No new dependency: a JSON log line is just
``json.dumps`` over the handful of `LogRecord` fields that matter, not
a reason to pull in a formatting library.
"""

from __future__ import annotations

import json
import logging
import os


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def log_format() -> str:
    return os.environ.get("CRDT_CAD_LOG_FORMAT", "text").strip().lower()


def configure_logging() -> None:
    handler = logging.StreamHandler()
    if log_format() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Idempotent: importing app.py more than once in the same process
    # (tests do this a lot via repeated TestClient construction) must
    # not stack up duplicate handlers, which would otherwise print every
    # log line once per import.
    root.handlers.clear()
    root.addHandler(handler)
