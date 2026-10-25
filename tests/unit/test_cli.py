"""
Tests for qbt_rules.cli module (v0.4.0 client-server architecture)
"""

import pytest
import os
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
from argparse import Namespace
import json

from qbt_rules.cli import (
    resolve_section_config,
    get_logging_config,
    get_qbittorrent_config,
    get_server_config,
    get_client_config,
    PLACEHOLDER_API_KEY,
    get_queue_config,
    get_notifications_config,
    run_server_mode,
    run_client_mode,
    wait_for_job,
    list_jobs_command,
    job_status_command,
    cancel_job_command,
    stats_command
)


class TestResolveSectionConfig:
    """Test resolve_section_config() -- the generic per-section resolver
    the get_*_config accessors above are all built on."""

    def test_uses_field_defaults(self):
        """No args/env/config -- each field falls back to its own default."""
        args = Namespace()
        config_obj = Mock(config={})

        result = resolve_section_config(args, config_obj, 'server', {
            'host': '0.0.0.0', 'port': 5000,
        })

        assert result == {'host': '0.0.0.0', 'port': 5000}

    def test_cli_arg_name_auto_derives_from_section_and_field(self):
        """section='server', field='port' -> getattr(args, 'server_port')."""
        args = Namespace(server_port=9000)
        config_obj = Mock(config={})

        result = resolve_section_config(args, config_obj, 'server', {'port': 5000})

        assert result['port'] == 9000

    def test_env_var_name_auto_derives_from_convention(self):
        """section='widgets', field='color' -> QBT_RULES_WIDGETS_COLOR, with
        no ENV_VAR_MAP entry needed."""
        args = Namespace()
        config_obj = Mock(config={})

        with patch.dict(os.environ, {'QBT_RULES_WIDGETS_COLOR': 'blue'}):
            result = resolve_section_config(args, config_obj, 'widgets', {'color': 'red'})

        assert result['color'] == 'blue'

    def test_config_file_value_used_when_no_cli_or_env(self):
        args = Namespace()
        config_obj = Mock(config={'widgets': {'color': 'green'}})

        result = resolve_section_config(args, config_obj, 'widgets', {'color': 'red'})

        assert result['color'] == 'green'

    def test_parser_applied_to_resolved_value(self):
        """parsers={'port': int} coerces a string CLI value to an int."""
        args = Namespace(server_port='8080')
        config_obj = Mock(config={})

        result = resolve_section_config(
            args, config_obj, 'server', {'port': 5000}, parsers={'port': int}
        )

        assert result['port'] == 8080
        assert isinstance(result['port'], int)

    def test_parser_not_applied_when_value_is_none(self):
        """A None default (e.g. an optional field) is never handed to the parser."""
        args = Namespace()
        config_obj = Mock(config={})

        result = resolve_section_config(
            args, config_obj, 'server', {'api_key': None}, parsers={'api_key': int}
        )

        assert result['api_key'] is None

    def test_dotted_section_name_for_nested_config(self):
        """section='integrations.sonarr' -> config key 'integrations.sonarr.url'
        and env var QBT_RULES_INTEGRATIONS_SONARR_URL -- the shape a future
        nested section (e.g. Sonarr/Radarr) would use."""
        args = Namespace()
        config_obj = Mock(config={'integrations': {'sonarr': {'url': 'http://sonarr.local'}}})

        result = resolve_section_config(args, config_obj, 'integrations.sonarr', {
            'url': None, 'api_key': None,
        })

        assert result == {'url': 'http://sonarr.local', 'api_key': None}

    def test_dotted_section_env_var_derivation(self):
        args = Namespace()
        config_obj = Mock(config={})

        with patch.dict(os.environ, {'QBT_RULES_INTEGRATIONS_SONARR_API_KEY': 'secret'}):
            result = resolve_section_config(args, config_obj, 'integrations.sonarr', {
                'api_key': None,
            })

        assert result['api_key'] == 'secret'

    def test_env_var_map_override_takes_precedence_over_convention(self):
        """A config_key present in ENV_VAR_MAP wins over the mechanical
        QBT_RULES_<SECTION>_<FIELD> guess -- this is how the one remaining
        genuine naming exception (engine.dry_run, no section prefix at
        all) stays working."""
        args = Namespace()
        config_obj = Mock(config={})

        # engine.dry_run is mapped to the bare QBT_RULES_DRY_RUN in
        # ENV_VAR_MAP, not the mechanically-derived QBT_RULES_ENGINE_DRY_RUN
        with patch.dict(os.environ, {'QBT_RULES_DRY_RUN': 'true'}):
            result = resolve_section_config(args, config_obj, 'engine', {'dry_run': False})

        assert result['dry_run'] == 'true'

    def test_cli_value_takes_precedence_over_env(self):
        args = Namespace(server_port=1111)
        config_obj = Mock(config={})

        with patch.dict(os.environ, {'QBT_RULES_SERVER_PORT': '2222'}):
            result = resolve_section_config(args, config_obj, 'server', {'port': 5000})

        assert result['port'] == 1111


