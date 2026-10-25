#!/usr/bin/env python3
"""
qbt-rules CLI - Client-server architecture

Supports two modes:
1. Server mode (--serve): Runs HTTP API server with worker
2. Client mode (default): Submits jobs to server via HTTP API

Job management commands: --list-jobs, --job-status, --cancel-job, --stats
"""

import sys
import os
import time
import requests
from pathlib import Path
from typing import Optional

from qbt_rules.arguments import create_parser, process_args, validate_torrent_hash
from qbt_rules.config import load_config, resolve_config, parse_int, parse_bool, ENV_VAR_MAP
from qbt_rules.api import QBittorrentAPI
from qbt_rules.errors import handle_errors
from qbt_rules.logging import setup_logging, get_logger
from qbt_rules import cli_ui

logger = None  # Set after logging is configured


def resolve_section_config(args, config_obj, section: str, fields: dict, parsers: Optional[dict] = None) -> dict:
    """
    Generic resolver for a config.yml section's scalar fields

    Env var names auto-derive as QBT_RULES_<SECTION>_<FIELD> (dots in
    `section` become underscores, e.g. section='integrations.sonarr' ->
    QBT_RULES_INTEGRATIONS_SONARR_<FIELD>) unless ENV_VAR_MAP has an
    explicit override for "<section>.<field>" -- needed for the handful
    of genuine naming exceptions (logging.*'s QBT_RULES_LOG_* prefix,
    engine.dry_run's bare QBT_RULES_DRY_RUN). CLI arg names auto-derive as
    <section>_<field> (dots become underscores), matching this codebase's
    existing --server-port style flag naming.

    Args:
        args: Parsed CLI arguments
        config_obj: Loaded Config instance
        section: Config section name, e.g. 'server' (dotted for nested
                 sections, e.g. 'integrations.sonarr')
        fields: {field_name: default_value}
        parsers: Optional {field_name: callable} for fields needing type
                 coercion (e.g. {'port': parse_int})

    Returns:
        Dictionary with the section's resolved configuration
    """
    parsers = parsers or {}
    result = {}
    for field, default in fields.items():
        config_key = f'{section}.{field}'
        env_var = ENV_VAR_MAP.get(
            config_key,
            f"QBT_RULES_{section.upper().replace('.', '_')}_{field.upper()}"
        )
        cli_value = getattr(args, f"{section.replace('.', '_')}_{field}", None)
        value = resolve_config(cli_value, env_var, config_obj.config, config_key, default=default)
        if field in parsers and value is not None:
            value = parsers[field](value)
        result[field] = value
    return result


def get_logging_config(args, config_obj) -> dict:
    """
    Get logging configuration from CLI args, env vars, or config file

    Doesn't use resolve_section_config()'s mechanical CLI-arg-name
    convention -- the established flag names are --log-level/--trace,
    not --logging-level/--logging-trace-mode, so each field is resolved
    by hand rather than via the generic helper. Every field still gets
    full CLI > _FILE env var > env var > config.yml > default resolution
    via resolve_config(), matching every other section.

    Returns:
        Dictionary with 'level' (str, uppercased), 'file' (Path, resolved
        relative to config_dir when given as a relative path), 'trace_mode'
        (bool), 'http_access' (bool), 'client_mode' (bool -- True for any
        invocation other than --serve, used by setup_logging() to keep
        console output bare in client mode while the log file still gets
        full diagnostic detail)
    """
    level = resolve_config(
        getattr(args, 'log_level', None),
        'QBT_RULES_LOGGING_LEVEL', config_obj.config, 'logging.level', default='INFO'
    )

    file_str = resolve_config(
        None, 'QBT_RULES_LOGGING_FILE', config_obj.config, 'logging.file',
        default='logs/qbittorrent.log'
    )
    file_path = Path(file_str)
    if not file_path.is_absolute():
        file_path = config_obj.config_dir / file_path

    trace_mode = parse_bool(resolve_config(
        True if getattr(args, 'trace', False) else None,
        'QBT_RULES_LOGGING_TRACE_MODE', config_obj.config, 'logging.trace_mode', default=False
    ))

    http_access = parse_bool(resolve_config(
        None, 'QBT_RULES_LOGGING_HTTP_ACCESS', config_obj.config, 'logging.http_access', default=False
    ))

    return {
        'level': level.upper(),
        'file': file_path,
        'trace_mode': trace_mode,
        'http_access': http_access,
        'client_mode': not getattr(args, 'serve', False),
    }


