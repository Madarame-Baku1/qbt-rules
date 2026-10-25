"""
HTTP API Server - Flask application with job queue integration

Provides REST API for:
- Job execution (queueing jobs)
- Job status and management
- Health checks and statistics
- Authentication via API key
"""

import os
import re
import secrets
import hashlib
import time
import logging
import yaml
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from functools import wraps
from urllib.parse import urlparse, parse_qs, urlencode

from flask import Flask, request, jsonify, Response, render_template, redirect, url_for, session, g

from qbt_rules.queue_manager import QueueManager, JobStatus
from qbt_rules.worker import Worker
from qbt_rules.config import Config, parse_bool
from qbt_rules.config_store import (
    EditableFile, FileConflictError, FileNotWritableError, redacted_config_view,
)
from qbt_rules.rules_validation import validate_rules_text
from qbt_rules.__version__ import __version__
from qbt_rules import metrics, schema

# Accepted spellings for boolean query parameters (parse_bool's true set
# plus their opposites)
BOOL_VALUES = {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}

logger = logging.getLogger(__name__)

# Global references (set by create_app)
queue: QueueManager = None
worker: Worker = None
api_key_config: str = None
dashboard_config: Optional[Config] = None
metrics_enabled: bool = False
editor_enabled: bool = True
rules_store: Optional[EditableFile] = None

# Largest request body accepted -- far beyond any real rules.yml, small
# enough that a validate/save request can't be used to exhaust memory
MAX_REQUEST_BYTES = 2 * 1024 * 1024

SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


def _timeago(value) -> str:
    """
    Jinja filter: render a timestamp as relative time ("2 hours ago")

    Args:
        value: A timezone-aware datetime (as list_jobs()/get_job() return
            for created_at/started_at/completed_at), an ISO-8601 string
            (the shape some unit-test fixtures use directly), or None

    Returns:
        A human-readable relative time string, the original string
        unchanged if it isn't valid ISO-8601, or '' for None
    """
    if value is None:
        return ''

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    seconds = (datetime.now(timezone.utc) - value).total_seconds()
    if seconds < 60:
        return 'just now'

    for unit_seconds, unit_name in (
        (86400 * 365, 'year'), (86400 * 30, 'month'), (86400, 'day'),
        (3600, 'hour'), (60, 'minute'),
    ):
        count = int(seconds / unit_seconds)
        if count >= 1:
            return f"{count} {unit_name}{'' if count == 1 else 's'} ago"

    return 'just now'


def create_app(
    queue_manager: QueueManager,
    worker_instance: Worker,
    api_key: str,
    config: Optional[Config] = None,
    metrics_config: Optional[Dict[str, Any]] = None,
    editor: bool = True
) -> Flask:
    """
    Create and configure Flask application

    Args:
        queue_manager: Queue manager instance
        worker_instance: Worker instance
        api_key: API authentication key
        config: Loaded Config instance, used by the read-only /rules
            view. Optional (defaults to None) so existing callers that don't
            need the dashboard's rules view are unaffected; that one route
            reports itself unavailable if config wasn't provided
        metrics_config: Resolved {'enabled': bool, 'multiproc_dir': str}
            from cli.py's get_metrics_config(). Optional (defaults to None,
            treated as disabled) -- metrics.init() must already have been
            called by the caller before this, since that's what actually
            makes prometheus_client importable/configured; this just
            controls whether /metrics and the HTTP request hooks are wired up
        editor: server.editor_enabled -- whether the rules editor may save
            rules.yml. False leaves it viewable (and validating) but
            read-only. Saving also needs config, and a writable rules.yml

    Returns:
        Configured Flask app
    """
    global queue, worker, api_key_config, dashboard_config, metrics_enabled, editor_enabled, rules_store

    queue = queue_manager
    worker = worker_instance
    api_key_config = api_key
    dashboard_config = config
    metrics_enabled = bool(metrics_config and metrics_config.get('enabled'))
    editor_enabled = editor
    rules_store = EditableFile(config.rules_file) if config is not None else None

    app = Flask(__name__)
    app.config['JSON_SORT_KEYS'] = False
    app.config['MAX_CONTENT_LENGTH'] = MAX_REQUEST_BYTES
    # Lax keeps the dashboard session cookie off cross-site POST/PUTs --
    # the first line of defence for the editor's write endpoints, with the
    # X-CSRF-Token check in require_api_key_or_session as the second
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.jinja_env.filters['timeago'] = _timeago

    # Signs the dashboard session cookie -- derived deterministically from
    # api_key rather than a fresh random value, so it's identical across
    # every Gunicorn worker process (each runs its own create_app() call;
    # a per-process random secret would make a session cookie signed by
    # one worker fail to validate on another under server.workers > 1).
    # Hashed rather than reused as-is so the raw API key is never doing
    # double duty as both the auth credential and the cookie-signing key.
    app.secret_key = hashlib.sha256(f'qbt-rules-dashboard-session:{api_key}'.encode()).digest()

    @app.context_processor
    def _inject_version():
        """Makes {{ version }} available in every dashboard template via
        base.html's shared topbar, without threading it through every
        individual route's render_template() call -- and {{ csrf_token() }}
        for pages that call the JSON API with the session cookie."""
        return {'version': __version__, 'csrf_token': _csrf_token}

    # Disable Flask's default logger (use our configured logger instead)
    app.logger.disabled = True
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # Register blueprints/routes
    register_routes(app)
    register_editor_routes(app)
    register_dashboard_routes(app)
    if metrics_enabled:
        register_metrics_routes(app)
        _register_http_metrics_hooks(app)

    logger.info("Flask application created")
    return app


