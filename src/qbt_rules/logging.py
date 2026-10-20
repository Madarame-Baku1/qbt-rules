"""
Centralized logging configuration for qBittorrent automation
Provides consistent logging setup across all modules and triggers
"""

import sys
import logging
from typing import Dict, Any

# Standard log formats
LOG_FORMAT_SIMPLE = '%(asctime)s | %(levelname)-8s | %(message)s'
LOG_FORMAT_DETAILED = '%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s'
CONSOLE_FORMAT_BARE = '%(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def setup_logging(logging_config: Dict[str, Any]):
    """
    Setup logging configuration with fallback to console-only

    Configures both file and console handlers, each with its own format.
    Falls back gracefully to console-only logging if file logging fails.

    Args:
        logging_config: Pre-resolved dict from cli.py's get_logging_config()
            -- {'level': str, 'file': Path, 'trace_mode': bool,
                'http_access': bool, 'client_mode': bool}
    """
    log_level = logging_config['level']
    trace_mode = logging_config['trace_mode']
    client_mode = logging_config.get('client_mode', False)

    # The log file is the persistent diagnostic record -- full format
    # (timestamp, level, and function/line under --trace) so it's there to
    # review later. It honours logging.level like the console does: at
    # DEBUG a long-running server writes hundreds of MB a day of per-torrent
    # evaluation detail, so DEBUG in the file is opt-in, not the default.
    file_log_format = LOG_FORMAT_DETAILED if trace_mode else LOG_FORMAT_SIMPLE

    # In client mode (any invocation other than --serve), console output is
    # bare message text: someone running e.g. `qbt-rules --job-status <id>`
    # interactively is looking straight at the result, not a log stream, so
    # a timestamp/level prefix on every line is just noise -- the full
    # diagnostic still lands in the file above. --trace explicitly asks for
    # verbose detail, so it wins in either mode. Server mode always keeps
    # the full format, since its console output is what `docker logs`
    # captures for live monitoring.
    if trace_mode:
        console_log_format = LOG_FORMAT_DETAILED
    elif client_mode:
        console_log_format = CONSOLE_FORMAT_BARE
    else:
        console_log_format = LOG_FORMAT_SIMPLE

    # Try to setup file logging
    file_handler = None
    try:
        log_file = logging_config['file']
        log_file.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(getattr(logging, log_level))
        file_formatter = logging.Formatter(file_log_format, datefmt=DATE_FORMAT)
        file_handler.setFormatter(file_formatter)
    except (PermissionError, OSError) as e:
        # Fall back to console-only logging
        print(f"Cannot setup file logging: {e}", file=sys.stderr)
        print(f"Continuing with console-only logging", file=sys.stderr)

    # Console handler (always works) -- stderr, not stdout, so diagnostic
    # logging never contaminates stdout data output (tables/JSON) from the
    # cli_ui layer, keeping --output json pipeable
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(getattr(logging, log_level))
    console_formatter = logging.Formatter(console_log_format, datefmt=DATE_FORMAT)
    console_handler.setFormatter(console_formatter)

    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # Clear existing handlers to avoid duplicates
    for handler in logger.handlers[:]:  # Iterate over copy
        handler.close()
        logger.removeHandler(handler)

    if file_handler:
        logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module

    Args:
        name: Module name (typically __name__)

    Returns:
        Logger instance configured for the module
    """
    return logging.getLogger(name)