class TestGetLoggingConfig:
    """Test get_logging_config() function

    Doesn't use resolve_section_config() directly -- --log-level/--trace
    are the established CLI flag names, not --logging-level/
    --logging-trace-mode, so each field is hand-resolved. Still gets full
    CLI > _FILE env var > env var > config.yml > default resolution via
    resolve_config() for every field, matching every other section
    (previously, this all went through bare, unprefixed env vars --
    LOG_LEVEL/LOG_FILE/TRACE_MODE -- with no _FILE support at all, despite
    config.default.yml documenting QBT_RULES_LOG_* with _FILE support)."""

    def test_returns_default_config(self, tmp_path):
        """Should return defaults when nothing is set"""
        args = Namespace()
        config_obj = Mock(config={}, config_dir=tmp_path)

        config = get_logging_config(args, config_obj)

        assert config['level'] == 'INFO'
        assert config['file'] == tmp_path / 'logs' / 'qbittorrent.log'
        assert config['trace_mode'] is False
        assert config['http_access'] is False
        assert config['client_mode'] is True

    def test_client_mode_false_when_serving(self, tmp_path):
        """--serve should mark client_mode False, so setup_logging() keeps
        the full console format for server-mode operational output."""
        args = Namespace(serve=True)
        config_obj = Mock(config={}, config_dir=tmp_path)

        config = get_logging_config(args, config_obj)

        assert config['client_mode'] is False

    def test_client_mode_true_when_not_serving(self, tmp_path):
        """Any invocation with --serve not set is client mode."""
        args = Namespace(serve=False)
        config_obj = Mock(config={}, config_dir=tmp_path)

        config = get_logging_config(args, config_obj)

        assert config['client_mode'] is True

    def test_uses_config_file_values(self, tmp_path):
        """Should use config.yml values"""
        args = Namespace()
        config_obj = Mock(config={
            'logging': {
                'level': 'debug',
                'file': 'custom/path.log',
                'trace_mode': True,
                'http_access': True,
            }
        }, config_dir=tmp_path)

        config = get_logging_config(args, config_obj)

        assert config['level'] == 'DEBUG'
        assert config['file'] == tmp_path / 'custom' / 'path.log'
        assert config['trace_mode'] is True
        assert config['http_access'] is True

    def test_absolute_file_path_used_as_is(self, tmp_path):
        """An absolute logging.file path isn't rebased under config_dir"""
        args = Namespace()
        config_obj = Mock(config={'logging': {'file': '/var/log/qbt-rules.log'}}, config_dir=tmp_path)

        config = get_logging_config(args, config_obj)

        assert config['file'] == Path('/var/log/qbt-rules.log')

    def test_uses_cli_args(self, tmp_path):
        """--log-level and --trace (args.log_level / args.trace) should win"""
        args = Namespace(log_level='WARNING', trace=True)
        config_obj = Mock(config={}, config_dir=tmp_path)

        config = get_logging_config(args, config_obj)

        assert config['level'] == 'WARNING'
        assert config['trace_mode'] is True

    def test_trace_flag_false_does_not_override_config_yml(self, tmp_path):
        """--trace not passed (False) must not clobber a true value from
        config.yml -- only an explicit --trace should force True via CLI"""
        args = Namespace(trace=False)
        config_obj = Mock(config={'logging': {'trace_mode': True}}, config_dir=tmp_path)

        config = get_logging_config(args, config_obj)

        assert config['trace_mode'] is True

    def test_uses_env_vars(self, tmp_path):
        """The bug this replaces: QBT_RULES_LOGGING_* env vars used to have
        zero effect -- only bare, unprefixed LOG_LEVEL/LOG_FILE/TRACE_MODE
        worked. Confirm the documented, prefixed names now actually work."""
        args = Namespace()
        config_obj = Mock(config={}, config_dir=tmp_path)

        env = {
            'QBT_RULES_LOGGING_LEVEL': 'error',
            'QBT_RULES_LOGGING_FILE': '/env/qbt-rules.log',
            'QBT_RULES_LOGGING_TRACE_MODE': 'true',
            'QBT_RULES_LOGGING_HTTP_ACCESS': 'true',
        }
        with patch.dict(os.environ, env):
            config = get_logging_config(args, config_obj)

        assert config['level'] == 'ERROR'
        assert config['file'] == Path('/env/qbt-rules.log')
        assert config['trace_mode'] is True
        assert config['http_access'] is True

    def test_uses_file_env_var(self, tmp_path):
        """QBT_RULES_LOGGING_LEVEL_FILE -- previously unsupported at all
        for logging config, unlike every other section."""
        secret_file = tmp_path / "log_level"
        secret_file.write_text("debug\n")

        args = Namespace()
        config_obj = Mock(config={}, config_dir=tmp_path)

        with patch.dict(os.environ, {'QBT_RULES_LOGGING_LEVEL_FILE': str(secret_file)}):
            config = get_logging_config(args, config_obj)

        assert config['level'] == 'DEBUG'

    def test_bare_unprefixed_env_vars_no_longer_work(self, tmp_path):
        """The old (undocumented, no _FILE support) bare LOG_LEVEL/LOG_FILE/
        TRACE_MODE env vars must no longer have any effect."""
        args = Namespace()
        config_obj = Mock(config={}, config_dir=tmp_path)

        env = {
            'LOG_LEVEL': 'DEBUG',
            'LOG_FILE': '/should/be/ignored.log',
            'TRACE_MODE': 'true',
        }
        with patch.dict(os.environ, env):
            config = get_logging_config(args, config_obj)

        assert config['level'] == 'INFO'          # default, NOT 'DEBUG'
        assert config['trace_mode'] is False       # default, NOT True
        assert config['file'] == tmp_path / 'logs' / 'qbittorrent.log'  # default, NOT the ignored path


class TestGetQbittorrentConfig:
    """Test get_qbittorrent_config() function

    This replaces Config.get_qbittorrent_config(), which read config.yml
    directly and never supported CLI args, env vars, or _FILE secrets at
    all for host/username/password -- silently ignoring every documented
    QBT_RULES_QBITTORRENT_* environment variable. These tests assert real
    resolved *values*, not just key presence, specifically because the
    bug this replaces was masked by tests that only checked keys existed."""

    def test_returns_default_config(self):
        """Should return default configuration when nothing is set"""
        args = Namespace()
        config_obj = Mock(config={})

        config = get_qbittorrent_config(args, config_obj)

        assert config['host'] == 'http://localhost:8080'
        assert config['username'] == 'admin'
        assert config['password'] == ''

    def test_uses_config_file_values(self):
        """Should use config.yml values -- this is the path that always
        worked, even before the fix"""
        args = Namespace()
        config_obj = Mock(config={
            'qbittorrent': {
                'host': 'http://qbittorrent:8080',
                'username': 'myuser',
                'password': 'mypass',
            }
        })

        config = get_qbittorrent_config(args, config_obj)

        assert config['host'] == 'http://qbittorrent:8080'
        assert config['username'] == 'myuser'
        assert config['password'] == 'mypass'

    def test_uses_args_values(self):
        """Should use CLI argument values when provided"""
        args = Namespace(
            qbittorrent_host='http://cli-host:8080',
            qbittorrent_username='cliuser',
            qbittorrent_password='clipass',
        )
        config_obj = Mock(config={})

        config = get_qbittorrent_config(args, config_obj)

        assert config['host'] == 'http://cli-host:8080'
        assert config['username'] == 'cliuser'
        assert config['password'] == 'clipass'

    def test_uses_env_vars(self):
        """The bug this replaces: QBT_RULES_QBITTORRENT_* env vars used to
        have zero effect, regardless of key name. Confirm they now
        actually resolve -- host, username, AND password."""
        args = Namespace()
        config_obj = Mock(config={})

        env = {
            'QBT_RULES_QBITTORRENT_HOST': 'http://env-host:8080',
            'QBT_RULES_QBITTORRENT_USERNAME': 'envuser',
            'QBT_RULES_QBITTORRENT_PASSWORD': 'envpass',
        }
        with patch.dict(os.environ, env):
            config = get_qbittorrent_config(args, config_obj)

        assert config['host'] == 'http://env-host:8080'
        assert config['username'] == 'envuser'
        assert config['password'] == 'envpass'

    def test_uses_file_env_var_for_password(self, tmp_path):
        """QBT_RULES_QBITTORRENT_PASSWORD_FILE, the documented
        production-secrets pattern -- also used to have zero effect."""
        secret_file = tmp_path / "qbt_password"
        secret_file.write_text("file-secret-pass\n")

        args = Namespace()
        config_obj = Mock(config={})

        with patch.dict(os.environ, {'QBT_RULES_QBITTORRENT_PASSWORD_FILE': str(secret_file)}):
            config = get_qbittorrent_config(args, config_obj)

        assert config['password'] == 'file-secret-pass'

    def test_legacy_user_pass_keys_no_longer_work(self):
        """The undocumented 'user'/'pass' config.yml key aliases are gone
        -- a config.yml using only those now resolves to the default,
        not the value that was actually set."""
        args = Namespace()
        config_obj = Mock(config={
            'qbittorrent': {
                'user': 'legacyuser',
                'pass': 'legacypass',
            }
        })

        config = get_qbittorrent_config(args, config_obj)

        assert config['username'] == 'admin'  # default, NOT 'legacyuser'
        assert config['password'] == ''       # default, NOT 'legacypass'


