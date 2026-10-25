"""
Centralized argument parsing for qBittorrent automation triggers
Provides consistent CLI interface across all triggers
"""

import argparse
import os
from pathlib import Path
from typing import Optional

from qbt_rules.__version__ import __version__
from qbt_rules.rules_validation import ERROR, format_path, lint_rules


def smart_config_default() -> str:
    """
    Determine smart default for config directory

    Returns ./config if it exists (bare metal), otherwise /config (Docker)
    """
    local_config = Path('./config')
    if local_config.exists() and local_config.is_dir():
        return './config'
    return '/config'


def create_parser() -> argparse.ArgumentParser:
    """
    Create argument parser for qbt-rules CLI

    Returns:
        Configured ArgumentParser
    """
    
    parser = argparse.ArgumentParser(
        description=f'qbt-rules - rules engine',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Mode selection
    parser.add_argument(
        '--serve',
        action='store_true',
        help='Run in server mode (HTTP API + worker)'
    )

    # Execution arguments (client mode)
    parser.add_argument(
        '--context',
        type=str,
        default=None,
        help='Context filter for rules (any custom string identifier you define)',
        metavar="CONTEXT"
    )

    parser.add_argument(
        '--hash',
        type=str,
        default=None,
        help='Torrent hash to process (40-character hex string)',
        metavar="HASH"
    )

    parser.add_argument(
        '--wait',
        action='store_true',
        help='Wait for job to complete (polls server for status)'
    )

    # Common configuration arguments
    parser.add_argument(
        '--config-dir',
        type=Path,
        default=None,
        help=f'Path to configuration directory (default: {smart_config_default()} or CONFIG_DIR env var)',
        metavar="DIR"
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Simulate changes without making them (useful for testing)'
    )

    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Set logging verbosity (default: INFO)',
        metavar="LEVEL"
    )

    parser.add_argument(
        '--trace',
        action='store_true',
        help='Enable trace mode with detailed logging (module/function/line)'
    )

    # Server configuration (for --serve mode)
    parser.add_argument(
        '--server-host',
        type=str,
        help='Server bind address (default: 0.0.0.0)',
        metavar="HOST"
    )

    parser.add_argument(
        '--server-port',
        type=int,
        help='Server port (default: 5000)',
        metavar="PORT"
    )

    parser.add_argument(
        '--server-api-key',
        type=str,
        help='Server API key for authentication',
        metavar="KEY"
    )

    parser.add_argument(
        '--server-workers',
        type=int,
        help='Gunicorn worker processes (default: 1)',
        metavar="NUM"
    )

    # Client configuration (for client mode)
    parser.add_argument(
        '--client-server-url',
        type=str,
        help='Server URL for client (default: http://localhost:5000)',
        metavar="URL"
    )

    parser.add_argument(
        '--client-api-key',
        type=str,
        help='Client API key for authentication',
        metavar="KEY"
    )

    # Queue configuration (for --serve mode)
    parser.add_argument(
        '--queue-backend',
        choices=['sqlite', 'redis'],
        help='Queue backend (default: sqlite)',
        metavar="BACKEND"
    )

    parser.add_argument(
        '--queue-sqlite-path',
        type=str,
        help='SQLite database path (default: /config/qbt-rules.db)',
        metavar="PATH"
    )

    parser.add_argument(
        '--queue-redis-url',
        type=str,
        help='Redis connection URL (default: redis://localhost:6379/0)',
        metavar="URL"
    )

    # Job management commands
    parser.add_argument(
        '--list-jobs',
        action='store_true',
        help='List recent jobs'
    )

    parser.add_argument(
        '--job-status',
        type=str,
        metavar="JOB_ID",
        help='Get status of specific job'
    )

    parser.add_argument(
        '--cancel-job',
        type=str,
        metavar="JOB_ID",
        dest='cancel_job_id',
        help='Cancel pending job'
    )

    parser.add_argument(
        '--stats',
        action='store_true',
        help='Get server statistics'
    )

    parser.add_argument(
        '--status-filter',
        choices=['pending', 'processing', 'completed', 'failed', 'cancelled'],
        help='Filter jobs by status (for --list-jobs)',
        metavar="STATUS"
    )

    parser.add_argument(
        '--limit',
        type=int,
        default=20,
        help='Limit number of results (for --list-jobs, default: 20)',
        metavar="NUM"
    )

    parser.add_argument(
        '--output',
        choices=['table', 'json'],
        default='table',
        help='Output format for data-display commands: --list-jobs, --job-status, '
             '--stats, --cancel-job, --list-rules (default: table)',
        metavar="FORMAT"
    )

    # Utility arguments
    parser.add_argument(
        '--version',
        action='version',
        version=f'qbt-rules v{__version__}'
    )

    parser.add_argument(
        '--validate',
        action='store_true',
        help='Validate configuration and rules files without running'
    )

    parser.add_argument(
        '--list-rules',
        action='store_true',
        help='List all rules with priorities and exit'
    )


        

    parser.epilog = '''
Examples:

  Server Mode:
    # Start server with default settings
    qbt-rules --serve

    # Start server with custom port and API key
    qbt-rules --serve --server-port 8080 --server-api-key my-secret-key

    # Start server with Redis queue backend
    qbt-rules --serve --queue-backend redis --queue-redis-url redis://localhost:6379/0

  Client Mode (submit jobs):
    # Execute with context filter (use any custom string you define)
    qbt-rules --context weekly-cleanup

    # Execute specific torrent
    qbt-rules --context download-finished --hash abc123def456...

    # Execute and wait for completion
    qbt-rules --context nightly-maintenance --wait

    # Use custom server URL
    qbt-rules --context import-complete --client-server-url http://remote-server:5000

  Job Management:
    # List recent jobs
    qbt-rules --list-jobs

    # List pending jobs only
    qbt-rules --list-jobs --status-filter pending

    # Get job status
    qbt-rules --job-status <job-id>

    # Cancel pending job
    qbt-rules --cancel-job <job-id>

    # Get server statistics
    qbt-rules --stats

    # List jobs as JSON (for scripting)
    qbt-rules --list-jobs --output json

  Utility Commands:
    # Validate configuration
    qbt-rules --validate

    # List all rules
    qbt-rules --list-rules

    # Show version
    qbt-rules --version

Environment Variables:
  All CLI options support environment variables with QBT_RULES_* prefix.
  Use _FILE suffix for any variable to read from file (Docker secrets).

  Examples:
    QBT_RULES_SERVER_API_KEY=my-key
    QBT_RULES_SERVER_API_KEY_FILE=/run/secrets/api_key
    QBT_RULES_CLIENT_SERVER_URL=http://localhost:5000
    QBT_RULES_QUEUE_BACKEND=sqlite

See PLAN.md or config.default.yml for complete configuration reference.
    '''



    return parser