def get_qbittorrent_config(args, config_obj) -> dict:
    """
    Get qBittorrent connection configuration from CLI args, env vars, or
    config file

    Returns:
        Dictionary with 'host', 'username', 'password'
    """
    return resolve_section_config(args, config_obj, 'qbittorrent', {
        'host': 'http://localhost:8080',
        'username': 'admin',
        'password': '',
    })


def get_server_config(args, config_obj) -> dict:
    """
    Get server configuration from CLI args, env vars, or config file

    Returns:
        Dictionary with server configuration
    """
    return resolve_section_config(args, config_obj, 'server', {
        'host': '0.0.0.0',
        'port': 5000,
        'api_key': None,
        'workers': 1,
        'editor_enabled': True,
    }, parsers={'port': parse_int, 'workers': parse_int, 'editor_enabled': parse_bool})


# The api_key value config.default.yml ships with, for both server and client
PLACEHOLDER_API_KEY = 'your-secure-api-key-here-generate-a-random-key'


def get_client_config(args, config_obj) -> dict:
    """
    Get client configuration from CLI args, env vars, or config file

    When no client key is set -- or it's still the placeholder from the
    copied config.default.yml -- the server's key is used instead. The
    usual client is `docker exec <container> qbt-rules ...` inside the
    server's own container, where the server key is right there; without
    this, a setup that only sets QBT_RULES_SERVER_API_KEY sent the
    placeholder and every CLI command failed authentication.

    Returns:
        Dictionary with client configuration
    """
    client = resolve_section_config(args, config_obj, 'client', {
        'server_url': 'http://localhost:5000',
        'api_key': None,
    })
    if not client['api_key'] or client['api_key'] == PLACEHOLDER_API_KEY:
        server_key = get_server_config(args, config_obj)['api_key']
        if server_key:
            client['api_key'] = server_key
    return client


def get_queue_config(args, config_obj) -> dict:
    """
    Get queue configuration from CLI args, env vars, or config file

    Returns:
        Dictionary with queue configuration
    """
    return resolve_section_config(args, config_obj, 'queue', {
        'backend': 'sqlite',
        'sqlite_path': '/config/qbt-rules.db',
        'redis_url': 'redis://localhost:6379/0',
    })


def get_notifications_config(args, config_obj) -> dict:
    """
    Get notifications configuration from CLI args, env vars, or config file

    Resolved once at server startup (same as server_config/queue_config)
    so the notify action's default webhook URL supports _FILE secrets --
    ActionExecutor never has access to CLI args, so it can't do this
    resolution itself; it's threaded down as an already-resolved dict.

    Returns:
        Dictionary with notifications configuration
    """
    return resolve_section_config(args, config_obj, 'notifications', {
        'webhook_url': None,
        'service': 'generic',
    })


def get_integrations_config(args, config_obj) -> dict:
    """
    Get integrations configuration (Sonarr/Radarr) from CLI args, env vars,
    or config file

    Resolved once at server startup, same as notifications_config -- the
    arr_blocklist action has no access to CLI args, so its
    config (including _FILE-resolved API keys) is threaded down as an
    already-resolved dict. 'integrations' is a two-level nested section
    (integrations.sonarr.*, integrations.radarr.*), so resolve_section_config
    is called once per service with a dotted section name -- it derives
    QBT_RULES_INTEGRATIONS_SONARR_URL/_API_KEY and
    QBT_RULES_INTEGRATIONS_RADARR_URL/_API_KEY automatically, no ENV_VAR_MAP
    entries needed.

    Returns:
        Dictionary with 'sonarr' and 'radarr' keys, each {'url', 'api_key'},
        and 'trackie' ({'url', 'max_age', 'timeout'})
    """
    return {
        'sonarr': resolve_section_config(args, config_obj, 'integrations.sonarr', {
            'url': None, 'api_key': None,
        }),
        'radarr': resolve_section_config(args, config_obj, 'integrations.radarr', {
            'url': None, 'api_key': None,
        }),
        # Not an arr: trackie serves tracker-side seeding stats for the
        # tracker.* field namespace. No API key -- it's read-only and meant
        # to sit on the same internal network as qbt-rules.
        'trackie': resolve_section_config(args, config_obj, 'integrations.trackie', {
            'url': None, 'max_age': None, 'timeout': None,
        }),
    }