class TestGetServerConfig:
    """Test get_server_config() function"""

    def test_returns_default_config(self):
        """Should return default configuration when no args provided"""
        args = Namespace()
        config_obj = Mock(config={})

        config = get_server_config(args, config_obj)

        assert config['host'] == '0.0.0.0'
        assert config['port'] == 5000
        assert config['api_key'] is None
        assert config['workers'] == 1

    def test_uses_args_values(self):
        """Should use CLI argument values when provided"""
        args = Namespace(
            server_host='127.0.0.1',
            server_port='8000',
            server_api_key='test-key',
            server_workers='2'
        )
        config_obj = Mock(config={})

        config = get_server_config(args, config_obj)

        assert config['host'] == '127.0.0.1'
        assert config['port'] == 8000
        assert config['api_key'] == 'test-key'
        assert config['workers'] == 2

    def test_uses_config_file_values(self):
        """Should use config file values when args not provided"""
        args = Namespace()
        config_obj = Mock(config={
            'server': {
                'host': '192.168.1.1',
                'port': 9000,
                'api_key': 'config-key',
                'workers': 4
            }
        })

        config = get_server_config(args, config_obj)

        assert config['host'] == '192.168.1.1'
        assert config['port'] == 9000
        assert config['api_key'] == 'config-key'
        assert config['workers'] == 4


    def test_editor_enabled_defaults_on(self):
        config = get_server_config(Namespace(), Mock(config={}))
        assert config['editor_enabled'] is True

    def test_editor_enabled_from_config_and_env(self, monkeypatch):
        config_obj = Mock(config={'server': {'editor_enabled': False}})
        assert get_server_config(Namespace(), config_obj)['editor_enabled'] is False

        monkeypatch.setenv('QBT_RULES_SERVER_EDITOR_ENABLED', 'true')
        assert get_server_config(Namespace(), config_obj)['editor_enabled'] is True


class TestGetNotificationsConfig:
    """Test get_notifications_config() function"""

    def test_returns_default_config(self):
        """Should return defaults when nothing is set"""
        args = Namespace()
        config_obj = Mock(config={})

        config = get_notifications_config(args, config_obj)

        assert config['webhook_url'] is None
        assert config['service'] == 'generic'

    def test_uses_config_file_values(self):
        """Should use config file values when present"""
        args = Namespace()
        config_obj = Mock(config={
            'notifications': {
                'webhook_url': 'https://example.com/webhook',
                'service': 'discord',
            }
        })

        config = get_notifications_config(args, config_obj)

        assert config['webhook_url'] == 'https://example.com/webhook'
        assert config['service'] == 'discord'


class TestGetClientConfig:
    """Test get_client_config() function"""

    def test_returns_default_config(self):
        """Should return default configuration when no args provided"""
        args = Namespace()
        config_obj = Mock(config={})

        config = get_client_config(args, config_obj)

        assert config['server_url'] == 'http://localhost:5000'
        assert config['api_key'] is None

    def test_uses_args_values(self):
        """Should use CLI argument values when provided"""
        args = Namespace(
            client_server_url='http://server.local:8000',
            client_api_key='client-key'
        )
        config_obj = Mock(config={})

        config = get_client_config(args, config_obj)

        assert config['server_url'] == 'http://server.local:8000'
        assert config['api_key'] == 'client-key'

    def test_falls_back_to_server_key_when_unset(self, monkeypatch):
        """docker exec inside the server's container: only the server key is set"""
        monkeypatch.delenv('QBT_RULES_CLIENT_API_KEY', raising=False)
        monkeypatch.setenv('QBT_RULES_SERVER_API_KEY', 'server-key')

        config = get_client_config(Namespace(), Mock(config={}))

        assert config['api_key'] == 'server-key'

    def test_falls_back_when_client_key_is_the_shipped_placeholder(self, monkeypatch):
        """The copied config.default.yml carries a placeholder client key"""
        monkeypatch.delenv('QBT_RULES_CLIENT_API_KEY', raising=False)
        monkeypatch.setenv('QBT_RULES_SERVER_API_KEY', 'server-key')
        config_obj = Mock(config={'client': {'api_key': PLACEHOLDER_API_KEY},
                                  'server': {'api_key': PLACEHOLDER_API_KEY}})

        config = get_client_config(Namespace(), config_obj)

        assert config['api_key'] == 'server-key'

    def test_explicit_client_key_wins_over_server_key(self, monkeypatch):
        monkeypatch.setenv('QBT_RULES_CLIENT_API_KEY', 'client-key')
        monkeypatch.setenv('QBT_RULES_SERVER_API_KEY', 'server-key')

        config = get_client_config(Namespace(), Mock(config={}))

        assert config['api_key'] == 'client-key'

    def test_placeholder_stays_when_no_server_key_either(self, monkeypatch):
        monkeypatch.delenv('QBT_RULES_CLIENT_API_KEY', raising=False)
        monkeypatch.delenv('QBT_RULES_SERVER_API_KEY', raising=False)
        config_obj = Mock(config={'client': {'api_key': PLACEHOLDER_API_KEY}})

        config = get_client_config(Namespace(), config_obj)

        assert config['api_key'] == PLACEHOLDER_API_KEY

    def test_shipped_default_config_has_the_placeholder(self):
        """PLACEHOLDER_API_KEY must track config.default.yml"""
        from pathlib import Path
        import yaml
        default = yaml.safe_load((Path(__file__).parents[2] / 'config' / 'config.default.yml').read_text())
        assert default['client']['api_key'] == PLACEHOLDER_API_KEY


class TestGetQueueConfig:
    """Test get_queue_config() function"""

    def test_returns_default_config(self):
        """Should return default configuration when no args provided"""
        args = Namespace()
        config_obj = Mock(config={})

        config = get_queue_config(args, config_obj)

        assert config['backend'] == 'sqlite'
        assert config['sqlite_path'] == '/config/qbt-rules.db'
        assert config['redis_url'] == 'redis://localhost:6379/0'

    def test_uses_args_values(self):
        """Should use CLI argument values when provided"""
        args = Namespace(
            queue_backend='redis',
            queue_sqlite_path='/tmp/test.db',
            queue_redis_url='redis://redis:6379/1'
        )
        config_obj = Mock(config={})

        config = get_queue_config(args, config_obj)

        assert config['backend'] == 'redis'
        assert config['sqlite_path'] == '/tmp/test.db'
        assert config['redis_url'] == 'redis://redis:6379/1'