def validate_torrent_hash(torrent_hash: Optional[str]) -> str:
    """
    Validate torrent hash format

    Args:
        torrent_hash: Hash to validate

    Returns:
        Validated hash

    Raises:
        ValueError: If hash is invalid
    """
    if not torrent_hash:
        raise ValueError("Torrent hash is required")

    # qBittorrent uses 40-character SHA-1 hashes (hex)
    if len(torrent_hash) != 40:
        raise ValueError(f"Invalid torrent hash length: {len(torrent_hash)} (expected 40)")

    # Check if all characters are valid hex
    try:
        int(torrent_hash, 16)
    except ValueError:
        raise ValueError(f"Invalid torrent hash format: must be hexadecimal")

    return torrent_hash.lower()


def process_args(args: argparse.Namespace) -> Path:
    """
    Process parsed arguments and set environment variables

    Args:
        args: Parsed arguments from argparse

    Returns:
        Path to configuration directory
    """
    # Set environment variables from command-line arguments
    # (--log-level/--trace are handled directly by cli.py's
    # get_logging_config(), not via this env-var-setting indirection)
    if args.dry_run:
        os.environ['QBT_RULES_DRY_RUN'] = 'true'

    # Determine config directory
    if args.config_dir:
        config_dir = args.config_dir
    elif 'CONFIG_DIR' in os.environ:
        config_dir = Path(os.environ['CONFIG_DIR'])
    else:
        config_dir = Path(smart_config_default())

    return config_dir


def handle_utility_args(args: argparse.Namespace, config) -> Optional[int]:
    """
    Handle utility arguments (--validate, --list-rules)

    Args:
        args: Parsed arguments
        config: Loaded configuration object

    Returns:
        The utility command's exit code, or None if no utility was handled
    """
    from qbt_rules.logging import get_logger
    logger = get_logger(__name__)

    # Handle --validate
    if args.validate:
        logger.info("Validating configuration and rules...")

        try:
            # Check qBittorrent config -- deferred import avoids a circular
            # import (cli.py already imports this module at load time)
            from qbt_rules.cli import get_notifications_config, get_qbittorrent_config
            qbt_config = get_qbittorrent_config(args, config)
            required = ['host', 'username', 'password']
            missing = [k for k in required if not qbt_config.get(k)]
            if missing:
                logger.error(f"Missing qBittorrent configuration: {', '.join(missing)}")
                return 1

            logger.info(f"✓ qBittorrent connection configured: {qbt_config['host']}")

            # Check resolved rules so actions supplied through $ref are covered.
            rules = config.get_rules()
            notifications_config = get_notifications_config(args, config)
            if not rules:
                logger.warning("No rules defined in rules.yml")
            else:
                logger.info(f"✓ Loaded {len(rules)} rules")

            issues = lint_rules({'rules': rules}, notifications=notifications_config)
            for issue in issues:
                log = getattr(logger, issue.severity)
                log(f"  {format_path(issue.path)}: {issue.message}")

            errors = sum(issue.severity == ERROR for issue in issues)
            if errors:
                logger.error(f"Validation failed: {errors} error(s)")
                return 1

            logger.info("\nValidation complete! Configuration is valid.")

        except Exception as e:
            logger.error(f"Validation failed: {e}")
            return 1

        return 0

    # Handle --list-rules
    if args.list_rules:
        from qbt_rules import cli_ui

        rules = config.get_rules()
        output_format = getattr(args, 'output', 'table')

        if output_format == 'json':
            cli_ui.print_json(rules)
            return 0

        if not rules:
            cli_ui.print_message("No rules defined in rules.yml")
            return 0

        # Rules execute in file order (no sorting)
        rows = []
        for index, rule in enumerate(rules, 1):
            enabled = '✓' if rule.get('enabled', True) else '✗'
            stop = '✓' if rule.get('stop_on_match', False) else '-'

            # Get context filter (from rule level)
            context_filter = rule.get('context', 'any')
            if isinstance(context_filter, list):
                context_filter = ','.join(context_filter)

            rule_id = rule.get('id', 'unnamed')
            description = rule.get('meta', {}).get('description', '')

            rows.append([index, enabled, stop, context_filter, rule_id, description])

        cli_ui.print_table(
            headers=['#', 'Enabled', 'Stop', 'Context', 'ID', 'Description'],
            rows=rows,
            title=f"Rules ({len(rules)} total, execute in file order):"
        )
        return 0

    return None
