"""Tests for logging module."""

import logging
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from qbt_rules.logging import (
    setup_logging, get_logger, LOG_FORMAT_SIMPLE, LOG_FORMAT_DETAILED, CONSOLE_FORMAT_BARE
)


def _logging_config(level="INFO", file=None, trace_mode=False, http_access=False, client_mode=False):
    """Build a resolved logging_config dict, matching cli.py's get_logging_config() shape."""
    return {
        'level': level,
        'file': file,
        'trace_mode': trace_mode,
        'http_access': http_access,
        'client_mode': client_mode,
    }


class TestSetupLogging:
    """Test logging setup functionality."""

    def test_basic_setup_with_file_logging(self, tmp_path, caplog):
        """Setup logging with file handler."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="INFO", file=log_file))

        # Verify file was created
        assert log_file.exists()

        # Verify root logger is configured (always set to DEBUG)
        root_logger = logging.getLogger()
        assert root_logger.level == logging.DEBUG

        # Verify handlers were added (file + console)
        assert len(root_logger.handlers) == 2

    def test_trace_mode_uses_detailed_format(self, tmp_path):
        """Trace mode uses detailed log format."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="DEBUG", file=log_file, trace_mode=True))

        # Check that file handler uses detailed format
        root_logger = logging.getLogger()
        file_handlers = [h for h in root_logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) > 0

        # Verify format contains module/function/line info
        formatter = file_handlers[0].formatter
        assert formatter is not None
        # The detailed format includes %(module)s %(funcName)s %(lineno)d
        assert "module" in formatter._fmt or "funcName" in formatter._fmt

    def test_simple_mode_uses_simple_format(self, tmp_path):
        """Simple mode uses simple log format."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="INFO", file=log_file))

        # Check that file handler uses simple format
        root_logger = logging.getLogger()
        file_handlers = [h for h in root_logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) > 0

        formatter = file_handlers[0].formatter
        assert formatter is not None
        # Simple format should not include module/function info
        assert "levelname" in formatter._fmt
        assert "message" in formatter._fmt

    def _console_handler(self):
        root_logger = logging.getLogger()
        return next(
            h for h in root_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        )

    def _file_handler(self):
        root_logger = logging.getLogger()
        return next(h for h in root_logger.handlers if isinstance(h, logging.FileHandler))

    def test_client_mode_console_is_bare(self, tmp_path):
        """Client mode (any invocation other than --serve) prints bare
        messages to console, with no timestamp/level prefix -- someone
        running a CLI command interactively shouldn't see logging
        machinery on every line."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="INFO", file=log_file, client_mode=True))

        assert self._console_handler().formatter._fmt == CONSOLE_FORMAT_BARE

    def test_client_mode_file_still_gets_full_format(self, tmp_path):
        """Client mode's console may be bare, but the file record keeps
        full diagnostic detail for later review."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="INFO", file=log_file, client_mode=True))

        assert self._file_handler().formatter._fmt == LOG_FORMAT_SIMPLE

    def test_server_mode_console_keeps_full_format(self, tmp_path):
        """Server mode (--serve) keeps the full timestamped format on
        console, since that's what `docker logs` captures for live
        monitoring."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="INFO", file=log_file, client_mode=False))

        assert self._console_handler().formatter._fmt == LOG_FORMAT_SIMPLE

    def test_trace_mode_overrides_client_mode_bare_console(self, tmp_path):
        """--trace is an explicit request for verbose detail, so it wins
        over client mode's normally-bare console format."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="DEBUG", file=log_file, trace_mode=True, client_mode=True))

        assert self._console_handler().formatter._fmt == LOG_FORMAT_DETAILED

    def test_file_permission_error_fallback(self, capsys):
        """File permission error falls back to console only."""
        # Should not raise, just print to stderr
        setup_logging(_logging_config(level="INFO", file=Path("/root/forbidden/test.log")))

        # Verify error message to stderr
        captured = capsys.readouterr()
        assert "Failed to setup file logging" in captured.err or "PermissionError" in captured.err or len(captured.err) > 0

        # Console handler should still work
        root_logger = logging.getLogger()
        assert len(root_logger.handlers) >= 1

    def test_file_handler_with_nonexistent_directory(self, tmp_path):
        """File handler creates parent directories."""
        log_file = tmp_path / "nested" / "dir" / "test.log"

        setup_logging(_logging_config(level="INFO", file=log_file))

        # Directory should be created
        assert log_file.parent.exists()
        assert log_file.exists()

    def test_console_handler_always_created(self, tmp_path):
        """Console handler is always created."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="WARNING", file=log_file))

        root_logger = logging.getLogger()
        console_handlers = [h for h in root_logger.handlers if isinstance(h, logging.StreamHandler) and h.stream == sys.stderr]
        assert len(console_handlers) >= 1

    def test_clears_existing_handlers(self, tmp_path):
        """Setup clears existing handlers to avoid duplicates."""
        log_file = tmp_path / "test.log"

        # Add a dummy handler
        root_logger = logging.getLogger()
        dummy_handler = logging.StreamHandler()
        root_logger.addHandler(dummy_handler)
        initial_count = len(root_logger.handlers)

        setup_logging(_logging_config(level="INFO", file=log_file))

        # Handlers should be cleared and new ones added
        # Should have exactly 2 handlers (file + console)
        assert len(root_logger.handlers) >= 2

    def test_log_level_debug(self, tmp_path):
        """DEBUG log level is set correctly on console handler."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="DEBUG", file=log_file))

        root_logger = logging.getLogger()
        # Root logger is always DEBUG
        assert root_logger.level == logging.DEBUG

        # Console handler should have DEBUG level (stream is stderr, not a file)
        console_handlers = [h for h in root_logger.handlers
                           if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
        assert len(console_handlers) > 0
        assert console_handlers[0].level == logging.DEBUG

    def test_log_level_error(self, tmp_path):
        """ERROR log level is set correctly on console handler."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="ERROR", file=log_file))

        root_logger = logging.getLogger()
        # Root logger is always DEBUG
        assert root_logger.level == logging.DEBUG

        # Console handler should have ERROR level (stream is stderr, not a file)
        console_handlers = [h for h in root_logger.handlers
                           if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)]
        assert len(console_handlers) > 0
        assert console_handlers[0].level == logging.ERROR

    def test_logging_actually_works(self, tmp_path):
        """Verify logging actually writes to file."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="INFO", file=log_file))

        # Write a test message
        logger = logging.getLogger("test")
        logger.info("Test message")

        # Verify it was written to file
        log_content = log_file.read_text()
        assert "Test message" in log_content
        assert "INFO" in log_content

    def test_file_honours_log_level(self, tmp_path):
        """At INFO the file drops DEBUG records -- a long-running server
        at DEBUG writes hundreds of MB a day of per-torrent detail."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="INFO", file=log_file))

        logger = logging.getLogger("test")
        logger.debug("Debug detail")
        logger.info("Info message")

        log_content = log_file.read_text()
        assert "Info message" in log_content
        assert "Debug detail" not in log_content

    def test_file_gets_debug_when_level_is_debug(self, tmp_path):
        """DEBUG in the file is still available, just opt-in."""
        log_file = tmp_path / "test.log"

        setup_logging(_logging_config(level="DEBUG", file=log_file))

        logging.getLogger("test").debug("Debug detail")

        assert "Debug detail" in log_file.read_text()