class TestRunServerMode:
    """Test run_server_mode() function"""

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.server.run_server')
    @patch('qbt_rules.server.create_app')
    @patch('qbt_rules.scheduler.Scheduler')
    @patch('qbt_rules.worker.Worker')
    @patch('qbt_rules.queue_manager.create_queue')
    @patch('qbt_rules.cli.QBittorrentAPI')
    def test_creates_and_runs_server(self, mock_api_class, mock_create_queue,
                                     mock_worker_class, mock_scheduler_class,
                                     mock_create_app, mock_run_server, mock_logger):
        """Should create Flask app and run server"""
        # Setup mocks
        mock_queue = Mock()
        mock_queue.__class__.__name__ = 'SQLiteQueue'
        mock_create_queue.return_value = mock_queue

        mock_api = Mock()
        mock_api_class.return_value = mock_api

        mock_worker = Mock()
        mock_worker_class.return_value = mock_worker

        mock_scheduler = Mock()
        mock_scheduler_class.return_value = mock_scheduler

        mock_app = Mock()
        mock_create_app.return_value = mock_app

        args = Namespace(
            serve=True,
            server_host='0.0.0.0',
            server_port=5000,
            server_api_key='test-key',
            server_workers=1,
            queue_backend='sqlite',
            queue_sqlite_path='/tmp/test.db',
            queue_redis_url=None
        )
        config_obj = Mock(config={
            'qbittorrent': {
                'host': 'http://localhost:8080',
                'username': 'admin',
                'password': 'password',
            }
        }, schedule=[], config_dir=Path('/config'))

        run_server_mode(args, config_obj, {'http_access': False})

        # Verify queue creation
        mock_create_queue.assert_called_once_with(
            backend='sqlite',
            db_path='/tmp/test.db',
            redis_url='redis://localhost:6379/0'  # Default value
        )

        # Verify API initialization
        mock_api_class.assert_called_once_with(
            host='http://localhost:8080',
            username='admin',
            password='password',
            connect_now=False
        )

        # Verify worker initialization
        mock_worker_class.assert_called_once_with(
            queue=mock_queue,
            api=mock_api,
            config=config_obj,
            notifications_config={'webhook_url': None, 'service': 'generic'},
            integrations_config={
                'sonarr': {'url': None, 'api_key': None},
                'radarr': {'url': None, 'api_key': None},
                'trackie': {'url': None, 'max_age': None, 'timeout': None},
            }
        )
        mock_worker.start.assert_called_once()

        # Verify scheduler initialization
        mock_scheduler_class.assert_called_once_with(
            queue=mock_queue,
            entries=[]
        )
        mock_scheduler.start.assert_called_once()

        # Verify Flask app creation
        mock_create_app.assert_called_once_with(
            queue_manager=mock_queue,
            worker_instance=mock_worker,
            api_key='test-key',
            config=config_obj,
            metrics_config={'enabled': False, 'multiproc_dir': '/tmp/qbt-rules-metrics'},
            editor=True
        )

        # Verify server run
        mock_run_server.assert_called_once_with(
            app=mock_app,
            host='0.0.0.0',
            port=5000,
            workers=1,
            log_http_access=False,
            metrics_enabled=False
        )

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    def test_exits_without_api_key(self, mock_exit, mock_logger):
        """Should exit if server API key not provided"""
        # Make sys.exit raise SystemExit to stop execution
        mock_exit.side_effect = SystemExit(1)

        args = Namespace(
            server_host='0.0.0.0',
            server_port=5000,
            server_api_key=None,  # No API key
            server_workers=1
        )
        config_obj = Mock(config={})

        with pytest.raises(SystemExit):
            run_server_mode(args, config_obj, {'http_access': False})

        mock_exit.assert_called_once_with(1)
        # Verify error was logged
        assert mock_logger.error.called

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.server.run_server')
    @patch('qbt_rules.server.create_app')
    @patch('qbt_rules.scheduler.Scheduler')
    @patch('qbt_rules.worker.Worker')
    @patch('qbt_rules.queue_manager.create_queue')
    @patch('qbt_rules.cli.QBittorrentAPI')
    def test_stops_worker_on_keyboard_interrupt(self, mock_api_class, mock_create_queue,
                                                 mock_worker_class, mock_scheduler_class,
                                                 mock_create_app, mock_run_server, mock_logger):
        """Should stop worker and scheduler when KeyboardInterrupt is raised"""
        mock_queue = Mock()
        mock_queue.__class__.__name__ = 'SQLiteQueue'
        mock_create_queue.return_value = mock_queue

        mock_api = Mock()
        mock_api_class.return_value = mock_api

        mock_worker = Mock()
        mock_worker_class.return_value = mock_worker

        mock_scheduler = Mock()
        mock_scheduler_class.return_value = mock_scheduler

        mock_app = Mock()
        mock_create_app.return_value = mock_app

        # Simulate KeyboardInterrupt
        mock_run_server.side_effect = KeyboardInterrupt()

        args = Namespace(
            server_host='0.0.0.0',
            server_port=5000,
            server_api_key='test-key',
            server_workers=1,
            queue_backend='sqlite',
            queue_sqlite_path='/tmp/test.db',
            queue_redis_url=None
        )
        config_obj = Mock(config={}, schedule=[], config_dir=Path('/config'))

        run_server_mode(args, config_obj, {'http_access': False})

        # Verify worker and scheduler were stopped
        mock_worker.stop.assert_called_once()
        mock_scheduler.stop.assert_called_once()