def get_metrics_config(args, config_obj) -> dict:
    """
    Get Prometheus metrics configuration from CLI args, env vars, or config file

    Returns:
        Dictionary with 'enabled' (bool) and 'multiproc_dir' (str)
    """
    return resolve_section_config(args, config_obj, 'metrics', {
        'enabled': False,
        'multiproc_dir': '/tmp/qbt-rules-metrics',
    }, parsers={'enabled': parse_bool})


def get_schedule_config(args, config_obj) -> list:
    """
    Get the schedule configuration (list of {cron, context} entries)

    Unlike the other get_*_config functions, this has no CLI-arg or
    per-field env-var override -- a list of recurring jobs doesn't map to
    a single flag/variable the way scalar config does. It's config.yml's
    'schedule' section as-is, already validated at load time by
    Config._load_schedule().

    Returns:
        List of schedule entry dicts (possibly empty)
    """
    return config_obj.schedule


def run_server_mode(args, config_obj, logging_config):
    """
    Run server mode - Start HTTP API server with worker

    Args:
        args: Parsed CLI arguments
        config_obj: Loaded configuration object
        logging_config: Pre-resolved dict from get_logging_config(), already
            computed once by main() for setup_logging() -- threaded through
            rather than re-resolved here
    """
    logger.info("=" * 60)
    logger.info("Starting qbt-rules server")
    logger.info("=" * 60)

    # Get configurations
    server_config = get_server_config(args, config_obj)
    queue_config = get_queue_config(args, config_obj)
    notifications_config = get_notifications_config(args, config_obj)
    integrations_config = get_integrations_config(args, config_obj)
    metrics_config = get_metrics_config(args, config_obj)

    # Initialize metrics (must happen before worker/engine/scheduler/server
    # are imported below, so PROMETHEUS_MULTIPROC_DIR is set in os.environ
    # before prometheus_client is first imported anywhere in this process --
    # see metrics.py's module docstring for why this ordering matters)
    if metrics_config['enabled']:
        multiproc_dir = Path(metrics_config['multiproc_dir'])
        multiproc_dir.mkdir(parents=True, exist_ok=True)
        for stale_file in multiproc_dir.glob('*.db'):
            stale_file.unlink()
        os.environ['PROMETHEUS_MULTIPROC_DIR'] = str(multiproc_dir)

    from qbt_rules import metrics
    metrics.init(enabled=metrics_config['enabled'])
    if metrics_config['enabled']:
        logger.info(f"Metrics enabled (multiproc dir: {metrics_config['multiproc_dir']})")

    # Validate API key
    if not server_config['api_key']:
        logger.error("Server API key is required. Set via:")
        logger.error("  - CLI: --server-api-key <key>")
        logger.error("  - Env: QBT_RULES_SERVER_API_KEY or QBT_RULES_SERVER_API_KEY_FILE")
        logger.error("  - Config: server.api_key in config.yml")
        sys.exit(1)

    # Initialize queue
    from qbt_rules.queue_manager import create_queue
    queue = create_queue(
        backend=queue_config['backend'],
        db_path=queue_config['sqlite_path'],
        redis_url=queue_config['redis_url']
    )
    logger.info(f"Queue backend: {queue.__class__.__name__}")

    # Initialize qBittorrent API (lazy initialization - won't connect until first job)
    qbt_config = get_qbittorrent_config(args, config_obj)
    api = QBittorrentAPI(
        host=qbt_config['host'],
        username=qbt_config['username'],
        password=qbt_config['password'],
        connect_now=False  # Defer connection until first job execution
    )
    logger.info(f"qBittorrent: {qbt_config['host']} (will connect on first job)")

    # Initialize worker
    from qbt_rules.worker import Worker
    worker = Worker(
        queue=queue,
        api=api,
        config=config_obj,
        notifications_config=notifications_config,
        integrations_config=integrations_config
    )
    worker.start()
    logger.info("Worker started")

    # Initialize scheduler (internal cron -- must start here, before
    # run_server()/Gunicorn forks, and must NOT be restarted per-fork the
    # way the worker thread is; see Scheduler's docstring)
    from qbt_rules.scheduler import Scheduler
    schedule_entries = get_schedule_config(args, config_obj)
    scheduler = Scheduler(queue=queue, entries=schedule_entries)
    scheduler.start()

    # Create Flask app
    from qbt_rules.server import create_app, run_server
    app = create_app(
        queue_manager=queue,
        worker_instance=worker,
        api_key=server_config['api_key'],
        config=config_obj,
        metrics_config=metrics_config,
        editor=server_config['editor_enabled']
    )

    # Run server
    logger.info(f"Starting server on {server_config['host']}:{server_config['port']}")
    logger.info("Press Ctrl+C to stop")
    logger.info("=" * 60)

    # Get HTTP access logging preference
    log_http_access = logging_config['http_access']

    try:
        run_server(
            app=app,
            host=server_config['host'],
            port=server_config['port'],
            workers=server_config['workers'],
            log_http_access=log_http_access,
            metrics_enabled=metrics_config['enabled']
        )
    except KeyboardInterrupt:
        logger.info("\nShutting down...")
        scheduler.stop()
        worker.stop()
        logger.info("Server stopped")