def require_api_key(f):
    """
    Decorator for endpoints requiring API key authentication

    Checks for API key in:
    1. Query parameter: ?key=xxx
    2. Header: X-API-Key: xxx

    Returns 401 if missing or invalid.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get API key from query param or header
        key = request.args.get('key') or request.headers.get('X-API-Key')

        # Constant-time comparison to prevent timing attacks
        if not key or not secrets.compare_digest(key, api_key_config):
            return jsonify({
                'error': 'Unauthorized',
                'message': 'Invalid or missing API key'
            }), 401

        return f(*args, **kwargs)

    return decorated_function


def _csrf_token() -> str:
    """This session's CSRF token, created on first use"""
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']


def require_api_key_or_session(f):
    """
    For JSON endpoints the dashboard's own pages call from the browser

    Accepts the API key exactly like require_api_key (scripts keep
    working unchanged), or the dashboard's login session. A session is a
    cookie the browser attaches automatically, so a state-changing request
    riding on one must also carry the page's X-CSRF-Token header -- which
    another site can't read or set -- or it's refused. Unauthenticated
    requests get the same JSON 401 as require_api_key, never a redirect.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        key = request.args.get('key') or request.headers.get('X-API-Key')
        if key:
            if secrets.compare_digest(key, api_key_config):
                return f(*args, **kwargs)
        elif session.get('authenticated'):
            if request.method in SAFE_METHODS:
                return f(*args, **kwargs)
            sent = request.headers.get('X-CSRF-Token', '')
            expected = session.get('csrf_token', '')
            if sent and expected and secrets.compare_digest(sent, expected):
                return f(*args, **kwargs)
            return jsonify({
                'error': 'Forbidden',
                'message': 'Missing or invalid X-CSRF-Token header -- reload the page and try again'
            }), 403

        return jsonify({
            'error': 'Unauthorized',
            'message': 'Invalid or missing API key'
        }), 401

    return decorated_function


def _next_url_without_key() -> str:
    """
    Build a same-origin path+query string for the current request, with
    any ?key= stripped out -- used as the "next" destination a rejected
    dashboard request redirects back to after a successful login.
    """
    args = request.args.to_dict(flat=False)
    args.pop('key', None)
    query = '&'.join(f'{k}={v}' for k, vs in args.items() for v in vs)
    return request.path + (f'?{query}' if query else '')


def _sanitize_next(next_url: Optional[str]) -> str:
    """
    Reduce an arbitrary ?next= value down to a same-origin path+query,
    discarding any scheme/host component and any embedded ?key=.

    Used both when rendering the login form (so its hidden `next` field
    only ever holds a safe value) and again right before issuing the
    post-login redirect (defense in depth -- that hidden field is still
    client-controlled input by the time it comes back in the POST body).
    This is what makes a crafted next unable to send anyone off-site,
    since redirect() only ever receives something already reduced to a
    bare path+query, never a full URL, and can't be tricked into treating
    a scheme-relative or absolute URL as one.
    """
    parsed = urlparse(next_url or '')
    params = parse_qs(parsed.query)
    params.pop('key', None)
    query = urlencode(params, doseq=True)
    return (parsed.path or '/') + (f'?{query}' if query else '')


def require_api_key_dashboard(f):
    """
    Like require_api_key, but for browser-facing dashboard routes

    A scripted API client can handle a JSON 401 -- a human in a browser
    can't do anything useful with one. A valid ?key=/header still works
    as a direct, one-shot bootstrap (and refreshes the session), but
    normal navigation no longer needs the key repeated in every URL:
    a successful check establishes a signed session cookie, checked
    here on every subsequent request before falling back to requiring
    the key again. On neither a valid key nor a valid session, redirects
    to the /login form, carrying the originally requested URL through as
    ?next= so a successful login lands back where the user was headed.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        key = request.args.get('key') or request.headers.get('X-API-Key')

        if key and secrets.compare_digest(key, api_key_config):
            session.permanent = True
            session['authenticated'] = True
            return f(*args, **kwargs)

        if session.get('authenticated'):
            return f(*args, **kwargs)

        login_kwargs = {'next': _next_url_without_key()}
        if key:
            # A key was provided but it was wrong, as opposed to a first
            # visit with no key/session at all -- show an error on the form.
            login_kwargs['error'] = 1
        return redirect(url_for('login', **login_kwargs))

    return decorated_function