class TestRunClientMode:
    """Test run_client_mode() function"""

    @staticmethod
    def _args(**overrides):
        values = dict(context='manual', hash=None, wait=False,
                      client_server_url='http://localhost:5000', client_api_key='test-key',
                      dry_run=False)
        values.update(overrides)
        return Namespace(**values)

    @staticmethod
    def _accepted(job):
        response = Mock(status_code=202)
        response.json.return_value = {'job_id': 'job-123', 'status': 'pending',
                                      'created_at': '2024-01-01T00:00:00Z', **job}
        return response

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.post')
    def test_dry_run_flag_is_sent_to_server(self, mock_post, mock_logger):
        """--dry-run used to be dropped, so the job ran for real"""
        mock_post.return_value = self._accepted({'dry_run': True})

        run_client_mode(self._args(dry_run=True), Mock(config={}))

        assert mock_post.call_args[1]['params']['dry_run'] == 'true'

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.post')
    def test_no_dry_run_param_without_flag(self, mock_post, mock_logger):
        mock_post.return_value = self._accepted({'dry_run': False})

        run_client_mode(self._args(), Mock(config={}))

        assert 'dry_run' not in mock_post.call_args[1]['params']

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.delete')
    @patch('qbt_rules.cli.requests.post')
    def test_cancels_job_when_server_ignores_dry_run(self, mock_post, mock_delete, mock_logger):
        """An older server drops the parameter and would run for real: cancel and fail"""
        mock_post.return_value = self._accepted({})  # no dry_run field at all
        mock_delete.return_value = Mock(status_code=200)

        with pytest.raises(SystemExit) as exc:
            run_client_mode(self._args(dry_run=True, wait=True), Mock(config={}))

        assert exc.value.code == 1
        assert mock_delete.call_args[0][0] == 'http://localhost:5000/api/jobs/job-123'

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.validate_torrent_hash')
    @patch('qbt_rules.cli.requests.post')
    def test_submits_job_successfully(self, mock_post, mock_validate, mock_logger):
        """Should submit job to server successfully"""
        # Mock hash validation
        mock_validate.return_value = 'abc123'

        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 202
        mock_response.json.return_value = {
            'job_id': 'job-123',
            'status': 'pending',
            'created_at': '2024-01-01T00:00:00Z'
        }
        mock_post.return_value = mock_response

        args = Namespace(
            context='adhoc-run',
            hash='abc123',
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        run_client_mode(args, config_obj)

        # Verify hash was validated
        mock_validate.assert_called_once_with('abc123')

        # Verify request was made
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == 'http://localhost:5000/api/execute'
        assert call_args[1]['params']['context'] == 'adhoc-run'
        assert call_args[1]['params']['hash'] == 'abc123'
        assert call_args[1]['params']['key'] == 'test-key'

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.validate_torrent_hash')
    @patch('qbt_rules.cli.requests.post')
    def test_handles_connection_error(self, mock_post, mock_validate, mock_exit, mock_logger):
        """Should handle connection errors gracefully"""
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError()
        mock_exit.side_effect = SystemExit(1)
        mock_validate.return_value = 'abc123'

        args = Namespace(
            context='adhoc-run',
            hash='abc123',
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        with pytest.raises(SystemExit):
            run_client_mode(args, config_obj)

        mock_exit.assert_called_once_with(1)
        # Verify error was logged
        assert mock_logger.error.called

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    def test_exits_without_api_key(self, mock_exit, mock_logger):
        """Should exit if client API key not provided"""
        mock_exit.side_effect = SystemExit(1)

        args = Namespace(
            context='adhoc-run',
            hash='abc123',
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key=None  # No API key
        )
        config_obj = Mock(config={})

        with pytest.raises(SystemExit):
            run_client_mode(args, config_obj)

        mock_exit.assert_called_once_with(1)
        # Verify error was logged
        assert mock_logger.error.called

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.validate_torrent_hash')
    @patch('qbt_rules.cli.requests.post')
    def test_handles_auth_error(self, mock_post, mock_validate, mock_exit, mock_logger):
        """Should handle authentication errors"""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_post.return_value = mock_response
        mock_exit.side_effect = SystemExit(1)
        mock_validate.return_value = 'abc123'

        args = Namespace(
            context='adhoc-run',
            hash='abc123',
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key='wrong-key'
        )
        config_obj = Mock(config={})

        with pytest.raises(SystemExit):
            run_client_mode(args, config_obj)

        mock_exit.assert_called_once_with(1)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.validate_torrent_hash')
    @patch('qbt_rules.cli.wait_for_job')
    @patch('qbt_rules.cli.requests.post')
    def test_waits_for_job_when_requested(self, mock_post, mock_wait, mock_validate, mock_logger):
        """Should wait for job completion when --wait flag is set"""
        mock_response = Mock()
        mock_response.status_code = 202
        mock_response.json.return_value = {
            'job_id': 'job-123',
            'status': 'pending',
            'created_at': '2024-01-01T00:00:00Z'
        }
        mock_post.return_value = mock_response
        mock_validate.return_value = 'abc123'

        args = Namespace(
            context='adhoc-run',
            hash='abc123',
            wait=True,  # Wait for completion
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        run_client_mode(args, config_obj)

        # Verify wait_for_job was called
        mock_wait.assert_called_once_with(
            'http://localhost:5000',
            'test-key',
            'job-123'
        )

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.validate_torrent_hash')
    @patch('qbt_rules.cli.requests.post')
    def test_handles_timeout_error(self, mock_post, mock_validate, mock_exit, mock_logger):
        """Should handle timeout errors"""
        import requests
        mock_post.side_effect = requests.exceptions.Timeout()
        mock_exit.side_effect = SystemExit(1)
        mock_validate.return_value = 'abc123'

        args = Namespace(
            context='adhoc-run',
            hash='abc123',
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        with pytest.raises(SystemExit):
            run_client_mode(args, config_obj)

        mock_exit.assert_called_once_with(1)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.validate_torrent_hash')
    @patch('qbt_rules.cli.requests.post')
    def test_handles_server_error(self, mock_post, mock_validate, mock_exit, mock_logger):
        """Should handle server errors"""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = 'Internal Server Error'
        mock_post.return_value = mock_response
        mock_exit.side_effect = SystemExit(1)
        mock_validate.return_value = 'abc123'

        args = Namespace(
            context='adhoc-run',
            hash='abc123',
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        with pytest.raises(SystemExit):
            run_client_mode(args, config_obj)

        mock_exit.assert_called_once_with(1)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.post')
    def test_handles_no_context_or_hash(self, mock_post, mock_logger):
        """Should handle missing context and hash"""
        mock_response = Mock()
        mock_response.status_code = 202
        mock_response.json.return_value = {
            'job_id': 'job-123',
            'status': 'pending',
            'created_at': '2024-01-01T00:00:00Z'
        }
        mock_post.return_value = mock_response

        args = Namespace(
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        run_client_mode(args, config_obj)

        # Verify request was made with None values
        call_args = mock_post.call_args
        assert call_args[1]['params']['context'] is None
        assert call_args[1]['params']['hash'] is None

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.validate_torrent_hash')
    @patch('qbt_rules.cli.requests.post')
    def test_validates_torrent_hash_when_provided(self, mock_post, mock_validate, mock_exit, mock_logger):
        """Should validate torrent hash when provided"""
        # Simulate validation error
        mock_validate.side_effect = ValueError("Invalid hash")
        mock_exit.side_effect = SystemExit(1)

        args = Namespace(
            context='adhoc-run',
            hash='invalid-hash',
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        with pytest.raises(SystemExit):
            run_client_mode(args, config_obj)

        # Should have attempted validation
        mock_validate.assert_called_once_with('invalid-hash')
        # Should have exited
        mock_exit.assert_called_once_with(1)


class TestWaitForJob:
    """Test wait_for_job() function"""

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.time.sleep')
    @patch('qbt_rules.cli.requests.get')
    def test_waits_for_job_completion(self, mock_get, mock_sleep, mock_logger):
        """Should poll until job completes"""
        # First call: processing, second call: completed
        mock_response1 = Mock()
        mock_response1.status_code = 200
        mock_response1.json.return_value = {'status': 'processing'}

        mock_response2 = Mock()
        mock_response2.status_code = 200
        mock_response2.json.return_value = {
            'status': 'completed',
            'result': {
                'torrents_processed': 10,
                'rules_matched': 5,
                'actions_executed': 3
            }
        }

        mock_get.side_effect = [mock_response1, mock_response2]

        wait_for_job('http://localhost:5000', 'test-key', 'job-123')

        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.time.sleep')
    @patch('qbt_rules.cli.requests.get')
    def test_shows_error_on_failure(self, mock_get, mock_sleep, mock_exit, mock_logger):
        """Should show error when job fails"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'status': 'failed',
            'error': 'Test error'
        }
        mock_get.return_value = mock_response
        mock_exit.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            wait_for_job('http://localhost:5000', 'test-key', 'job-123')

        # Should not retry for failed job
        assert mock_get.call_count == 1
        mock_exit.assert_called_once_with(1)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.time.sleep')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_cancelled_job(self, mock_get, mock_sleep, mock_exit, mock_logger):
        """Should handle cancelled jobs"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'status': 'cancelled'}
        mock_get.return_value = mock_response
        mock_exit.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            wait_for_job('http://localhost:5000', 'test-key', 'job-123')

        mock_exit.assert_called_once_with(1)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.time.sleep')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_polling_error(self, mock_get, mock_sleep, mock_exit, mock_logger):
        """Should handle errors during polling"""
        mock_get.side_effect = Exception("Network error")
        mock_exit.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            wait_for_job('http://localhost:5000', 'test-key', 'job-123')

        mock_exit.assert_called_once_with(1)


class TestListJobsCommand:
    """Test list_jobs_command() function"""

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.get')
    def test_lists_jobs_successfully(self, mock_get, mock_cli_ui):
        """Should list jobs from server"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'total': 2,
            'jobs': [
                {
                    'job_id': 'job-1',
                    'status': 'completed',
                    'context': 'adhoc-run',
                    'created_at': '2024-01-01T00:00:00Z'
                },
                {
                    'job_id': 'job-2',
                    'status': 'pending',
                    'context': 'automatic',
                    'created_at': '2024-01-01T01:00:00Z'
                }
            ]
        }
        mock_get.return_value = mock_response

        args = Namespace(
            status_filter=None,
            limit=20,
            output='table',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        list_jobs_command(args, config_obj)

        # Verify request was made
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert 'http://localhost:5000/api/jobs' in call_args[0][0]

        # Verify table was rendered with the expected columns and rows
        mock_cli_ui.print_table.assert_called_once()
        _, kwargs = mock_cli_ui.print_table.call_args
        assert kwargs['headers'] == ['Job ID', 'Status', 'Context', 'Created']
        assert kwargs['rows'] == [
            ['job-1', 'completed', 'adhoc-run', '2024-01-01T00:00:00Z'],
            ['job-2', 'pending', 'automatic', '2024-01-01T01:00:00Z'],
        ]

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.get')
    def test_lists_jobs_as_json(self, mock_get, mock_cli_ui):
        """Should print the raw API response as JSON when --output json is set"""
        mock_response = Mock()
        mock_response.status_code = 200
        response_data = {
            'total': 1,
            'jobs': [{'job_id': 'job-1', 'status': 'completed', 'context': None, 'created_at': '2024-01-01T00:00:00Z'}]
        }
        mock_response.json.return_value = response_data
        mock_get.return_value = mock_response

        args = Namespace(
            status_filter=None,
            limit=20,
            output='json',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        list_jobs_command(args, config_obj)

        mock_cli_ui.print_json.assert_called_once_with(response_data)
        mock_cli_ui.print_table.assert_not_called()

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_no_jobs(self, mock_get, mock_cli_ui):
        """Should handle empty job list"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'total': 0,
            'jobs': []
        }
        mock_get.return_value = mock_response

        args = Namespace(
            output='table',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        list_jobs_command(args, config_obj)

        mock_cli_ui.print_message.assert_called_once_with("No jobs found")
        mock_cli_ui.print_table.assert_not_called()

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_error(self, mock_get, mock_exit, mock_logger):
        """Should handle errors"""
        mock_get.side_effect = Exception("Network error")

        args = Namespace(
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        list_jobs_command(args, config_obj)

        mock_exit.assert_called_once_with(1)


class TestJobStatusCommand:
    """Test job_status_command() function"""

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.get')
    def test_gets_job_status(self, mock_get, mock_cli_ui):
        """Should get job status from server"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'job_id': 'job-123',
            'status': 'completed',
            'context': 'adhoc-run',
            'hash': 'abc123',
            'created_at': '2024-01-01T00:00:00Z',
            'started_at': '2024-01-01T00:00:01Z',
            'completed_at': '2024-01-01T00:00:10Z',
            'result': {
                'torrents_processed': 10
            }
        }
        mock_get.return_value = mock_response

        args = Namespace(
            job_status='job-123',
            output='table',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        job_status_command(args, config_obj)

        # Verify request was made
        mock_get.assert_called_once()

        # Verify job details and result sections were rendered
        assert mock_cli_ui.print_kv_section.call_count == 2
        details_call, result_call = mock_cli_ui.print_kv_section.call_args_list
        assert details_call.args[0] == "Job Details:"
        assert ("Job ID", "job-123") in details_call.args[1]
        assert ("Context", "adhoc-run") in details_call.args[1]
        assert result_call.args[0] == "Result:"
        assert result_call.args[1] == [("torrents_processed", 10)]

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.get')
    def test_gets_job_status_as_json(self, mock_get, mock_cli_ui):
        """Should print the raw API response as JSON when --output json is set"""
        mock_response = Mock()
        mock_response.status_code = 200
        job_data = {'job_id': 'job-123', 'status': 'completed'}
        mock_response.json.return_value = job_data
        mock_get.return_value = mock_response

        args = Namespace(
            job_status='job-123',
            output='json',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        job_status_command(args, config_obj)

        mock_cli_ui.print_json.assert_called_once_with(job_data)
        mock_cli_ui.print_kv_section.assert_not_called()

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_job_not_found(self, mock_get, mock_logger):
        """Should handle job not found"""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        args = Namespace(
            job_status='nonexistent',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        job_status_command(args, config_obj)

        # Verify error was logged
        assert any('Job not found' in str(call) for call in mock_logger.error.call_args_list)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_error(self, mock_get, mock_exit, mock_logger):
        """Should handle errors"""
        mock_get.side_effect = Exception("Network error")

        args = Namespace(
            job_status='job-123',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        job_status_command(args, config_obj)

        mock_exit.assert_called_once_with(1)

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.get')
    def test_displays_job_with_error(self, mock_get, mock_cli_ui):
        """Should display job with error"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'job_id': 'job-123',
            'status': 'failed',
            'context': 'adhoc-run',
            'created_at': '2024-01-01T00:00:00Z',
            'started_at': '2024-01-01T00:00:01Z',
            'completed_at': '2024-01-01T00:00:10Z',
            'error': 'Connection timeout'
        }
        mock_get.return_value = mock_response

        args = Namespace(
            job_status='job-123',
            output='table',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        job_status_command(args, config_obj)

        # Verify the error detail was printed
        mock_cli_ui.print_message.assert_called_once()
        assert 'Connection timeout' in mock_cli_ui.print_message.call_args.args[0]


class TestCancelJobCommand:
    """Test cancel_job_command() function"""

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.delete')
    def test_cancels_job(self, mock_delete, mock_cli_ui):
        """Should cancel job on server"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_delete.return_value = mock_response

        args = Namespace(
            cancel_job_id='job-123',
            output='table',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        cancel_job_command(args, config_obj)

        # Verify request was made
        mock_delete.assert_called_once()
        # Verify success was announced
        mock_cli_ui.print_message.assert_called_once_with("✓ Job cancelled: job-123")

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.delete')
    def test_cancels_job_as_json(self, mock_delete, mock_cli_ui):
        """Should print the raw API response as JSON when --output json is set"""
        mock_response = Mock()
        mock_response.status_code = 200
        response_data = {'job_id': 'job-123', 'status': 'cancelled', 'message': 'Job cancelled successfully'}
        mock_response.json.return_value = response_data
        mock_delete.return_value = mock_response

        args = Namespace(
            cancel_job_id='job-123',
            output='json',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        cancel_job_command(args, config_obj)

        mock_cli_ui.print_json.assert_called_once_with(response_data)
        mock_cli_ui.print_message.assert_not_called()

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.delete')
    def test_handles_cannot_cancel(self, mock_delete, mock_logger):
        """Should handle cannot cancel error"""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            'message': 'Job already completed'
        }
        mock_delete.return_value = mock_response

        args = Namespace(
            cancel_job_id='job-123',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        cancel_job_command(args, config_obj)

        # Verify error was logged
        assert any('Cannot cancel job' in str(call) for call in mock_logger.error.call_args_list)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.delete')
    def test_handles_job_not_found(self, mock_delete, mock_logger):
        """Should handle job not found"""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_delete.return_value = mock_response

        args = Namespace(
            cancel_job_id='nonexistent',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        cancel_job_command(args, config_obj)

        # Verify error was logged
        assert any('Job not found' in str(call) for call in mock_logger.error.call_args_list)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.requests.delete')
    def test_handles_error(self, mock_delete, mock_exit, mock_logger):
        """Should handle errors"""
        mock_delete.side_effect = Exception("Network error")

        args = Namespace(
            cancel_job_id='job-123',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        cancel_job_command(args, config_obj)

        mock_exit.assert_called_once_with(1)


class TestStatsCommand:
    """Test stats_command() function"""

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.get')
    def test_gets_stats(self, mock_get, mock_cli_ui):
        """Should get server stats"""
        mock_response = Mock()
        mock_response.status_code = 200
        stats_data = {
            'jobs': {
                'total': 100,
                'pending': 5,
                'processing': 1,
                'completed': 90,
                'failed': 3,
                'cancelled': 1
            },
            'performance': {
                'average_execution_time': 2.5
            },
            'queue': {
                'backend': 'SQLite',
                'depth': 5
            },
            'worker': {
                'status': 'idle',
                'last_job_completed': '2024-01-01T00:00:00Z'
            }
        }
        mock_response.json.return_value = stats_data
        mock_get.return_value = mock_response

        args = Namespace(
            output='table',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        stats_command(args, config_obj)

        # Verify request was made
        mock_get.assert_called_once()

        # Verify all four sections were rendered
        assert mock_cli_ui.print_kv_section.call_count == 4
        titles = [c.args[0] for c in mock_cli_ui.print_kv_section.call_args_list]
        assert titles == ["Jobs:", "Performance:", "Queue:", "Worker:"]
        jobs_call = mock_cli_ui.print_kv_section.call_args_list[0]
        assert ("Total", 100) in jobs_call.args[1]

    @patch('qbt_rules.cli.cli_ui')
    @patch('qbt_rules.cli.requests.get')
    def test_gets_stats_as_json(self, mock_get, mock_cli_ui):
        """Should print the raw API response as JSON when --output json is set"""
        mock_response = Mock()
        mock_response.status_code = 200
        stats_data = {
            'jobs': {'total': 100, 'pending': 5, 'processing': 1, 'completed': 90, 'failed': 3, 'cancelled': 1},
            'performance': {'average_execution_time': 2.5},
            'queue': {'backend': 'SQLite', 'depth': 5},
            'worker': {'status': 'idle', 'last_job_completed': '2024-01-01T00:00:00Z'}
        }
        mock_response.json.return_value = stats_data
        mock_get.return_value = mock_response

        args = Namespace(
            output='json',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        stats_command(args, config_obj)

        mock_cli_ui.print_json.assert_called_once_with(stats_data)
        mock_cli_ui.print_kv_section.assert_not_called()

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_server_error(self, mock_get, mock_logger):
        """Should handle server errors"""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        args = Namespace(
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        stats_command(args, config_obj)

        # Verify error was logged
        assert any('Server error' in str(call) for call in mock_logger.error.call_args_list)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_error(self, mock_get, mock_exit, mock_logger):
        """Should handle errors"""
        mock_get.side_effect = Exception("Network error")

        args = Namespace(
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        stats_command(args, config_obj)

        mock_exit.assert_called_once_with(1)


class TestRunClientModeEdgeCases:
    """Test edge cases in run_client_mode() function"""

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.validate_torrent_hash')
    @patch('qbt_rules.cli.requests.post')
    def test_handles_generic_exception(self, mock_post, mock_validate, mock_exit, mock_logger):
        """Should handle generic exceptions"""
        mock_validate.return_value = 'abc123'
        mock_post.side_effect = RuntimeError("Unexpected error")
        mock_exit.side_effect = SystemExit(1)

        args = Namespace(
            context='adhoc-run',
            hash='abc123',
            wait=False,
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        with pytest.raises(SystemExit):
            run_client_mode(args, config_obj)

        mock_exit.assert_called_once_with(1)
        # Verify error was logged
        assert any('Unexpected error' in str(call) for call in mock_logger.error.call_args_list)


class TestWaitForJobEdgeCases:
    """Test edge cases in wait_for_job() function"""

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.sys.exit')
    @patch('qbt_rules.cli.time.sleep')
    @patch('qbt_rules.cli.requests.get')
    def test_handles_timeout(self, mock_get, mock_sleep, mock_exit, mock_logger):
        """Should timeout if job doesn't complete within max_wait"""
        # Always return processing status
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'status': 'processing'}
        mock_get.return_value = mock_response
        mock_exit.side_effect = SystemExit(1)

        with pytest.raises(SystemExit):
            wait_for_job('http://localhost:5000', 'test-key', 'job-123')

        # Should have called many times (polling)
        assert mock_get.call_count > 10
        mock_exit.assert_called_once_with(1)
        # Verify timeout error was logged
        assert any('did not complete' in str(call) for call in mock_logger.error.call_args_list)


class TestCommandEdgeCases:
    """Test edge cases in command functions"""

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.get')
    def test_list_jobs_server_error(self, mock_get, mock_logger):
        """Should handle server errors in list_jobs_command"""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        args = Namespace(
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        list_jobs_command(args, config_obj)

        # Verify server error was logged
        assert any('Server error' in str(call) for call in mock_logger.error.call_args_list)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.get')
    def test_job_status_server_error(self, mock_get, mock_logger):
        """Should handle server errors in job_status_command"""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_get.return_value = mock_response

        args = Namespace(
            job_status='job-123',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        job_status_command(args, config_obj)

        # Verify server error was logged
        assert any('Server error' in str(call) for call in mock_logger.error.call_args_list)

    @patch('qbt_rules.cli.logger')
    @patch('qbt_rules.cli.requests.delete')
    def test_cancel_job_server_error(self, mock_delete, mock_logger):
        """Should handle server errors in cancel_job_command"""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_delete.return_value = mock_response

        args = Namespace(
            cancel_job_id='job-123',
            client_server_url='http://localhost:5000',
            client_api_key='test-key'
        )
        config_obj = Mock(config={})

        cancel_job_command(args, config_obj)

        # Verify server error was logged
        assert any('Server error' in str(call) for call in mock_logger.error.call_args_list)


class TestMain:
    """Test main() entry point function"""

    @patch('qbt_rules.cli.sys.argv', ['qbt-rules', '--serve'])
    @patch('qbt_rules.cli.run_server_mode')
    @patch('qbt_rules.arguments.handle_utility_args')
    @patch('qbt_rules.cli.get_logger')
    @patch('qbt_rules.cli.setup_logging')
    @patch('qbt_rules.cli.load_config')
    @patch('qbt_rules.arguments.process_args')
    @patch('qbt_rules.cli.sys.exit')
    def test_main_server_mode(self, mock_exit, mock_process_args,
                               mock_load_config, mock_setup_logging, mock_get_logger,
                               mock_handle_utility, mock_run_server):
        """Should run in server mode when --serve flag provided"""
        mock_process_args.return_value = '/config'

        mock_config = Mock(config={}, config_dir=Path('/config'))
        mock_load_config.return_value = mock_config

        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger

        mock_handle_utility.return_value = None

        from qbt_rules.cli import main
        main()

        assert mock_run_server.called
        mock_exit.assert_called_with(0)

    @patch('qbt_rules.cli.sys.argv', ['qbt-rules', '--list-jobs'])
    @patch('qbt_rules.cli.list_jobs_command')
    @patch('qbt_rules.arguments.handle_utility_args')
    @patch('qbt_rules.cli.get_logger')
    @patch('qbt_rules.cli.setup_logging')
    @patch('qbt_rules.cli.load_config')
    @patch('qbt_rules.arguments.process_args')
    @patch('qbt_rules.cli.sys.exit')
    def test_main_list_jobs_mode(self, mock_exit, mock_process_args,
                                  mock_load_config, mock_setup_logging, mock_get_logger,
                                  mock_handle_utility, mock_list_jobs):
        """Should run list_jobs command"""
        mock_process_args.return_value = '/config'

        mock_config = Mock(config={}, config_dir=Path('/config'))
        mock_load_config.return_value = mock_config

        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger

        mock_handle_utility.return_value = None

        from qbt_rules.cli import main
        main()

        assert mock_list_jobs.called
        mock_exit.assert_called_with(0)

    @patch('qbt_rules.cli.sys.argv', ['qbt-rules', '--job-status', 'job-123'])
    @patch('qbt_rules.cli.job_status_command')
    @patch('qbt_rules.arguments.handle_utility_args')
    @patch('qbt_rules.cli.get_logger')
    @patch('qbt_rules.cli.setup_logging')
    @patch('qbt_rules.cli.load_config')
    @patch('qbt_rules.arguments.process_args')
    @patch('qbt_rules.cli.sys.exit')
    def test_main_job_status_mode(self, mock_exit, mock_process_args,
                                   mock_load_config, mock_setup_logging, mock_get_logger,
                                   mock_handle_utility, mock_job_status):
        """Should run job_status command"""
        mock_process_args.return_value = '/config'

        mock_config = Mock(config={}, config_dir=Path('/config'))
        mock_load_config.return_value = mock_config

        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger

        mock_handle_utility.return_value = None

        from qbt_rules.cli import main
        main()

        assert mock_job_status.called
        mock_exit.assert_called_with(0)

    @patch('qbt_rules.cli.sys.argv', ['qbt-rules', '--cancel-job', 'job-123'])
    @patch('qbt_rules.cli.cancel_job_command')
    @patch('qbt_rules.arguments.handle_utility_args')
    @patch('qbt_rules.cli.get_logger')
    @patch('qbt_rules.cli.setup_logging')
    @patch('qbt_rules.cli.load_config')
    @patch('qbt_rules.arguments.process_args')
    @patch('qbt_rules.cli.sys.exit')
    def test_main_cancel_job_mode(self, mock_exit, mock_process_args,
                                   mock_load_config, mock_setup_logging, mock_get_logger,
                                   mock_handle_utility, mock_cancel_job):
        """Should run cancel_job command"""
        mock_process_args.return_value = '/config'

        mock_config = Mock(config={}, config_dir=Path('/config'))
        mock_load_config.return_value = mock_config

        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger

        mock_handle_utility.return_value = None

        from qbt_rules.cli import main
        main()

        assert mock_cancel_job.called
        mock_exit.assert_called_with(0)

    @patch('qbt_rules.cli.sys.argv', ['qbt-rules', '--stats'])
    @patch('qbt_rules.cli.stats_command')
    @patch('qbt_rules.arguments.handle_utility_args')
    @patch('qbt_rules.cli.get_logger')
    @patch('qbt_rules.cli.setup_logging')
    @patch('qbt_rules.cli.load_config')
    @patch('qbt_rules.arguments.process_args')
    @patch('qbt_rules.cli.sys.exit')
    def test_main_stats_mode(self, mock_exit, mock_process_args,
                              mock_load_config, mock_setup_logging, mock_get_logger,
                              mock_handle_utility, mock_stats):
        """Should run stats command"""
        mock_process_args.return_value = '/config'

        mock_config = Mock(config={}, config_dir=Path('/config'))
        mock_load_config.return_value = mock_config

        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger

        mock_handle_utility.return_value = None

        from qbt_rules.cli import main
        main()

        assert mock_stats.called
        mock_exit.assert_called_with(0)

    @patch('qbt_rules.cli.sys.argv', ['qbt-rules', '--context', 'adhoc-run'])
    @patch('qbt_rules.cli.run_client_mode')
    @patch('qbt_rules.arguments.handle_utility_args')
    @patch('qbt_rules.cli.get_logger')
    @patch('qbt_rules.cli.setup_logging')
    @patch('qbt_rules.cli.load_config')
    @patch('qbt_rules.arguments.process_args')
    @patch('qbt_rules.cli.sys.exit')
    def test_main_client_mode(self, mock_exit, mock_process_args,
                               mock_load_config, mock_setup_logging, mock_get_logger,
                               mock_handle_utility, mock_run_client):
        """Should run in client mode by default"""
        mock_process_args.return_value = '/config'

        mock_config = Mock(config={}, config_dir=Path('/config'))
        mock_load_config.return_value = mock_config

        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger

        mock_handle_utility.return_value = None

        from qbt_rules.cli import main
        main()

        assert mock_run_client.called
        mock_exit.assert_called_with(0)

    @pytest.mark.parametrize('exit_code', [0, 1])
    @patch('qbt_rules.cli.sys.argv', ['qbt-rules', '--validate'])
    @patch('qbt_rules.arguments.handle_utility_args')
    @patch('qbt_rules.cli.get_logger')
    @patch('qbt_rules.cli.setup_logging')
    @patch('qbt_rules.cli.load_config')
    @patch('qbt_rules.arguments.process_args')
    @patch('qbt_rules.cli.sys.exit')
    def test_main_utility_mode_propagates_exit_status(self, mock_exit, mock_process_args,
                                mock_load_config, mock_setup_logging, mock_get_logger,
                                mock_handle_utility, exit_code):
        """Should handle utility arguments like --validate"""
        mock_process_args.return_value = '/config'

        mock_config = Mock(config={}, config_dir=Path('/config'))
        mock_load_config.return_value = mock_config

        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger

        # Utility mode exits early
        mock_handle_utility.return_value = exit_code
        def terminate(code):
            raise SystemExit(code)

        mock_exit.side_effect = terminate

        from qbt_rules.cli import main
        with pytest.raises(SystemExit) as error:
            main()

        assert error.value.code == exit_code
        mock_exit.assert_called_once_with(exit_code)