def run_client_mode(args, config_obj):
    """
    Run client mode - Submit job to server via HTTP API

    Args:
        args: Parsed CLI arguments
        config_obj: Loaded configuration object
    """
    client_config = get_client_config(args, config_obj)

    # Validate API key
    if not client_config['api_key']:
        logger.error("Client API key is required. Set via:")
        logger.error("  - CLI: --client-api-key <key>")
        logger.error("  - Env: QBT_RULES_CLIENT_API_KEY or QBT_RULES_CLIENT_API_KEY_FILE")
        logger.error("  - Config: client.api_key in config.yml")
        sys.exit(1)

    # Determine context and hash
    context = args.context if hasattr(args, 'context') and args.context else None
    hash_filter = args.hash if hasattr(args, 'hash') and args.hash else None

    # Validate hash if provided
    if hash_filter:
        try:
            hash_filter = validate_torrent_hash(hash_filter)
        except ValueError as e:
            logger.error(f"Invalid torrent hash: {e}")
            sys.exit(1)

    # Submit job to server
    server_url = client_config['server_url'].rstrip('/')
    api_key = client_config['api_key']

    logger.info(f"Submitting job to {server_url}")
    logger.info(f"  Context: {context or 'none'}")
    logger.info(f"  Hash: {hash_filter or 'all torrents'}")
    dry_run = bool(getattr(args, 'dry_run', False))
    if dry_run:
        logger.info("  Dry run: yes")

    params = {
        'context': context,
        'hash': hash_filter,
        'key': api_key
    }
    if dry_run:
        params['dry_run'] = 'true'

    try:
        response = requests.post(
            f"{server_url}/api/execute",
            params=params,
            timeout=10
        )

        if response.status_code == 202:
            job = response.json()
            if dry_run and job.get('dry_run') is not True:
                # A server older than per-job dry-run ignores the parameter
                # and would run the job for real -- cancel it before it starts
                _cancel_unconfirmed_dry_run(server_url, api_key, job['job_id'])
                sys.exit(1)
            logger.info(f"✓ Job queued: {job['job_id']}")
            logger.info(f"  Status: {job['status']}")
            logger.info(f"  Queued at: {job['created_at']}")

            # Wait for completion if requested
            if args.wait:
                wait_for_job(server_url, api_key, job['job_id'])

        elif response.status_code == 401:
            logger.error("Authentication failed - check API key")
            sys.exit(1)
        else:
            logger.error(f"Server error: {response.status_code}")
            logger.error(f"Response: {response.text}")
            sys.exit(1)

    except requests.exceptions.ConnectionError:
        logger.error(f"Cannot connect to server at {server_url}")
        logger.error("Is the server running? Start with: qbt-rules --serve")
        sys.exit(1)
    except requests.exceptions.Timeout:
        logger.error(f"Connection to {server_url} timed out")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