def register_routes(app: Flask):
    """Register all API routes"""

    @app.route('/api/execute', methods=['POST'])
    @require_api_key_or_session
    def execute():
        """
        Queue job for execution

        Query Parameters:
            context (optional): Context filter (weekly-cleanup, torrent-imported, etc.)
            hash (optional): Torrent hash filter
            dry_run (optional): true to run this job in dry-run mode. Only
                adds dry-run; a server configured for dry-run stays in it
            key (required): API key

        Returns:
            202: Job queued successfully
            400: Invalid parameters
            401: Unauthorized
        """
        context = request.args.get('context')
        hash_filter = request.args.get('hash')
        dry_run_arg = request.args.get('dry_run')
        if dry_run_arg is not None and dry_run_arg.lower() not in BOOL_VALUES:
            # Reject rather than guess: a typo here must not run deletes for real
            return jsonify({
                'error': 'Bad Request',
                'message': f"dry_run must be one of {', '.join(sorted(BOOL_VALUES))}"
            }), 400
        dry_run = parse_bool(dry_run_arg)

        try:
            # Enqueue job
            job_id = queue.enqueue(context=context, hash_filter=hash_filter, dry_run=dry_run)

            # Get full job details
            job = queue.get_job(job_id)

            logger.info(f"Job queued: {job_id} (context={context}, hash={hash_filter}"
                        f"{', dry_run=True' if dry_run else ''})")

            return jsonify(job), 202

        except Exception as e:
            logger.error(f"Error queueing job: {e}", exc_info=True)
            return jsonify({
                'error': 'Internal Server Error',
                'message': str(e)
            }), 500

    @app.route('/api/jobs', methods=['GET'])
    @require_api_key
    def list_jobs():
        """
        List jobs with filtering and pagination

        Query Parameters:
            status (optional): Filter by status
            context (optional): Filter by context
            limit (optional): Max results (default: 50, max: 100)
            offset (optional): Pagination offset (default: 0)
            key (required): API key

        Returns:
            200: List of jobs
            401: Unauthorized
        """
        status = request.args.get('status')
        context = request.args.get('context')
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))

        # Validate status
        if status and not QueueManager.validate_status(status):
            return jsonify({
                'error': 'Bad Request',
                'message': f'Invalid status: {status}'
            }), 400

        try:
            jobs = queue.list_jobs(
                status=status,
                context=context,
                limit=limit,
                offset=offset
            )

            total = queue.count_jobs(status=status)

            return jsonify({
                'total': total,
                'limit': limit,
                'offset': offset,
                'jobs': jobs
            }), 200

        except Exception as e:
            logger.error(f"Error listing jobs: {e}", exc_info=True)
            return jsonify({
                'error': 'Internal Server Error',
                'message': str(e)
            }), 500

    @app.route('/api/jobs/<job_id>', methods=['GET'])
    @require_api_key_or_session
    def get_job(job_id: str):
        """
        Get job by ID

        Path Parameters:
            job_id: Job ID (UUID)

        Query Parameters:
            key (required): API key

        Returns:
            200: Job details
            404: Job not found
            401: Unauthorized
        """
        job = queue.get_job(job_id)

        if not job:
            return jsonify({
                'error': 'Not Found',
                'message': f'Job not found: {job_id}'
            }), 404

        return jsonify(job), 200

    @app.route('/api/jobs/<job_id>', methods=['DELETE'])
    @require_api_key
    def cancel_job(job_id: str):
        """
        Cancel pending job

        Path Parameters:
            job_id: Job ID (UUID)

        Query Parameters:
            key (required): API key

        Returns:
            200: Job cancelled
            400: Job cannot be cancelled (not pending)
            404: Job not found
            401: Unauthorized
        """
        job = queue.get_job(job_id)

        if not job:
            return jsonify({
                'error': 'Not Found',
                'message': f'Job not found: {job_id}'
            }), 404

        if job['status'] != JobStatus.PENDING:
            return jsonify({
                'error': 'Bad Request',
                'message': f"Cannot cancel job in status: {job['status']}"
            }), 400

        success = queue.cancel_job(job_id)

        if success:
            logger.info(f"Job cancelled: {job_id}")
            return jsonify({
                'job_id': job_id,
                'status': JobStatus.CANCELLED,
                'message': 'Job cancelled successfully'
            }), 200
        else:
            return jsonify({
                'error': 'Internal Server Error',
                'message': 'Failed to cancel job'
            }), 500

    @app.route('/api/health', methods=['GET'])
    def health():
        """
        Health check endpoint (no authentication required)

        Returns:
            200: Service healthy
            503: Service unhealthy
        """
        errors = []

        # Check queue backend
        if not queue.health_check():
            errors.append("Queue backend not accessible")

        # Check worker
        if not worker.is_alive():
            errors.append("Worker thread not running")

        # Check for stuck processing jobs
        processing_count = queue.count_jobs(JobStatus.PROCESSING)
        if processing_count > 5:  # Arbitrary threshold
            errors.append(f"Too many processing jobs: {processing_count}")

        if errors:
            return jsonify({
                'status': 'unhealthy',
                'errors': errors,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }), 503

        # Healthy response
        worker_status = worker.get_status()

        return jsonify({
            'status': 'healthy',
            'version': __version__,
            'queue': {
                'backend': queue.__class__.__name__,
                'pending_jobs': queue.get_queue_depth(),
                'processing_jobs': queue.count_jobs(JobStatus.PROCESSING)
            },
            'worker': {
                'status': 'running' if worker_status['running'] else 'stopped',
                'last_job_completed': worker_status['last_job_completed']
            },
            'timestamp': datetime.now(timezone.utc).isoformat()
        }), 200

    @app.route('/api/stats', methods=['GET'])
    @require_api_key
    def stats():
        """
        Get server statistics

        Query Parameters:
            key (required): API key

        Returns:
            200: Statistics
            401: Unauthorized
        """
        try:
            queue_stats = queue.get_stats()
            worker_status = worker.get_status()

            return jsonify({
                'jobs': {
                    'total': queue_stats['total_jobs'],
                    'pending': queue_stats['pending'],
                    'processing': queue_stats['processing'],
                    'completed': queue_stats['completed'],
                    'failed': queue_stats['failed'],
                    'cancelled': queue_stats['cancelled']
                },
                'performance': {
                    'average_execution_time': f"{queue_stats['average_execution_time']}s" if queue_stats['average_execution_time'] else None,
                },
                'queue': {
                    'backend': queue.__class__.__name__,
                    'depth': queue.get_queue_depth()
                },
                'worker': {
                    'status': 'running' if worker_status['running'] else 'stopped',
                    'last_job_completed': worker_status['last_job_completed']
                },
                'timestamp': datetime.now(timezone.utc).isoformat()
            }), 200

        except Exception as e:
            logger.error(f"Error getting stats: {e}", exc_info=True)
            return jsonify({
                'error': 'Internal Server Error',
                'message': str(e)
            }), 500

    @app.route('/api/version', methods=['GET'])
    def version():
        """
        Get version information (no authentication required)

        Returns:
            200: Version info
        """
        return jsonify({
            'version': __version__,
            'api_version': '1.0',
            'python_version': os.sys.version.split()[0]
        }), 200

    @app.errorhandler(413)
    def too_large(error):
        """Handle bodies over MAX_CONTENT_LENGTH"""
        return jsonify({
            'error': 'Payload Too Large',
            'message': f'Request body exceeds {MAX_REQUEST_BYTES // (1024 * 1024)} MB'
        }), 413

    @app.errorhandler(404)
    def not_found(error):
        """Handle 404 errors"""
        return jsonify({
            'error': 'Not Found',
            'message': 'Endpoint not found'
        }), 404

    @app.errorhandler(500)
    def internal_error(error):
        """Handle 500 errors"""
        logger.error(f"Internal server error: {error}", exc_info=True)
        return jsonify({
            'error': 'Internal Server Error',
            'message': 'An unexpected error occurred'
        }), 500


