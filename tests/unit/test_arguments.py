"""Tests for arguments module."""

import os
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from qbt_rules.arguments import (
    smart_config_default,
    create_parser,
    validate_torrent_hash,
    process_args,
    handle_utility_args,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clean up environment variables before and after each test."""
    # Clean up before test
    monkeypatch.delenv('QBT_RULES_DRY_RUN', raising=False)

    yield

    # Clean up after test
    monkeypatch.delenv('QBT_RULES_DRY_RUN', raising=False)


class TestSmartConfigDefault:
    """Test smart config directory default logic."""

    def test_returns_local_config_when_exists(self, tmp_path, monkeypatch):
        """Returns ./config when it exists as a directory."""
        # Change to tmp_path and create config directory
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        result = smart_config_default()
        assert result == "./config"

    def test_returns_docker_config_when_local_missing(self, tmp_path, monkeypatch):
        """Returns /config when ./config doesn't exist."""
        # Change to tmp_path where no config dir exists
        monkeypatch.chdir(tmp_path)

        result = smart_config_default()
        assert result == "/config"

    def test_returns_docker_config_when_local_is_file(self, tmp_path, monkeypatch):
        """Returns /config when ./config exists but is a file."""
        # Change to tmp_path and create config as a file
        monkeypatch.chdir(tmp_path)
        config_file = tmp_path / "config"
        config_file.write_text("not a directory")

        result = smart_config_default()
        assert result == "/config"


class TestCreateParser:
    """Test argument parser creation."""

    def test_create_parser_returns_argumentparser(self):
        """create_parser returns ArgumentParser instance."""
        parser = create_parser()
        assert parser is not None
        assert hasattr(parser, 'parse_args')

    def test_parser_has_context_argument(self):
        """Parser includes --context argument."""
        parser = create_parser()
        args = parser.parse_args(['--context', 'adhoc-run'])
        assert args.context == 'adhoc-run'

    def test_parser_has_torrent_hash_argument(self):
        """Parser includes --hash argument."""
        parser = create_parser()
        args = parser.parse_args(['--hash', 'a' * 40])
        assert args.hash == 'a' * 40

    def test_parser_has_config_dir_argument(self):
        """Parser includes --config-dir argument."""
        parser = create_parser()
        args = parser.parse_args(['--config-dir', '/tmp/config'])
        assert args.config_dir == Path('/tmp/config')

    def test_parser_has_dry_run_argument(self):
        """Parser includes --dry-run argument."""
        parser = create_parser()
        args = parser.parse_args(['--dry-run'])
        assert args.dry_run is True

    def test_parser_dry_run_default_false(self):
        """Parser --dry-run defaults to False."""
        parser = create_parser()
        args = parser.parse_args([])
        assert args.dry_run is False

    def test_parser_has_log_level_argument(self):
        """Parser includes --log-level argument."""
        parser = create_parser()
        args = parser.parse_args(['--log-level', 'DEBUG'])
        assert args.log_level == 'DEBUG'

    def test_parser_log_level_choices(self):
        """Parser --log-level accepts valid choices."""
        parser = create_parser()
        for level in ['DEBUG', 'INFO', 'WARNING', 'ERROR']:
            args = parser.parse_args(['--log-level', level])
            assert args.log_level == level

    def test_parser_has_trace_argument(self):
        """Parser includes --trace argument."""
        parser = create_parser()
        args = parser.parse_args(['--trace'])
        assert args.trace is True

    def test_parser_trace_default_false(self):
        """Parser --trace defaults to False."""
        parser = create_parser()
        args = parser.parse_args([])
        assert args.trace is False

    def test_parser_has_output_argument(self):
        """Parser includes --output argument."""
        parser = create_parser()
        args = parser.parse_args(['--output', 'json'])
        assert args.output == 'json'

    def test_parser_output_choices(self):
        """Parser --output accepts valid choices."""
        parser = create_parser()
        for output_format in ['table', 'json']:
            args = parser.parse_args(['--output', output_format])
            assert args.output == output_format

    def test_parser_output_default_table(self):
        """Parser --output defaults to 'table'."""
        parser = create_parser()
        args = parser.parse_args([])
        assert args.output == 'table'

    def test_parser_has_validate_argument(self):
        """Parser includes --validate argument."""
        parser = create_parser()
        args = parser.parse_args(['--validate'])
        assert args.validate is True

    def test_parser_has_list_rules_argument(self):
        """Parser includes --list-rules argument."""
        parser = create_parser()
        args = parser.parse_args(['--list-rules'])
        assert args.list_rules is True

    def test_parser_has_version_argument(self):
        """Parser includes --version argument."""
        parser = create_parser()
        # Version argument exits, so we can't test it directly
        # Just verify the parser was created successfully
        assert parser is not None


class TestValidateTorrentHash:
    """Test torrent hash validation."""

    def test_valid_hash_lowercase(self):
        """Valid lowercase hex hash passes."""
        hash_val = 'a' * 40
        result = validate_torrent_hash(hash_val)
        assert result == hash_val

    def test_valid_hash_uppercase_returns_lowercase(self):
        """Valid uppercase hex hash is converted to lowercase."""
        hash_val = 'A' * 40
        result = validate_torrent_hash(hash_val)
        assert result == 'a' * 40

    def test_valid_hash_mixed_case_returns_lowercase(self):
        """Valid mixed case hex hash is converted to lowercase."""
        hash_val = 'AbCdEf0123456789' * 2 + '01234567'  # 40 chars
        result = validate_torrent_hash(hash_val)
        assert result == hash_val.lower()

    def test_valid_hex_characters(self):
        """Valid hash with all hex characters (0-9, a-f)."""
        hash_val = '0123456789abcdef' * 2 + '01234567'  # 40 chars
        result = validate_torrent_hash(hash_val)
        assert result == hash_val.lower()

    def test_none_raises_valueerror(self):
        """None input raises ValueError."""
        with pytest.raises(ValueError, match="Torrent hash is required"):
            validate_torrent_hash(None)

    def test_empty_string_raises_valueerror(self):
        """Empty string raises ValueError."""
        with pytest.raises(ValueError, match="Torrent hash is required"):
            validate_torrent_hash('')

    def test_too_short_raises_valueerror(self):
        """Hash with 39 characters raises ValueError."""
        with pytest.raises(ValueError, match="Invalid torrent hash length"):
            validate_torrent_hash('a' * 39)

    def test_too_long_raises_valueerror(self):
        """Hash with 41 characters raises ValueError."""
        with pytest.raises(ValueError, match="Invalid torrent hash length"):
            validate_torrent_hash('a' * 41)

    def test_invalid_characters_raises_valueerror(self):
        """Hash with non-hex characters raises ValueError."""
        with pytest.raises(ValueError, match="Invalid torrent hash format"):
            validate_torrent_hash('g' * 40)

    def test_special_characters_raises_valueerror(self):
        """Hash with special characters raises ValueError."""
        with pytest.raises(ValueError, match="Invalid torrent hash format"):
            validate_torrent_hash('a' * 39 + '!')

    def test_spaces_raises_valueerror(self):
        """Hash with spaces raises ValueError."""
        with pytest.raises(ValueError, match="Invalid torrent hash format"):
            validate_torrent_hash('a' * 20 + ' ' + 'a' * 19)


class TestProcessArgs:
    """Test argument processing."""

    def test_sets_dry_run_env_var_when_true(self, monkeypatch):
        """Sets QBT_RULES_DRY_RUN environment variable when args.dry_run=True."""
        # Clean env
        monkeypatch.delenv('QBT_RULES_DRY_RUN', raising=False)

        args = Mock()
        args.dry_run = True
        args.log_level = None
        args.trace = False
        args.config_dir = '/test/config'

        process_args(args)

        assert os.environ.get('QBT_RULES_DRY_RUN') == 'true'

    def test_does_not_set_dry_run_when_false(self, monkeypatch):
        """Doesn't set QBT_RULES_DRY_RUN environment variable when args.dry_run=False."""
        # Clean env
        monkeypatch.delenv('QBT_RULES_DRY_RUN', raising=False)

        args = Mock()
        args.dry_run = False
        args.log_level = None
        args.trace = False
        args.config_dir = '/test/config'

        process_args(args)

        assert 'QBT_RULES_DRY_RUN' not in os.environ

    def test_does_not_set_bare_log_env_vars(self, monkeypatch):
        """--log-level/--trace are resolved directly by cli.py's
        get_logging_config(), not by setting bare LOG_LEVEL/TRACE_MODE
        env vars here."""
        monkeypatch.delenv('LOG_LEVEL', raising=False)
        monkeypatch.delenv('TRACE_MODE', raising=False)

        args = Mock()
        args.dry_run = False
        args.log_level = 'DEBUG'
        args.trace = True
        args.config_dir = '/test/config'

        process_args(args)

        assert 'LOG_LEVEL' not in os.environ
        assert 'TRACE_MODE' not in os.environ

    def test_config_dir_from_args_highest_priority(self, monkeypatch):
        """config_dir from args.config_dir has highest priority."""
        # Set env var
        monkeypatch.setenv('CONFIG_DIR', '/env/config')

        args = Mock()
        args.dry_run = False
        args.log_level = None
        args.trace = False
        args.config_dir = Path('/args/config')  # Parser returns Path

        result = process_args(args)

        assert result == Path('/args/config')

    def test_config_dir_from_env_var_medium_priority(self, monkeypatch):
        """config_dir from CONFIG_DIR env var when args.config_dir is None."""
        # Set env var
        monkeypatch.setenv('CONFIG_DIR', '/env/config')

        args = Mock()
        args.dry_run = False
        args.log_level = None
        args.trace = False
        args.config_dir = None

        result = process_args(args)

        assert result == Path('/env/config')

    def test_config_dir_from_smart_default_lowest_priority(self, monkeypatch, tmp_path):
        """config_dir from smart_config_default() when no args or env."""
        # Clean env
        monkeypatch.delenv('CONFIG_DIR', raising=False)
        # Create config dir for smart default
        monkeypatch.chdir(tmp_path)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        args = Mock()
        args.dry_run = False
        args.log_level = None
        args.trace = False
        args.config_dir = None

        result = process_args(args)

        assert result == Path('./config')

    def test_returns_path_object(self):
        """process_args returns Path object."""
        args = Mock()
        args.dry_run = False
        args.log_level = None
        args.trace = False
        args.config_dir = Path('/test/config')  # Parser returns Path

        result = process_args(args)

        assert isinstance(result, Path)
        assert result == Path('/test/config')


class TestHandleUtilityArgs:
    """Test utility argument handling."""

    def test_returns_none_when_no_utility_args(self):
        """Returns None when neither validate nor list_rules is set."""
        args = Mock()
        args.validate = False
        args.list_rules = False

        mock_config = Mock()

        result = handle_utility_args(args, mock_config)

        assert result is None

    def test_validate_returns_success_status(self):
        """Returns exit code zero for valid configuration."""
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_qbittorrent_config.return_value = {
            'host': 'localhost',
            'user': 'admin',
            'pass': 'admin'
        }
        mock_config.get_rules.return_value = []

        result = handle_utility_args(args, mock_config)

        assert result == 0

    def test_validate_checks_qbittorrent_config(self):
        """Validate checks qBittorrent configuration via the real
        get_qbittorrent_config() (cli.py), not a Config method -- the
        deferred `from qbt_rules.cli import get_qbittorrent_config` inside
        handle_utility_args() means the patch target is where it's
        defined, not where it's used."""
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_rules.return_value = []

        with patch('qbt_rules.cli.get_qbittorrent_config') as mock_get_qbt:
            mock_get_qbt.return_value = {
                'host': 'localhost',
                'username': 'admin',
                'password': 'admin',
            }
            handle_utility_args(args, mock_config)

            mock_get_qbt.assert_called_once_with(args, mock_config)

    def test_validate_with_missing_qbittorrent_config(self, capsys):
        """Validate handles missing qBittorrent config fields."""
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_rules.return_value = []

        with patch('qbt_rules.cli.get_qbittorrent_config') as mock_get_qbt:
            mock_get_qbt.return_value = {
                'host': 'localhost',
                'username': '',
                'password': '',
            }
            result = handle_utility_args(args, mock_config)

        # Invalid configuration returns a failing exit code.
        assert result == 1

    def test_validate_loads_rules(self):
        """Validate loads and counts rules."""
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_rules.return_value = [
            {'name': 'Rule 1', 'enabled': True, 'conditions': {'all': []}, 'actions': []},
            {'name': 'Rule 2', 'enabled': True, 'conditions': {'all': []}, 'actions': []},
        ]

        with patch('qbt_rules.cli.get_qbittorrent_config') as mock_get_qbt:
            mock_get_qbt.return_value = {
                'host': 'localhost',
                'username': 'admin',
                'password': 'admin',
            }
            handle_utility_args(args, mock_config)

        # Should call get_rules
        mock_config.get_rules.assert_called_once()

    def test_validate_reports_invalid_action(self, caplog):
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_rules.return_value = [
            {'id': 'bad-rule', 'conditions': {}, 'actions': [{'type': 'set_catgory'}]}
        ]

        with patch('qbt_rules.cli.get_qbittorrent_config') as mock_get_qbt:
            mock_get_qbt.return_value = {
                'host': 'localhost',
                'username': 'admin',
                'password': 'admin',
            }
            result = handle_utility_args(args, mock_config)

        assert result == 1
        assert any('unknown action type' in record.message.lower() for record in caplog.records)
        mock_config.get_rules.assert_called_once_with()

    @pytest.mark.parametrize('action_type, expected', [('set_catgory', 1), ('set_category', 0)])
    def test_validate_checks_actions_after_reference_resolution(self, tmp_path, caplog,
                                                               action_type, expected):
        from qbt_rules.config import Config

        (tmp_path / 'config.yml').write_text('{}\n', encoding='utf-8')
        (tmp_path / 'rules.yml').write_text(
            "refs:\n"
            "  actions:\n"
            "    shared:\n"
            f"      - type: {action_type}\n"
            "rules:\n"
            "  - id: referenced-rule\n"
            "    conditions: []\n"
            "    actions:\n"
            "      - $ref: actions.shared\n",
            encoding='utf-8',
        )
        args = Mock(validate=True, list_rules=False)
        config = Config(tmp_path)

        with patch('qbt_rules.cli.get_qbittorrent_config') as mock_get_qbt:
            mock_get_qbt.return_value = {
                'host': 'localhost',
                'username': 'admin',
                'password': 'admin',
            }
            result = handle_utility_args(args, config)

        assert result == expected
        if expected:
            assert any('unknown action type' in record.message.lower() for record in caplog.records)

    @pytest.mark.parametrize('action, notifications, expected, diagnostic', [
        ({'type': 'set_category'}, {}, 0, None),
        ({'type': 'set_upload_limit'}, {}, 0, None),
        ({'type': 'set_download_limit'}, {}, 0, None),
        ({'type': 'delete_torrent', 'params': {'keep_files': True}}, {}, 0, 'ignored'),
        ({'type': 'start', 'params': {'extra': True}}, {}, 0, 'ignored'),
        ({'type': 'notify', 'params': {'service': 'generic', 'body': [1, 2],
                                     'url': 'https://example.invalid/hook'}}, {}, 0, None),
        ({'type': 'notify', 'params': {'body': [1, 2]}},
         {'service': 'generic', 'webhook_url': 'https://example.invalid/hook'}, 0, None),
        ({'type': 'notify', 'params': {'message': 'hello'}},
         {'service': 'discord', 'webhook_url': 'https://example.invalid/hook'}, 0, None),
        ({'type': 'notify', 'params': {'service': 'generic', 'body': False}},
         {'service': 'discord', 'webhook_url': 'https://example.invalid/hook'}, 0, None),
        ({'type': 'add_tag'}, {}, 1, 'params.tags'),
        ({'type': 'unknown'}, {}, 1, 'Unknown action type'),
        ({'type': 'notify', 'params': {'message': 'hello'}},
         {'webhook_url': 'https://example.invalid/hook', 'service': 'invalid'},
         1, 'notifications.service'),
        ({'type': 'notify', 'params': {'message': 'hello'}}, {},
         1, 'notifications.webhook_url'),
        ({'type': 'notify', 'params': {'body': [1, 2]}},
         {'service': 'discord', 'webhook_url': 'https://example.invalid/hook'},
         1, 'notify needs params.message'),
    ])
    def test_validate_matches_shared_action_rules(self, tmp_path, caplog, action,
                                                notifications, expected, diagnostic):
        import yaml

        from qbt_rules.config import Config

        (tmp_path / 'config.yml').write_text(yaml.safe_dump({
            'qbittorrent': {'host': 'localhost', 'username': 'admin', 'password': 'example'},
            'notifications': notifications,
        }), encoding='utf-8')
        (tmp_path / 'rules.yml').write_text(yaml.safe_dump({'rules': [{
            'id': 'compatibility',
            'conditions': [{'field': 'info.ratio', 'operator': '>=', 'value': 1}],
            'actions': [action],
        }]}), encoding='utf-8')
        args = create_parser().parse_args(['--validate'])

        assert handle_utility_args(args, Config(tmp_path)) == expected
        if diagnostic:
            assert any(diagnostic in record.message for record in caplog.records)

    def test_validate_with_no_rules_warns(self, capsys):
        """Validate warns when no rules defined."""
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_qbittorrent_config.return_value = {
            'host': 'localhost',
            'user': 'admin',
            'pass': 'admin'
        }
        mock_config.get_rules.return_value = []

        handle_utility_args(args, mock_config)

        # Should complete without error

    def test_list_rules_returns_success_status(self):
        """Returns exit code zero when args.list_rules=True."""
        args = Mock()
        args.validate = False
        args.list_rules = True
        args.output = 'table'

        mock_config = Mock()
        mock_config.get_rules.return_value = []

        result = handle_utility_args(args, mock_config)

        assert result == 0

    @patch('qbt_rules.cli_ui.print_table')
    def test_list_rules_displays_rules(self, mock_print_table):
        """list_rules displays all rules."""
        args = Mock()
        args.validate = False
        args.list_rules = True
        args.output = 'table'

        mock_config = Mock()
        mock_config.get_rules.return_value = [
            {
                'id': 'test-rule',
                'meta': {'description': 'Test Rule'},
                'enabled': True,
                'stop_on_match': False,
                'conditions': {'trigger': 'adhoc-run', 'all': []},
                'actions': []
            }
        ]

        handle_utility_args(args, mock_config)

        # Should call get_rules
        mock_config.get_rules.assert_called_once()

        mock_print_table.assert_called_once()
        _, kwargs = mock_print_table.call_args
        assert kwargs['headers'] == ['#', 'Enabled', 'Stop', 'Context', 'ID', 'Description']
        assert kwargs['rows'] == [[1, '✓', '-', 'any', 'test-rule', 'Test Rule']]

    @patch('qbt_rules.cli_ui.print_table')
    def test_list_rules_with_enabled_status(self, mock_print_table):
        """list_rules shows enabled status."""
        args = Mock()
        args.validate = False
        args.list_rules = True
        args.output = 'table'

        mock_config = Mock()
        mock_config.get_rules.return_value = [
            {'name': 'Enabled Rule', 'enabled': True, 'conditions': {}, 'actions': []},
            {'name': 'Disabled Rule', 'enabled': False, 'conditions': {}, 'actions': []},
        ]

        handle_utility_args(args, mock_config)

        # Should process both rules, marking enabled/disabled correctly
        _, kwargs = mock_print_table.call_args
        assert kwargs['rows'][0][1] == '✓'
        assert kwargs['rows'][1][1] == '✗'

    @patch('qbt_rules.cli_ui.print_table')
    def test_list_rules_with_context_filter(self, mock_print_table):
        """list_rules shows trigger filter."""
        args = Mock()
        args.validate = False
        args.list_rules = True
        args.output = 'table'

        mock_config = Mock()
        mock_config.get_rules.return_value = [
            {'name': 'Single Context', 'enabled': True, 'context': 'adhoc-run', 'conditions': {}, 'actions': []},
            {'name': 'Multi Context', 'enabled': True, 'context': ['adhoc-run', 'weekly-cleanup'], 'conditions': {}, 'actions': []},
            {'name': 'No Context', 'enabled': True, 'conditions': {}, 'actions': []},
        ]

        handle_utility_args(args, mock_config)

        # Single context passes through; list contexts join with commas;
        # missing context defaults to 'any'
        _, kwargs = mock_print_table.call_args
        assert kwargs['rows'][0][3] == 'adhoc-run'
        assert kwargs['rows'][1][3] == 'adhoc-run,weekly-cleanup'
        assert kwargs['rows'][2][3] == 'any'

    @patch('qbt_rules.cli_ui.print_table')
    def test_list_rules_with_rule_missing_id(self, mock_print_table):
        """list_rules handles a rule dict with no 'id' gracefully (defensive
        fallback here -- real Config loading requires 'id', see config.py)."""
        args = Mock()
        args.validate = False
        args.list_rules = True
        args.output = 'table'

        mock_config = Mock()
        mock_config.get_rules.return_value = [
            {'enabled': True, 'conditions': {}, 'actions': []},  # No id field
        ]

        handle_utility_args(args, mock_config)

        # Should handle the missing id gracefully, defaulting to 'unnamed'
        _, kwargs = mock_print_table.call_args
        assert kwargs['rows'][0][4] == 'unnamed'

    @patch('qbt_rules.cli_ui.print_message')
    def test_list_rules_with_empty_list(self, mock_print_message):
        """list_rules handles empty rules list."""
        args = Mock()
        args.validate = False
        args.list_rules = True
        args.output = 'table'

        mock_config = Mock()
        mock_config.get_rules.return_value = []

        handle_utility_args(args, mock_config)

        # Should print an empty-state message, not a table
        mock_print_message.assert_called_once_with("No rules defined in rules.yml")

    @patch('qbt_rules.cli_ui.print_json')
    def test_list_rules_as_json(self, mock_print_json):
        """list_rules prints raw rule dicts as JSON when --output json is set."""
        args = Mock()
        args.validate = False
        args.list_rules = True
        args.output = 'json'

        rules = [{'name': 'Test Rule', 'enabled': True, 'conditions': {}, 'actions': []}]
        mock_config = Mock()
        mock_config.get_rules.return_value = rules

        handle_utility_args(args, mock_config)

        mock_print_json.assert_called_once_with(rules)

    def test_validate_warns_on_rule_with_no_conditions(self, caplog):
        """Validate warns when a rule has no conditions."""
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_qbittorrent_config.return_value = {
            'host': 'localhost',
            'user': 'admin',
            'pass': 'admin'
        }
        mock_config.get_rules.return_value = [
            {
                'id': 'NoConditionsRule',
                'conditions': [],
                'actions': [{'type': 'set_category', 'params': {'category': 'test'}}]
            }
        ]

        result = handle_utility_args(args, mock_config)

        # Check that warning was logged
        assert any("Empty conditions" in record.message for record in caplog.records)
        assert result == 0

    def test_validate_succeeds_with_complete_rule(self):
        """Validate succeeds for a rule with both conditions and actions (covers line 248)."""
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_qbittorrent_config.return_value = {
            'host': 'localhost',
            'user': 'admin',
            'pass': 'admin'
        }
        mock_config.get_rules.return_value = [
            {
                'name': 'CompleteRule',
                'conditions': {'all': [{'field': 'info.ratio', 'operator': '>=', 'value': 1.0}]},
                'actions': [{'type': 'set_category', 'params': {'category': 'test'}}]
            }
        ]

        result = handle_utility_args(args, mock_config)

        # This test primarily ensures line 248 is covered (success logging path)
        assert result == 0

    def test_validate_handles_exception(self):
        """Validate handles exceptions gracefully."""
        args = Mock()
        args.validate = True
        args.list_rules = False

        mock_config = Mock()
        mock_config.get_qbittorrent.side_effect = Exception("Config error")

        # Should not raise, just log error
        result = handle_utility_args(args, mock_config)

        assert result == 1