def _cancel_unconfirmed_dry_run(server_url: str, api_key: str, job_id: str):
    """Cancel a job the server queued without the dry-run the client asked for"""
    logger.error("The server didn't confirm dry-run for this job (it's older than "
                 "per-job dry-run support), so it would have run for real.")
    try:
        response = requests.delete(f"{server_url}/api/jobs/{job_id}",
                                   params={'key': api_key}, timeout=10)
        cancelled = response.status_code == 200
    except requests.exceptions.RequestException:
        cancelled = False
    if cancelled:
        logger.error(f"Cancelled job {job_id} before it ran.")
    else:
        logger.error(f"Could not cancel job {job_id}; it may already be running. "
                     f"Check it with --job-status {job_id}.")
    logger.error("Upgrade the server, or set dry_run on the server instead "
                 "(QBT_RULES_DRY_RUN=true).")


def wait_for_job(server_url: str, api_key: str, job_id: str):
    """
    Poll server until job completes

    Args:
        server_url: Server URL
        api_key: API key
        job_id: Job ID to monitor
    """
    logger.info(f"\nWaiting for job {job_id} to complete...")

    poll_interval = 2  # seconds
    max_wait = 300  # 5 minutes
    elapsed = 0

    while elapsed < max_wait:
        try:
            response = requests.get(
                f"{server_url}/api/jobs/{job_id}",
                params={'key': api_key},
                timeout=10
            )

            if response.status_code == 200:
                job = response.json()
                status = job['status']

                if status == 'completed':
                    logger.info(f"✓ Job completed successfully")
                    result = job.get('result', {})
                    logger.info(f"  Torrents processed: {result.get('torrents_processed', 0)}")
                    logger.info(f"  Rules matched: {result.get('rules_matched', 0)}")
                    logger.info(f"  Actions executed: {result.get('actions_executed', 0)}")
                    return
                elif status == 'failed':
                    logger.error(f"✗ Job failed")
                    error = job.get('error', 'Unknown error')
                    logger.error(f"  Error: {error}")
                    sys.exit(1)
                elif status == 'cancelled':
                    logger.warning(f"Job was cancelled")
                    sys.exit(1)
                else:
                    # Still processing
                    logger.info(f"  Status: {status} (waiting...)")

            time.sleep(poll_interval)
            elapsed += poll_interval

        except Exception as e:
            logger.error(f"Error polling job status: {e}")
            sys.exit(1)

    logger.error(f"Job did not complete within {max_wait}s")
    sys.exit(1)