def _editor_state() -> Dict[str, Any]:
    """Whether rules.yml can be saved from the editor right now, and why not"""
    if rules_store is None:
        return {'editable': False, 'readonly_reason': 'The server was started without a config directory'}
    if not editor_enabled:
        return {'editable': False, 'readonly_reason': 'Saving is turned off (server.editor_enabled: false)'}
    ok, reason = rules_store.writable()
    return {'editable': ok, 'readonly_reason': reason}


def _strip_etag(value: Optional[str]) -> Optional[str]:
    """'W/"abc"' / '"abc"' / 'abc' -> 'abc'"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith('W/'):
        value = value[2:]
    return value.strip('"') or None


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def register_editor_routes(app: Flask):
    """
    JSON API behind the rules editor (/rules/edit)

    rules.yml is edited as text -- the browser sends the whole file and the
    server writes exactly those bytes, so comments, ordering and formatting
    all survive. Every write carries the ETag of the version it was edited
    from (If-Match) and is refused with 412 if the file has changed since;
    every write is validated first (rules_validation) and refused with 422
    if anything would fail or misfire; every write keeps a copy of what it
    replaced (config_store). The rules engine hot-reloads rules.yml, so a
    save applies to the next job without a restart.

    config.yml is exposed read-only, with credentials masked -- see
    config_store.redacted_config_view() for why.
    """

    def _unavailable():
        return jsonify({
            'error': 'Service Unavailable',
            'message': 'The server was started without a config directory'
        }), 503

    def _bad_content():
        return jsonify({'error': 'Bad Request', 'message': "Body must be JSON with a 'content' string"}), 400

    @app.route('/api/rules/source', methods=['GET'])
    @require_api_key_or_session
    def rules_source():
        """
        rules.yml as text, with the ETag a save must send back

        Returns:
            200: {content, etag, modified_at, size, path, editable, readonly_reason}
        """
        if rules_store is None:
            return _unavailable()
        snapshot = rules_store.read()
        response = jsonify({**snapshot.to_dict(), 'path': str(rules_store.path), **_editor_state()})
        response.headers['ETag'] = f'"{snapshot.etag}"'
        response.headers['Cache-Control'] = 'no-store'
        return response, 200

    @app.route('/api/rules/validate', methods=['POST'])
    @require_api_key_or_session
    def rules_validate():
        """
        Validate a candidate rules.yml without saving it

        Body: {"content": "<yaml>"}

        Returns:
            200: {valid, issues: [{severity, message, path, line, column,
                 end_line, end_column}], counts, summary}
            400: No content
        """
        content = _json_body().get('content')
        if not isinstance(content, str):
            return _bad_content()
        return jsonify(validate_rules_text(content)), 200

    @app.route('/api/rules/source', methods=['PUT'])
    @require_api_key_or_session
    def rules_save():
        """
        Save rules.yml

        Body: {"content": "<yaml>"}, plus an If-Match header (or "etag" in
        the body) holding the ETag of the version the edit started from.

        Returns:
            200: Saved, or unchanged -- {saved, etag, modified_at, size, validation}
            400: No content
            403: Saving is turned off, or the file isn't writable
            412: The file changed since that ETag -- {current: {content, etag, ...}}
            422: Validation found errors -- {validation}
            428: No If-Match/etag given
        """
        if rules_store is None:
            return _unavailable()

        body = _json_body()
        content = body.get('content')
        if not isinstance(content, str):
            return _bad_content()

        state = _editor_state()
        if not state['editable']:
            return jsonify({'error': 'Forbidden', 'message': state['readonly_reason']}), 403

        if_match = _strip_etag(request.headers.get('If-Match')) or _strip_etag(body.get('etag'))
        if not if_match:
            return jsonify({
                'error': 'Precondition Required',
                'message': 'Send the ETag from GET /api/rules/source as If-Match, so a save '
                           'cannot overwrite changes made since'
            }), 428

        validation = validate_rules_text(content)
        if not validation['valid']:
            return jsonify({
                'error': 'Unprocessable Entity',
                'message': f"rules.yml has {validation['counts']['error']} error(s) -- not saved",
                'validation': validation,
            }), 422

        try:
            snapshot, changed = rules_store.write(content, if_match)
        except FileNotWritableError as e:
            return jsonify({'error': 'Forbidden', 'message': str(e)}), 403
        except FileConflictError as e:
            return jsonify({
                'error': 'Precondition Failed',
                'message': 'rules.yml changed since you opened it',
                'current': e.current.to_dict(),
            }), 412

        if changed:
            # This process serves the new rules immediately; every other
            # Gunicorn worker notices the changed file on its next
            # get_rules() call
            try:
                dashboard_config.reload_rules(force=True)
            except Exception as e:
                logger.warning(f"Saved rules.yml but reloading it failed: {e}")

        response = jsonify({
            'saved': changed,
            'etag': snapshot.etag,
            'modified_at': snapshot.modified_at,
            'size': snapshot.size,
            'validation': validation,
        })
        response.headers['ETag'] = f'"{snapshot.etag}"'
        return response, 200

    @app.route('/api/rules/history', methods=['GET'])
    @require_api_key_or_session
    def rules_history():
        """
        Previous versions of rules.yml kept by editor saves, newest first

        Returns:
            200: {versions: [{id, saved_at, size}]}
        """
        if rules_store is None:
            return _unavailable()
        return jsonify({'versions': rules_store.history()}), 200

    @app.route('/api/rules/history/<version_id>', methods=['GET'])
    @require_api_key_or_session
    def rules_history_version(version_id: str):
        """
        One previous version's content -- restore it by saving it

        Returns:
            200: {id, content}
            404: No such version
        """
        if rules_store is None:
            return _unavailable()
        content = rules_store.read_version(version_id)
        if content is None:
            return jsonify({'error': 'Not Found', 'message': f'No saved version {version_id}'}), 404
        return jsonify({'id': version_id, 'content': content}), 200

    @app.route('/api/rules/catalog', methods=['GET'])
    @require_api_key_or_session
    def rules_catalog():
        """
        Fields, operators and action types the engine supports, for the
        editor's autocomplete, plus the contexts config.yml's schedule fires
        """
        catalog = schema.catalog()
        schedule = dashboard_config.schedule if dashboard_config is not None else []
        catalog['schedule_contexts'] = sorted({str(entry.get('context')) for entry in schedule})
        return jsonify(catalog), 200

    @app.route('/api/config/source', methods=['GET'])
    @require_api_key_or_session
    def config_source():
        """
        config.yml as text, read-only, with credentials masked

        Returns:
            200: {content, redacted, issues, path}
        """
        if dashboard_config is None:
            return _unavailable()
        view = redacted_config_view(dashboard_config.config_file.read_text('utf-8'))
        response = jsonify({**view, 'path': str(dashboard_config.config_file)})
        response.headers['Cache-Control'] = 'no-store'
        return response, 200


_TRACEBACK_EXCEPTION_LINE = re.compile(r'^[A-Za-z_][\w.]*(?:Error|Exception|Warning)\b.*:', re.MULTILINE)


def _summarize_error(error: str) -> str:
    """
    Extract the final exception's summary line(s) from a full Python
    traceback (as stored by worker.py via traceback.format_exc())

    A traceback can be a chain of several exceptions ("During handling
    of the above exception..."), each contributing its own zero-indent
    "SomeError: message" line -- stack frame lines are always indented,
    so matching only zero-indent lines and taking the last match finds
    the final, most relevant exception, skipping past every earlier
    cause and every "File ..." frame line. Returns everything from that
    point to the end of the string (covering multi-line messages, like
    this project's own bulleted ConnectionError format).

    Falls back to the input's last non-empty line if it doesn't look
    like a Python traceback at all.
    """
    if not error:
        return error

    matches = list(_TRACEBACK_EXCEPTION_LINE.finditer(error))
    if not matches:
        lines = error.strip().splitlines()
        return lines[-1] if lines else error

    return error[matches[-1].start():].strip()


def register_dashboard_routes(app: Flask):
    """
    Register read-only web dashboard routes

    Server-rendered via Flask + Jinja2 (templates in src/qbt_rules/templates/),
    reusing the same queue/worker/config data the JSON API already exposes --
    no new query logic. Lives at the site root (/, /jobs, /rules) rather
    than under a /dashboard/ prefix, distinct from the /api/* and /metrics
    namespaces. Auth is via require_api_key_dashboard (same ?key= query
    param the JSON API supports, but redirects a human to /login instead
    of a JSON 401 on failure); every internal link carries the key forward
    so navigating between pages stays authenticated.
    """

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """
        API key entry form for browser use

        Not behind require_api_key_dashboard itself -- that would make it
        impossible to ever reach. On success, establishes the session
        require_api_key_dashboard checks on every subsequent request, so
        the key doesn't need to be repeated in every URL from here on.

        `next` (where to go after a successful submission) is always run
        through _sanitize_next() before being used as a redirect target --
        both when rendering it into the form's hidden field and again on
        the POST that reads it back -- so a crafted next can't send anyone
        off-site.
        """
        if request.method == 'POST':
            key = request.form.get('key', '')
            next_url = _sanitize_next(request.form.get('next'))

            if key and secrets.compare_digest(key, api_key_config):
                session.permanent = True
                session['authenticated'] = True
                return redirect(next_url)

            return redirect(url_for('login', next=next_url, error=1))

        next_url = _sanitize_next(request.args.get('next'))

        return render_template(
            'login.html',
            next=next_url,
            error=bool(request.args.get('error')),
        )

    @app.route('/', methods=['GET'])
    @require_api_key_dashboard
    def dashboard_overview():
        """Dashboard home: job counts by status, recent activity, rules summary"""
        queue_stats = queue.get_stats()
        recent_jobs = queue.list_jobs(limit=5)

        rules = dashboard_config.get_rules() if dashboard_config is not None else None
        rules_summary = {
            'total': len(rules),
            'enabled': sum(1 for r in rules if r.get('enabled', True)),
            'rules': rules,
        } if rules is not None else None

        return render_template(
            'dashboard.html',
            active='overview',
            queue_stats=queue_stats,
            recent_jobs=recent_jobs,
            rules_summary=rules_summary,
        )

    @app.route('/jobs', methods=['GET'])
    @require_api_key_dashboard
    def dashboard_jobs():
        """Paginated job list, optionally filtered by status"""
        status = request.args.get('status')
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))

        jobs = queue.list_jobs(status=status, limit=limit, offset=offset)
        total = queue.count_jobs(status=status)

        return render_template(
            'jobs.html',
            active='jobs',
            jobs=jobs,
            total=total,
            limit=limit,
            offset=offset,
            status=status,
        )

    @app.route('/jobs/<job_id>', methods=['GET'])
    @require_api_key_dashboard
    def dashboard_job_detail(job_id: str):
        """Single job's full detail, including result/error if present"""
        job = queue.get_job(job_id)
        error_summary = _summarize_error(job['error']) if job and job.get('error') else None

        return render_template(
            'job_detail.html',
            active='jobs',
            job=job,
            job_id=job_id,
            error_summary=error_summary,
        ), (200 if job else 404)

    @app.route('/rules', methods=['GET'])
    @require_api_key_dashboard
    def dashboard_rules():
        """Read-only rules.yml view, sourced from the same hot-reload-aware
        Config.get_rules() the rules engine itself uses"""
        rules = dashboard_config.get_rules() if dashboard_config is not None else None

        # Pair each rule with its own resolved-YAML text for the template's
        # per-rule "Show resolved YAML" panel, without mutating the rule
        # dicts get_rules() returns (those may be a direct reference to the
        # engine's cached resolved rules, not a defensive copy).
        rules_with_yaml = [
            (rule, yaml.dump(rule, default_flow_style=False, sort_keys=False))
            for rule in rules
        ] if rules else []

        return render_template(
            'rules.html',
            active='rules',
            rules=rules,
            rules_with_yaml=rules_with_yaml,
            config_available=dashboard_config is not None,
        )


    @app.route('/rules/edit', methods=['GET'])
    @require_api_key_dashboard
    def dashboard_rules_edit():
        """rules.yml editor -- the page itself is static; everything it
        shows comes from the /api/rules/* and /api/config/source endpoints"""
        return render_template(
            'rules_edit.html',
            active='rules',
            config_available=dashboard_config is not None,
        )


def register_metrics_routes(app: Flask):
    """
    Register the Prometheus /metrics endpoint

    Only called from create_app() when metrics are enabled -- by that
    point metrics.init(enabled=True) has already run (in cli.py, before
    Gunicorn forks), so prometheus_client is guaranteed importable here.
    """

    @app.route('/metrics', methods=['GET'])
    @require_api_key
    def metrics_endpoint():
        """
        Prometheus scrape endpoint

        Combines multiprocess-aggregated event counters/histograms (HTTP
        requests, actions executed, job duration, scheduler fires) with
        on-demand state gauges computed fresh from the same data /api/stats
        and /api/health already expose (queue depth/job counts, worker
        status, schedule entry count) -- see metrics.py's module docstring
        for why these are split into two different collection strategies.
        """
        worker_status = worker.get_status()
        schedule_entry_count = len(dashboard_config.schedule) if dashboard_config is not None else 0

        output = metrics.generate_metrics_output(
            queue_backend=queue.__class__.__name__,
            queue_depth=queue.get_queue_depth(),
            job_counts=queue.get_stats(),
            worker_running=worker_status['running'],
            worker_last_job_completed=worker_status['last_job_completed'],
            schedule_entry_count=schedule_entry_count,
        )
        return Response(output, mimetype=metrics.content_type())


def _register_http_metrics_hooks(app: Flask):
    """
    Register before_request/after_request hooks recording HTTP request
    count/duration metrics for every route except /metrics itself (avoids a
    self-referential scrape-of-scrape metric, standard practice)
    """

    @app.before_request
    def _metrics_start_timer():
        g._metrics_start_time = time.time()

    @app.after_request
    def _metrics_record_request(response):
        if request.endpoint != 'metrics_endpoint' and hasattr(g, '_metrics_start_time'):
            duration = time.time() - g._metrics_start_time
            metrics.record_http_request(
                method=request.method,
                endpoint=request.endpoint or 'unknown',
                status=response.status_code,
                duration=duration,
            )
        return response


def run_server(
    app: Flask,
    host: str = '0.0.0.0',
    port: int = 5000,
    workers: int = 1,
    log_http_access: bool = False,
    metrics_enabled: bool = False
):
    """
    Run Flask app with Gunicorn in production mode

    Args:
        app: Flask application
        host: Bind address
        port: Bind port
        workers: Number of Gunicorn workers
        log_http_access: Enable HTTP access logging (default: False to suppress health checks)
        metrics_enabled: Whether to register the child_exit Gunicorn hook
            (Prometheus multiprocess bookkeeping when a worker exits --
            see child_exit()'s own docstring for what it actually does
            today) -- only meaningful when metrics are enabled
    """
    from gunicorn.app.base import BaseApplication
    from gunicorn.glogging import Logger

    class FilteredLogger(Logger):
        """Custom Gunicorn logger that filters out health check and dashboard requests"""

        def access(self, resp, req, environ, request_time):
            """Override access log to filter /api/health and dashboard requests"""
            # Only filter if log_http_access is False
            if not log_http_access:
                # Skip logging for health check endpoint
                if environ.get('PATH_INFO') == '/api/health':
                    return
                # Skip logging for dashboard pages -- the dashboard's only
                # auth mechanism is a ?key= query param, and a browsing
                # session hits many more URLs than a scripted API client
                # typically would, so logging every one repeats the key
                # in cleartext far more than the JSON API does. Matched by
                # an explicit allowlist (dashboard routes live at the site
                # root, not under a shared /dashboard/ prefix) rather than
                # "not /api/* and not /metrics", so unrelated 404 probes
                # (bots, scanners) still get logged as before.
                path = environ.get('PATH_INFO', '')
                if path == '/' or path.startswith(('/jobs', '/rules', '/login')):
                    return
                # Skip logging for the Prometheus scrape endpoint -- polled
                # every 15-30s by a scraper, same volume rationale as health
                if environ.get('PATH_INFO') == '/metrics':
                    return

            # Log all other requests (or all requests if log_http_access is True)
            super().access(resp, req, environ, request_time)

    class StandaloneApplication(BaseApplication):
        def __init__(self, app, options=None):
            self.application = app
            self.options = options or {}
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                if key in self.cfg.settings and value is not None:
                    self.cfg.set(key, value)

        def load(self):
            return self.application

    def post_fork(server, worker_process):
        """
        Gunicorn post-fork hook - restart worker thread in forked process

        When Gunicorn forks, threads don't survive the fork. We need to
        restart the worker thread in each forked worker process.
        """
        logger.info(f"Gunicorn worker {worker_process.pid} forked - restarting worker thread")

        # Import here to access the global worker instance
        from qbt_rules.server import worker as worker_instance

        # Stop any existing thread (should be dead anyway after fork)
        if worker_instance.running:
            worker_instance.running = False

        # Restart the worker thread in this process
        worker_instance.start()
        logger.info(f"Worker thread restarted in Gunicorn worker {worker_process.pid}")

    def child_exit(server, worker_process):
        """
        Gunicorn child_exit hook - Prometheus multiprocess bookkeeping
        when a worker exits

        Verified empirically (real 2-worker server, one worker killed
        mid-run): a dead worker's Counter/Histogram .db files in
        PROMETHEUS_MULTIPROC_DIR correctly keep contributing to aggregated
        /metrics totals after it's gone -- and that's the *correct*
        behavior, not a leak to clean up. Those values are cumulative
        historical fact ("this many requests really happened") and stay
        valid regardless of whether the process that recorded them still
        exists; MultiProcessCollector already sums every .db file in the
        directory unconditionally. mark_process_dead() only ever removes
        gauge_{live-mode}_{pid}.db files, for prometheus_client's
        multiprocess "live" Gauge modes (livesum/liveall/livemax/livemin)
        specifically -- this module doesn't use those today (the on-demand
        state Collector in metrics.py computes gauges fresh on every
        scrape instead, deliberately sidestepping the need for them), so
        this call is currently a no-op. Kept as forward-compatible hygiene
        in case a future metric genuinely needs a live-mode Gauge.
        """
        from prometheus_client import multiprocess
        multiprocess.mark_process_dead(worker_process.pid)

    options = {
        'bind': f'{host}:{port}',
        'workers': workers,
        'worker_class': 'sync',
        'timeout': 120,
        'accesslog': '-',  # Log to stdout
        'errorlog': '-',   # Log to stderr
        'loglevel': 'warning',
        'logger_class': FilteredLogger,  # Use custom logger to filter health checks
        'preload_app': True,  # Load app before forking workers
        'post_fork': post_fork,  # Restart worker thread after fork
    }
    if metrics_enabled:
        options['child_exit'] = child_exit

    logger.info(f"Starting Gunicorn server on {host}:{port} with {workers} worker(s)")

    app_instance = StandaloneApplication(app, options)
    app_instance.run()
