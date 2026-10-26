#!/usr/bin/env python3
#
# tests/test_gunicorn_logging.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

import logging
import time
import unittest
from pathlib import Path

from gunicorn.config import Config

from app.runtime.gunicorn_logging import FetchlyGunicornLogger
from run import RedactingFormatter, _build_log_config


class GunicornLoggingTests(unittest.TestCase):
    def test_uvicorn_handlers_use_the_global_log_format(self) -> None:
        loggers = [logging.getLogger("gunicorn.error"), logging.getLogger("gunicorn.access")]
        original_state = [(logger, list(logger.handlers), logger.level, logger.propagate) for logger in loggers]
        self.addCleanup(self._restore_loggers, original_state)

        config = Config()
        config.set("errorlog", "-")
        config.set("accesslog", "-")
        logger = FetchlyGunicornLogger(config)
        run_formatter = _build_log_config(log_level="INFO", use_colors=False)["formatters"]["redacted"]

        for target in (logger.error_log, logger.access_log):
            handler = next(handler for handler in target.handlers if getattr(handler, "_gunicorn", False))
            formatter = handler.formatter
            self.assertIsNotNone(formatter)
            self.assertEqual(formatter._style._fmt, run_formatter["format"])
            self.assertEqual(formatter.datefmt, run_formatter["datefmt"])
            # Logs carry UTC, whatever TZ the host or container sets.
            self.assertIs(formatter.converter, time.gmtime)

    def test_uvicorn_formatter_writes_iso_utc_timestamps(self) -> None:
        config = _build_log_config(log_level="INFO", use_colors=False)["formatters"]["redacted"]
        formatter = RedactingFormatter(config["format"], config["datefmt"])
        record = logging.makeLogRecord({"msg": "hello", "created": 0.0})
        self.assertTrue(formatter.format(record).startswith("1970-01-01T00:00:00Z - "))

    def test_entrypoint_uses_the_custom_logger(self) -> None:
        entrypoint = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
        self.assertIn("--logger-class app.runtime.gunicorn_logging.FetchlyGunicornLogger", entrypoint.read_text())

    @staticmethod
    def _restore_loggers(original_state: list[tuple[logging.Logger, list[logging.Handler], int, bool]]) -> None:
        for logger, handlers, level, propagate in original_state:
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