def list_jobs_command(args, config_obj):
    """List jobs command"""
    client_config = get_client_config(args, config_obj)
    server_url = client_config['server_url'].rstrip('/')
    api_key = client_config['api_key']

    status_filter = getattr(args, 'status_filter', None)
    limit = getattr(args, 'limit', 20)
    output_format = getattr(args, 'output', 'table')

    try:
        response = requests.get(
            f"{server_url}/api/jobs",
            params={
                'status': status_filter,
                'limit': limit,
                'key': api_key
            },
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            jobs = data['jobs']

            if output_format == 'json':
                cli_ui.print_json(data)
                return

            if not jobs:
                cli_ui.print_message("No jobs found")
                return

            cli_ui.print_table(
                headers=['Job ID', 'Status', 'Context', 'Created'],
                rows=[
                    [job['job_id'], job['status'], job.get('context') or 'none', job['created_at']]
                    for job in jobs
                ],
                title=f"Jobs (showing {len(jobs)} of {data['total']} total):"
            )

        else:
            logger.error(f"Server error: {response.status_code}")

    except Exception as e:
        logger.error(f"Error listing jobs: {e}")
        sys.exit(1)


def job_status_command(args, config_obj):
    """Get job status command"""
    client_config = get_client_config(args, config_obj)
    server_url = client_config['server_url'].rstrip('/')
    api_key = client_config['api_key']

    job_id = args.job_status
    output_format = getattr(args, 'output', 'table')

    try:
        response = requests.get(
            f"{server_url}/api/jobs/{job_id}",
            params={'key': api_key},
            timeout=10
        )

        if response.status_code == 200:
            job = response.json()

            if output_format == 'json':
                cli_ui.print_json(job)
                return

            cli_ui.print_kv_section("Job Details:", [
                ("Job ID", job['job_id']),
                ("Status", job['status']),
                ("Context", job.get('context') or 'none'),
                ("Hash", job.get('hash') or 'all'),
                ("Created", job['created_at']),
                ("Started", job['started_at'] or 'not started'),
                ("Completed", job['completed_at'] or 'not completed'),
            ])

            if job.get('result'):
                cli_ui.print_kv_section("Result:", list(job['result'].items()), indent=2)

            if job.get('error'):
                cli_ui.print_message(f"\n  Error:\n    {job['error']}")

        elif response.status_code == 404:
            logger.error(f"Job not found: {job_id}")
        else:
            logger.error(f"Server error: {response.status_code}")

    except Exception as e:
        logger.error(f"Error getting job status: {e}")
        sys.exit(1)


def cancel_job_command(args, config_obj):
    """Cancel job command"""
    client_config = get_client_config(args, config_obj)
    server_url = client_config['server_url'].rstrip('/')
    api_key = client_config['api_key']

    job_id = args.cancel_job_id
    output_format = getattr(args, 'output', 'table')

    try:
        response = requests.delete(
            f"{server_url}/api/jobs/{job_id}",
            params={'key': api_key},
            timeout=10
        )

        if response.status_code == 200:
            if output_format == 'json':
                cli_ui.print_json(response.json())
            else:
                cli_ui.print_message(f"✓ Job cancelled: {job_id}")
        elif response.status_code == 400:
            error = response.json()
            logger.error(f"Cannot cancel job: {error['message']}")
        elif response.status_code == 404:
            logger.error(f"Job not found: {job_id}")
        else:
            logger.error(f"Server error: {response.status_code}")

    except Exception as e:
        logger.error(f"Error cancelling job: {e}")
        sys.exit(1)


def stats_command(args, config_obj):
    """Get server stats command"""
    client_config = get_client_config(args, config_obj)
    server_url = client_config['server_url'].rstrip('/')
    api_key = client_config['api_key']

    output_format = getattr(args, 'output', 'table')

    try:
        response = requests.get(
            f"{server_url}/api/stats",
            params={'key': api_key},
            timeout=10
        )

        if response.status_code == 200:
            stats = response.json()

            if output_format == 'json':
                cli_ui.print_json(stats)
                return

            cli_ui.print_message("\nServer Statistics:")

            cli_ui.print_kv_section("Jobs:", [
                ("Total", stats['jobs']['total']),
                ("Pending", stats['jobs']['pending']),
                ("Processing", stats['jobs']['processing']),
                ("Completed", stats['jobs']['completed']),
                ("Failed", stats['jobs']['failed']),
                ("Cancelled", stats['jobs']['cancelled']),
            ], indent=2)

            cli_ui.print_kv_section("Performance:", [
                ("Average execution time", stats['performance']['average_execution_time'] or 'N/A'),
            ], indent=2)

            cli_ui.print_kv_section("Queue:", [
                ("Backend", stats['queue']['backend']),
                ("Depth", stats['queue']['depth']),
            ], indent=2)

            cli_ui.print_kv_section("Worker:", [
                ("Status", stats['worker']['status']),
                ("Last job completed", stats['worker']['last_job_completed'] or 'never'),
            ], indent=2)

        else:
            logger.error(f"Server error: {response.status_code}")

    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        sys.exit(1)


@handle_errors
def main():
    """Main entry point for qbt-rules CLI"""
    global logger

    # Parse arguments
    parser = create_parser()
    args = parser.parse_args()

    # Process arguments and get config directory
    config_dir = process_args(args)

    # Load configuration
    config = load_config(config_dir)

    # Setup logging
    logging_config = get_logging_config(args, config)
    setup_logging(logging_config)
    logger = get_logger(__name__)

    # Handle utility arguments (--validate, --list-rules)
    from qbt_rules.arguments import handle_utility_args
    utility_exit_code = handle_utility_args(args, config)
    if utility_exit_code is not None:
        sys.exit(utility_exit_code)

    # Determine mode
    if args.serve:
        # Server mode
        run_server_mode(args, config, logging_config)
    elif args.list_jobs:
        # List jobs command
        list_jobs_command(args, config)
    elif args.job_status:
        # Job status command
        job_status_command(args, config)
    elif args.cancel_job_id:
        # Cancel job command
        cancel_job_command(args, config)
    elif args.stats:
        # Stats command
        stats_command(args, config)
    else:
        # Client mode (default)
        run_client_mode(args, config)

    sys.exit(0)


if __name__ == '__main__':
    main()