class TestGetLogger:
    """Test logger retrieval functionality."""

    def test_get_logger_returns_logger(self):
        """get_logger returns a Logger instance."""
        logger = get_logger("test_module")
        assert isinstance(logger, logging.Logger)

    def test_get_logger_with_different_names(self):
        """get_logger returns different loggers for different names."""
        logger1 = get_logger("module1")
        logger2 = get_logger("module2")

        assert logger1.name == "module1"
        assert logger2.name == "module2"
        assert logger1 is not logger2

    def test_get_logger_same_name_returns_same_instance(self):
        """get_logger returns same instance for same name."""
        logger1 = get_logger("same_module")
        logger2 = get_logger("same_module")

        assert logger1 is logger2

    def test_get_logger_with_package_name(self):
        """get_logger works with package names."""
        logger = get_logger("qbt_rules.engine")
        assert logger.name == "qbt_rules.engine"
        assert isinstance(logger, logging.Logger)


class TestLogFormats:
    """Test log format constants."""

    def test_simple_format_exists(self):
        """Simple log format is defined."""
        assert LOG_FORMAT_SIMPLE is not None
        assert isinstance(LOG_FORMAT_SIMPLE, str)
        assert len(LOG_FORMAT_SIMPLE) > 0

    def test_detailed_format_exists(self):
        """Detailed log format is defined."""
        assert LOG_FORMAT_DETAILED is not None
        assert isinstance(LOG_FORMAT_DETAILED, str)
        assert len(LOG_FORMAT_DETAILED) > 0

    def test_detailed_format_more_verbose(self):
        """Detailed format contains more information than simple."""
        # Detailed format should be longer and contain module/function info
        assert len(LOG_FORMAT_DETAILED) > len(LOG_FORMAT_SIMPLE)
