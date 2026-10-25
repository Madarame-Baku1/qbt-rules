"""Comprehensive tests for config.py - Configuration loading and management."""

import pytest
import os
import logging
import tempfile
from pathlib import Path
from unittest.mock import patch
from qbt_rules.config import (
    expand_env_vars,
    load_yaml_file,
    Config,
    load_config,
    get_nested_config,
    parse_bool,
    parse_int,
    parse_duration,
    resolve_config,
    copy_default_if_missing,
    DEFAULT_CONFIG_SHARE_PATH,
)
from qbt_rules.errors import ConfigurationError


# ============================================================================
# get_nested_config()
# ============================================================================

class TestGetNestedConfig:
    """Test get_nested_config() function."""

    def test_simple_key(self):
        """Get value from simple key."""
        config = {'name': 'test', 'value': 42}
        assert get_nested_config(config, 'name') == 'test'
        assert get_nested_config(config, 'value') == 42

    def test_nested_key(self):
        """Get value from nested key."""
        config = {
            'server': {
                'host': 'localhost',
                'port': 8080
            }
        }
        assert get_nested_config(config, 'server.host') == 'localhost'
        assert get_nested_config(config, 'server.port') == 8080

    def test_deeply_nested_key(self):
        """Get value from deeply nested key."""
        config = {
            'db': {
                'connection': {
                    'host': '127.0.0.1',
                    'port': 5432
                }
            }
        }
        assert get_nested_config(config, 'db.connection.host') == '127.0.0.1'
        assert get_nested_config(config, 'db.connection.port') == 5432

    def test_missing_key_returns_none(self):
        """Missing key returns None."""
        config = {'name': 'test'}
        assert get_nested_config(config, 'missing') is None
        assert get_nested_config(config, 'name.nested') is None

    def test_missing_nested_key_returns_none(self):
        """Missing nested key returns None."""
        config = {'server': {'host': 'localhost'}}
        assert get_nested_config(config, 'server.missing') is None
        assert get_nested_config(config, 'missing.host') is None

    def test_non_dict_value_returns_none(self):
        """Non-dict value in path returns None."""
        config = {'value': 'string'}
        assert get_nested_config(config, 'value.nested') is None


# ============================================================================
# parse_bool()
# ============================================================================

class TestParseBool:
    """Test parse_bool() function."""

    def test_none_returns_false(self):
        """None returns False."""
        assert parse_bool(None) is False

    def test_bool_true(self):
        """Boolean True returns True."""
        assert parse_bool(True) is True

    def test_bool_false(self):
        """Boolean False returns False."""
        assert parse_bool(False) is False

    def test_int_zero(self):
        """Integer 0 returns False."""
        assert parse_bool(0) is False

    def test_int_non_zero(self):
        """Non-zero integers return True."""
        assert parse_bool(1) is True
        assert parse_bool(42) is True
        assert parse_bool(-1) is True

    def test_string_true_values(self):
        """String 'true' variants return True."""
        assert parse_bool('true') is True
        assert parse_bool('True') is True
        assert parse_bool('TRUE') is True
        assert parse_bool('1') is True
        assert parse_bool('yes') is True
        assert parse_bool('YES') is True
        assert parse_bool('on') is True
        assert parse_bool('ON') is True

    def test_string_false_values(self):
        """Other strings return False."""
        assert parse_bool('false') is False
        assert parse_bool('0') is False
        assert parse_bool('no') is False
        assert parse_bool('off') is False
        assert parse_bool('random') is False

    def test_other_types(self):
        """Other types use bool() conversion."""
        assert parse_bool([1, 2, 3]) is True  # Non-empty list
        assert parse_bool([]) is False  # Empty list
        assert parse_bool({'key': 'value'}) is True  # Non-empty dict
        assert parse_bool({}) is False  # Empty dict


# ============================================================================
# parse_int()
# ============================================================================

class TestParseInt:
    """Test parse_int() function."""

    def test_none_returns_default(self):
        """None returns default value."""
        assert parse_int(None, default=42) == 42
        assert parse_int(None, default=0) == 0

    def test_int_returns_int(self):
        """Integer returns itself."""
        assert parse_int(42) == 42
        assert parse_int(0) == 0
        assert parse_int(-10) == -10

    def test_string_int(self):
        """String integer parses correctly."""
        assert parse_int('42') == 42
        assert parse_int('0') == 0
        assert parse_int('-10') == -10

    def test_string_invalid_returns_default(self):
        """Invalid string returns default."""
        assert parse_int('not_a_number', default=10) == 10
        assert parse_int('42.5', default=0) == 0
        assert parse_int('', default=99) == 99

    def test_other_types_convertible(self):
        """Other types that can convert to int."""
        assert parse_int(42.0) == 42
        assert parse_int(42.9) == 42

    def test_other_types_not_convertible(self):
        """Other types that can't convert return default."""
        assert parse_int([1, 2, 3], default=10) == 10
        assert parse_int({'key': 'value'}, default=20) == 20


# ============================================================================
# parse_duration()
# ============================================================================

class TestParseDuration:
    """Test parse_duration() function."""

    def test_int_seconds_to_days(self):
        """Integer seconds convert to days."""
        assert parse_duration(86400) == '1d'  # 1 day
        assert parse_duration(172800) == '2d'  # 2 days
        assert parse_duration(604800) == '7d'  # 7 days
        assert parse_duration(43200) == '0d'  # Half day rounds down

    def test_string_with_days_format(self):
        """String '30 days' format converts to '30d'."""
        assert parse_duration('30 days') == '30d'
        assert parse_duration('7 days') == '7d'
        assert parse_duration('1 day') == '1d'

    def test_string_already_formatted(self):
        """String already in format returns unchanged."""
        assert parse_duration('7d') == '7d'
        assert parse_duration('2w') == '2w'
        assert parse_duration('30d') == '30d'

    def test_string_with_whitespace(self):
        """String with whitespace is stripped."""
        assert parse_duration('  7d  ') == '7d'
        assert parse_duration('2w ') == '2w'

    def test_other_types_to_string(self):
        """Other types convert to string."""
        assert parse_duration(42.5) == '42.5'


# ============================================================================
# resolve_config()
# ============================================================================

class TestResolveConfig:
    """Test resolve_config() function."""

    def test_cli_value_highest_priority(self):
        """CLI value takes highest priority."""
        config = {'key': 'config_value'}
        with patch.dict(os.environ, {'ENV_VAR': 'env_value'}):
            result = resolve_config(
                cli_value='cli_value',
                env_var='ENV_VAR',
                config=config,
                config_key='key',
                default='default_value'
            )
            assert result == 'cli_value'

    def test_env_file_second_priority(self, tmp_path):
        """_FILE environment variable has second priority."""
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("file_secret")

        config = {'key': 'config_value'}
        with patch.dict(os.environ, {
            'ENV_VAR_FILE': str(secret_file),
            'ENV_VAR': 'env_value'
        }):
            result = resolve_config(
                cli_value=None,
                env_var='ENV_VAR',
                config=config,
                config_key='key',
                default='default_value'
            )
            assert result == 'file_secret'

    def test_env_file_not_found(self, tmp_path):
        """_FILE with missing file falls through to next priority."""
        missing_file = tmp_path / "missing.txt"

        with patch.dict(os.environ, {
            'ENV_VAR_FILE': str(missing_file),
            'ENV_VAR': 'env_value'
        }):
            result = resolve_config(
                cli_value=None,
                env_var='ENV_VAR',
                config=None,
                config_key='key',
                default='default_value'
            )
            assert result == 'env_value'

    def test_env_file_permission_error(self, tmp_path, mocker):
        """_FILE with permission error falls through."""
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("file_secret")
        secret_file.chmod(0o000)  # Remove all permissions

        with patch.dict(os.environ, {
            'ENV_VAR_FILE': str(secret_file),
            'ENV_VAR': 'env_value'
        }):
            result = resolve_config(
                cli_value=None,
                env_var='ENV_VAR',
                config=None,
                config_key='key',
                default='default_value'
            )
            assert result == 'env_value'

        # Restore permissions for cleanup
        secret_file.chmod(0o644)

    def test_env_file_generic_error(self, tmp_path, mocker):
        """_FILE with generic read error falls through."""
        secret_file = tmp_path / "secret.txt"

        # Mock open to raise a generic exception
        with patch.dict(os.environ, {'ENV_VAR_FILE': str(secret_file)}):
            with patch('builtins.open', side_effect=IOError("Generic error")):
                result = resolve_config(
                    cli_value=None,
                    env_var='ENV_VAR',
                    config={'key': 'config_value'},
                    config_key='key',
                    default='default_value'
                )
                assert result == 'config_value'

    def test_direct_env_var_third_priority(self):
        """Direct environment variable has third priority."""
        config = {'key': 'config_value'}
        with patch.dict(os.environ, {'ENV_VAR': 'env_value'}):
            result = resolve_config(
                cli_value=None,
                env_var='ENV_VAR',
                config=config,
                config_key='key',
                default='default_value'
            )
            assert result == 'env_value'

    def test_config_file_fourth_priority(self):
        """Config file value has fourth priority."""
        config = {'key': 'config_value'}
        result = resolve_config(
            cli_value=None,
            env_var='ENV_VAR',
            config=config,
            config_key='key',
            default='default_value'
        )
        assert result == 'config_value'

    def test_nested_config_key(self):
        """Nested config key uses get_nested_config."""
        config = {'server': {'host': 'localhost'}}
        result = resolve_config(
            cli_value=None,
            env_var='ENV_VAR',
            config=config,
            config_key='server.host',
            default='default_value'
        )
        assert result == 'localhost'

    def test_default_value_lowest_priority(self):
        """Default value used when nothing else matches."""
        result = resolve_config(
            cli_value=None,
            env_var='ENV_VAR',
            config=None,
            config_key='key',
            default='default_value'
        )
        assert result == 'default_value'


# ============================================================================
# expand_env_vars()
# ============================================================================

class TestExpandEnvVars:
    """Test expand_env_vars() function."""

    def test_simple_env_var(self):
        """Expand simple environment variable."""
        with patch.dict(os.environ, {'TEST_VAR': 'hello'}):
            result = expand_env_vars('${TEST_VAR}')
            assert result == 'hello'

    def test_env_var_with_default(self):
        """Use default when env var not set."""
        with patch.dict(os.environ, {}, clear=True):
            result = expand_env_vars('${MISSING_VAR:-default_value}')
            assert result == 'default_value'

    def test_env_var_with_default_override(self):
        """Env var overrides default."""
        with patch.dict(os.environ, {'PRESENT_VAR': 'actual'}):
            result = expand_env_vars('${PRESENT_VAR:-default}')
            assert result == 'actual'

    def test_multiple_vars_in_string(self):
        """Multiple env vars in one string."""
        with patch.dict(os.environ, {'VAR1': 'foo', 'VAR2': 'bar'}):
            result = expand_env_vars('${VAR1}/${VAR2}')
            assert result == 'foo/bar'

    def test_dict_expansion(self):
        """Recursively expand dict values."""
        with patch.dict(os.environ, {'HOST': 'localhost', 'PORT': '8080'}):
            data = {'server': {'host': '${HOST}', 'port': '${PORT}'}}
            result = expand_env_vars(data)
            assert result == {'server': {'host': 'localhost', 'port': '8080'}}

    def test_list_expansion(self):
        """Recursively expand list values."""
        with patch.dict(os.environ, {'VAR': 'value'}):
            data = ['${VAR}', 'plain', '${VAR}']
            result = expand_env_vars(data)
            assert result == ['value', 'plain', 'value']

    def test_nested_structures(self):
        """Expand nested dicts and lists."""
        with patch.dict(os.environ, {'VAR': 'test'}):
            data = {'outer': {'inner': ['${VAR}', '${VAR:-default}']}}
            result = expand_env_vars(data)
            assert result == {'outer': {'inner': ['test', 'test']}}

    def test_primitive_types_unchanged(self):
        """Primitive types are unchanged."""
        assert expand_env_vars(42) == 42
        assert expand_env_vars(True) is True
        assert expand_env_vars(None) is None
        assert expand_env_vars(3.14) == 3.14

    def test_empty_default(self):
        """Empty default value works."""
        with patch.dict(os.environ, {}, clear=True):
            result = expand_env_vars('${MISSING:-}')
            assert result == ''

    def test_no_expansion_needed(self):
        """String without variables is unchanged."""
        result = expand_env_vars('plain string')
        assert result == 'plain string'


# ============================================================================
# load_yaml_file()
# ============================================================================

class TestLoadYamlFile:
    """Test load_yaml_file() function."""

    def test_valid_yaml(self, tmp_path):
        """Load valid YAML file."""
        yaml_file = tmp_path / "test.yml"
        yaml_file.write_text("key: value\nnumber: 42")

        result = load_yaml_file(yaml_file)
        assert result == {'key': 'value', 'number': 42}

    def test_nested_yaml(self, tmp_path):
        """Load nested YAML structure."""
        yaml_file = tmp_path / "nested.yml"
        yaml_file.write_text("""
outer:
  inner:
    key: value
  list:
    - item1
    - item2
""")

        result = load_yaml_file(yaml_file)
        assert result['outer']['inner']['key'] == 'value'
        assert result['outer']['list'] == ['item1', 'item2']

    def test_file_not_exists(self, tmp_path):
        """Raise error when file doesn't exist."""
        yaml_file = tmp_path / "nonexistent.yml"

        with pytest.raises(ConfigurationError) as exc_info:
            load_yaml_file(yaml_file)
        assert "does not exist" in str(exc_info.value)

    def test_empty_file(self, tmp_path):
        """Raise error when file is empty."""
        yaml_file = tmp_path / "empty.yml"
        yaml_file.write_text("")

        with pytest.raises(ConfigurationError) as exc_info:
            load_yaml_file(yaml_file)
        assert "empty" in str(exc_info.value).lower()

    def test_invalid_yaml_syntax(self, tmp_path):
        """Raise error for invalid YAML syntax."""
        yaml_file = tmp_path / "invalid.yml"
        yaml_file.write_text("key: value\n  invalid indentation\n  : bad")

        with pytest.raises(ConfigurationError) as exc_info:
            load_yaml_file(yaml_file)
        assert "YAML syntax" in str(exc_info.value)

    def test_permission_denied(self, tmp_path):
        """Raise error when permission denied."""
        yaml_file = tmp_path / "protected.yml"
        yaml_file.write_text("key: value")
        yaml_file.chmod(0o000)  # Remove all permissions

        try:
            with pytest.raises(ConfigurationError) as exc_info:
                load_yaml_file(yaml_file)
            assert "Permission denied" in str(exc_info.value)
        finally:
            yaml_file.chmod(0o644)  # Restore permissions for cleanup

    def test_yaml_with_lists(self, tmp_path):
        """Load YAML with lists."""
        yaml_file = tmp_path / "lists.yml"
        yaml_file.write_text("""
items:
  - name: item1
    value: 1
  - name: item2
    value: 2
""")

        result = load_yaml_file(yaml_file)
        assert len(result['items']) == 2
        assert result['items'][0]['name'] == 'item1'

    def test_yaml_with_multiline_strings(self, tmp_path):
        """Load YAML with multiline strings."""
        yaml_file = tmp_path / "multiline.yml"
        yaml_file.write_text("""
description: |
  This is a
  multiline
  string
""")

        result = load_yaml_file(yaml_file)
        assert 'This is a' in result['description']
        assert 'multiline' in result['description']


# ============================================================================
# copy_default_if_missing()
# ============================================================================

class TestCopyDefaultIfMissing:
    """Test copy_default_if_missing() function."""

    def test_copies_when_target_missing(self, tmp_path, mocker):
        """Should copy default file when target doesn't exist."""
        # Setup
        target_path = tmp_path / 'config' / 'config.yml'
        default_filename = 'config.default.yml'

        # Create mock default file
        mock_default_dir = tmp_path / 'usr_share'
        mock_default_dir.mkdir()
        mock_default_file = mock_default_dir / default_filename
        mock_default_file.write_text('default: config')

        # Mock DEFAULT_CONFIG_SHARE_PATH
        mocker.patch('qbt_rules.config.DEFAULT_CONFIG_SHARE_PATH', mock_default_dir)

        # Execute
        result = copy_default_if_missing(target_path, default_filename)

        # Verify
        assert result is True
        assert target_path.exists()
        assert target_path.read_text() == 'default: config'

    def test_skips_when_target_exists(self, tmp_path, mocker):
        """Should not copy when target already exists."""
        # Setup
        target_path = tmp_path / 'config.yml'
        target_path.write_text('existing: config')
        default_filename = 'config.default.yml'

        # Create mock default file
        mock_default_dir = tmp_path / 'usr_share'
        mock_default_dir.mkdir()
        mock_default_file = mock_default_dir / default_filename
        mock_default_file.write_text('default: config')

        # Mock DEFAULT_CONFIG_SHARE_PATH
        mocker.patch('qbt_rules.config.DEFAULT_CONFIG_SHARE_PATH', mock_default_dir)

        # Execute
        result = copy_default_if_missing(target_path, default_filename)

        # Verify
        assert result is False
        assert target_path.read_text() == 'existing: config'

    def test_returns_false_when_default_missing(self, tmp_path, mocker):
        """Should return False when default file doesn't exist."""
        # Setup
        target_path = tmp_path / 'config.yml'
        default_filename = 'config.default.yml'

        # Mock DEFAULT_CONFIG_SHARE_PATH to non-existent directory
        mock_default_dir = tmp_path / 'usr_share'
        mocker.patch('qbt_rules.config.DEFAULT_CONFIG_SHARE_PATH', mock_default_dir)

        # Execute
        result = copy_default_if_missing(target_path, default_filename)

        # Verify
        assert result is False
        assert not target_path.exists()

    def test_creates_parent_directory(self, tmp_path, mocker):
        """Should create parent directory if it doesn't exist."""
        # Setup
        target_path = tmp_path / 'config' / 'subdir' / 'config.yml'
        default_filename = 'config.default.yml'

        # Create mock default file
        mock_default_dir = tmp_path / 'usr_share'
        mock_default_dir.mkdir()
        mock_default_file = mock_default_dir / default_filename
        mock_default_file.write_text('default: config')

        # Mock DEFAULT_CONFIG_SHARE_PATH
        mocker.patch('qbt_rules.config.DEFAULT_CONFIG_SHARE_PATH', mock_default_dir)

        # Execute
        result = copy_default_if_missing(target_path, default_filename)

        # Verify
        assert result is True
        assert target_path.parent.exists()
        assert target_path.exists()

    def test_handles_copy_failure_gracefully(self, tmp_path, mocker):
        """Should handle copy failures gracefully and return False."""
        # Setup
        target_path = tmp_path / 'config.yml'
        default_filename = 'config.default.yml'

        # Create mock default file
        mock_default_dir = tmp_path / 'usr_share'
        mock_default_dir.mkdir()
        mock_default_file = mock_default_dir / default_filename
        mock_default_file.write_text('default: config')

        # Mock DEFAULT_CONFIG_SHARE_PATH
        mocker.patch('qbt_rules.config.DEFAULT_CONFIG_SHARE_PATH', mock_default_dir)

        # Mock shutil.copy2 to raise exception
        mocker.patch('qbt_rules.config.shutil.copy2', side_effect=PermissionError('Test error'))

        # Execute
        result = copy_default_if_missing(target_path, default_filename)

        # Verify
        assert result is False
        assert not target_path.exists()


# ============================================================================
# Config class
# ============================================================================

class TestConfig:
    """Test Config class."""

    def test_init_with_custom_dir(self, tmp_config_dir):
        """Initialize with custom config directory."""
        config = Config(tmp_config_dir)
        assert config.config_dir == tmp_config_dir
        assert config.config_file == tmp_config_dir / 'config.yml'
        assert config.rules_file == tmp_config_dir / 'rules.yml'

    def test_init_with_env_var(self, tmp_config_dir):
        """Initialize with CONFIG_DIR environment variable."""
        with patch.dict(os.environ, {'CONFIG_DIR': str(tmp_config_dir)}):
            config = Config()
            assert config.config_dir == tmp_config_dir

    def test_load_config_success(self, tmp_config_dir):
        """Successfully load config.yml."""
        config = Config(tmp_config_dir)
        assert config.config is not None
        assert 'qbittorrent' in config.config

    def test_load_rules_success(self, tmp_config_dir):
        """Successfully load rules.yml."""
        config = Config(tmp_config_dir)
        assert config.rules is not None
        assert isinstance(config.rules, list)
        assert len(config.rules) > 0

    def test_rules_version_defaults_to_1_when_absent(self, tmp_config_dir):
        """rules.yml with no top-level 'version' key defaults to 1, not None/error."""
        config = Config(tmp_config_dir)
        assert config.rules_version == 1

    def test_rules_version_reads_explicit_value(self, tmp_path):
        """An explicit top-level 'version' key in rules.yml is read through."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text("qbittorrent:\n  host: http://localhost:8080\n")
        (config_dir / "rules.yml").write_text("version: 2\nrules: []\n")

        config = Config(config_dir)
        assert config.rules_version == 2

    def test_config_version_defaults_to_1_when_absent(self, tmp_config_dir):
        """config.yml with no top-level 'version' key defaults to 1 via config.get()."""
        config = Config(tmp_config_dir)
        assert config.get('version', 1) == 1

    def test_missing_config_file(self, tmp_path):
        """Raise error when config.yml missing."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        with pytest.raises(ConfigurationError):
            Config(empty_dir)

    def test_invalid_rules_not_list(self, tmp_path):
        """Raise error when rules is not a list."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("qbittorrent:\n  host: localhost")
        (config_dir / "rules.yml").write_text("rules: not_a_list")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "must be a list" in str(exc_info.value)

    def test_rule_missing_id_raises_error(self, tmp_path):
        """A rule with no 'id' fails to load -- 'id' is required (v0.7+), 'name' is not read at all."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("qbittorrent:\n  host: localhost")
        (config_dir / "rules.yml").write_text("""
rules:
  - name: "Has a name but no id"
    conditions:
      all: []
    actions: []
""")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "missing required field: 'id'" in str(exc_info.value)

    def test_rule_empty_id_raises_error(self, tmp_path):
        """An empty-string id is rejected, same as a missing one."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("qbittorrent:\n  host: localhost")
        (config_dir / "rules.yml").write_text("""
rules:
  - id: ""
    conditions:
      all: []
    actions: []
""")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "invalid 'id'" in str(exc_info.value)

    def test_rule_non_string_id_raises_error(self, tmp_path):
        """A non-string id (e.g. a bare number) is rejected."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("qbittorrent:\n  host: localhost")
        (config_dir / "rules.yml").write_text("""
rules:
  - id: 42
    conditions:
      all: []
    actions: []
""")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "invalid 'id'" in str(exc_info.value)

    def test_duplicate_rule_id_raises_error(self, tmp_path):
        """Two rules sharing an id fail to load -- silently merging their
        by_rule stats/history under one key would be worse than an error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("qbittorrent:\n  host: localhost")
        (config_dir / "rules.yml").write_text("""
rules:
  - id: dupe
    conditions:
      all: []
    actions: []
  - id: dupe
    conditions:
      all: []
    actions: []
""")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "Duplicate rule id: 'dupe'" in str(exc_info.value)

    def test_schedule_defaults_to_empty_list(self, tmp_config_dir):
        """schedule defaults to an empty list when config.yml has no schedule: section."""
        config = Config(tmp_config_dir)
        assert config.schedule == []

    def test_schedule_loads_valid_entries(self, tmp_path):
        """Successfully load a valid schedule: section."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text(
            "qbittorrent:\n  host: localhost\n"
            "schedule:\n"
            "  - cron: '*/30 * * * *'\n"
            "    context: cron\n"
            "  - cron: '0 3 * * *'\n"
            "    context: nightly\n"
        )
        (config_dir / "rules.yml").write_text("rules: []")

        config = Config(config_dir)
        assert len(config.schedule) == 2
        assert config.schedule[0] == {'cron': '*/30 * * * *', 'context': 'cron'}
        assert config.schedule[1] == {'cron': '0 3 * * *', 'context': 'nightly'}

    def test_schedule_not_a_list_raises(self, tmp_path):
        """Raise error when schedule is not a list."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("qbittorrent:\n  host: localhost\nschedule: not_a_list")
        (config_dir / "rules.yml").write_text("rules: []")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "'schedule' must be a list" in str(exc_info.value)

    def test_schedule_entry_not_a_dict_raises(self, tmp_path):
        """Raise error when a schedule entry isn't a dictionary."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text(
            "qbittorrent:\n  host: localhost\nschedule:\n  - 'just a string'\n"
        )
        (config_dir / "rules.yml").write_text("rules: []")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "Schedule entry #1 must be a dictionary" in str(exc_info.value)

    def test_schedule_entry_missing_cron_raises(self, tmp_path):
        """Raise error when a schedule entry is missing 'cron'."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text(
            "qbittorrent:\n  host: localhost\nschedule:\n  - context: cron\n"
        )
        (config_dir / "rules.yml").write_text("rules: []")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "Schedule entry #1 missing required field: 'cron'" in str(exc_info.value)

    def test_schedule_entry_missing_context_raises(self, tmp_path):
        """Raise error when a schedule entry is missing 'context'."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text(
            "qbittorrent:\n  host: localhost\nschedule:\n  - cron: '* * * * *'\n"
        )
        (config_dir / "rules.yml").write_text("rules: []")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "Schedule entry #1 missing required field: 'context'" in str(exc_info.value)

    def test_schedule_entry_invalid_cron_raises(self, tmp_path):
        """Raise error when a schedule entry's cron expression is invalid."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text(
            "qbittorrent:\n  host: localhost\n"
            "schedule:\n  - cron: 'not a cron expression'\n    context: cron\n"
        )
        (config_dir / "rules.yml").write_text("rules: []")

        with pytest.raises(ConfigurationError) as exc_info:
            Config(config_dir)
        assert "invalid cron expression" in str(exc_info.value)

    def test_schedule_env_vars_not_set_falls_back_to_config_yml(self, tmp_path):
        """No QBT_RULES_SCHEDULE_* env vars set -- config.yml's schedule: list is used as-is."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text(
            "qbittorrent:\n  host: localhost\n"
            "schedule:\n  - cron: '*/30 * * * *'\n    context: cron\n"
        )
        (config_dir / "rules.yml").write_text("rules: []")

        config = Config(config_dir)
        assert config.schedule == [{'cron': '*/30 * * * *', 'context': 'cron'}]

    def test_schedule_env_vars_contiguous_entries_loaded(self, tmp_config_dir):
        """QBT_RULES_SCHEDULE_0_*/_1_* both set -- both entries are loaded, in order."""
        env = {
            'QBT_RULES_SCHEDULE_0_CRON': '*/30 * * * *',
            'QBT_RULES_SCHEDULE_0_CONTEXT': 'cron',
            'QBT_RULES_SCHEDULE_1_CRON': '0 3 * * *',
            'QBT_RULES_SCHEDULE_1_CONTEXT': 'nightly',
        }
        with patch.dict(os.environ, env):
            config = Config(tmp_config_dir)

        assert config.schedule == [
            {'cron': '*/30 * * * *', 'context': 'cron'},
            {'cron': '0 3 * * *', 'context': 'nightly'},
        ]

    def test_schedule_env_vars_gap_stops_enumeration(self, tmp_config_dir):
        """A missing index (here, index 1) stops enumeration -- index 2 is never read."""
        env = {
            'QBT_RULES_SCHEDULE_0_CRON': '*/30 * * * *',
            'QBT_RULES_SCHEDULE_0_CONTEXT': 'cron',
            'QBT_RULES_SCHEDULE_2_CRON': '0 3 * * *',
            'QBT_RULES_SCHEDULE_2_CONTEXT': 'nightly',
        }
        with patch.dict(os.environ, env):
            config = Config(tmp_config_dir)

        assert config.schedule == [{'cron': '*/30 * * * *', 'context': 'cron'}]

    def test_schedule_env_var_cron_without_context_raises(self, tmp_config_dir):
        """QBT_RULES_SCHEDULE_0_CRON set without the matching _0_CONTEXT is a config error."""
        with patch.dict(os.environ, {'QBT_RULES_SCHEDULE_0_CRON': '*/30 * * * *'}):
            with pytest.raises(ConfigurationError) as exc_info:
                Config(tmp_config_dir)
        assert "QBT_RULES_SCHEDULE_0_CRON is set but QBT_RULES_SCHEDULE_0_CONTEXT is missing" in str(exc_info.value)

    def test_schedule_env_vars_replace_config_yml_entirely(self, tmp_path):
        """Env-defined schedule entries win outright over config.yml's list, not merged."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text(
            "qbittorrent:\n  host: localhost\n"
            "schedule:\n  - cron: '0 0 * * *'\n    context: from-config-yml\n"
        )
        (config_dir / "rules.yml").write_text("rules: []")

        env = {
            'QBT_RULES_SCHEDULE_0_CRON': '*/30 * * * *',
            'QBT_RULES_SCHEDULE_0_CONTEXT': 'from-env',
        }
        with patch.dict(os.environ, env):
            config = Config(config_dir)

        assert config.schedule == [{'cron': '*/30 * * * *', 'context': 'from-env'}]

    def test_schedule_env_var_supports_file_variant(self, tmp_config_dir, tmp_path):
        """QBT_RULES_SCHEDULE_0_CRON_FILE reads the cron expression from a file."""
        cron_file = tmp_path / "cron_secret"
        cron_file.write_text('*/15 * * * *\n')

        env = {
            'QBT_RULES_SCHEDULE_0_CRON_FILE': str(cron_file),
            'QBT_RULES_SCHEDULE_0_CONTEXT': 'cron',
        }
        with patch.dict(os.environ, env):
            config = Config(tmp_config_dir)

        assert config.schedule == [{'cron': '*/15 * * * *', 'context': 'cron'}]

    def test_get_simple_key(self, tmp_config_dir):
        """Get simple configuration key."""
        config = Config(tmp_config_dir)
        # Based on tmp_config_dir fixture
        assert config.get('qbittorrent.host') == 'http://localhost:8080'

    def test_get_nested_key(self, tmp_config_dir):
        """Get nested configuration key."""
        config = Config(tmp_config_dir)
        assert config.get('qbittorrent.username') == 'admin'

    def test_get_with_default(self, tmp_config_dir):
        """Get with default value for missing key."""
        config = Config(tmp_config_dir)
        assert config.get('nonexistent.key', 'default') == 'default'

    def test_get_missing_key_no_default(self, tmp_config_dir):
        """Get missing key returns None without default."""
        config = Config(tmp_config_dir)
        assert config.get('nonexistent.key') is None

    def test_is_dry_run_false_by_default(self, tmp_config_dir):
        """Dry run is false by default."""
        config = Config(tmp_config_dir)
        assert config.is_dry_run() is False

    def test_is_dry_run_from_config(self, tmp_path):
        """Read dry run from config file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("""
qbittorrent:
  host: localhost
engine:
  dry_run: true
""")
        (config_dir / "rules.yml").write_text("rules: []")

        config = Config(config_dir)
        assert config.is_dry_run() is True

    def test_is_dry_run_from_env_var(self, tmp_config_dir):
        """Environment variable overrides config file."""
        with patch.dict(os.environ, {'QBT_RULES_DRY_RUN': 'true'}):
            config = Config(tmp_config_dir)
            assert config.is_dry_run() is True

    def test_is_dry_run_env_var_variations(self, tmp_config_dir):
        """Test various env var values for dry run."""
        for value in ['true', '1', 'yes', 'on', 'TRUE', 'YES']:
            with patch.dict(os.environ, {'QBT_RULES_DRY_RUN': value}):
                config = Config(tmp_config_dir)
                assert config.is_dry_run() is True, f"Failed for value: {value}"

        for value in ['false', '0', 'no', 'off', 'FALSE', 'NO']:
            with patch.dict(os.environ, {'QBT_RULES_DRY_RUN': value}):
                config = Config(tmp_config_dir)
                assert config.is_dry_run() is False, f"Failed for value: {value}"

    def test_is_dry_run_string_value_from_config(self, tmp_path):
        """Dry run handles string 'true' from YAML config (covers line 194)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("""
qbittorrent:
  host: localhost
engine:
  dry_run: "true"
""")
        (config_dir / "rules.yml").write_text("rules: []")

        config = Config(config_dir)
        assert config.is_dry_run() is True

    def test_get_rules(self, tmp_config_dir):
        """Get list of rules."""
        config = Config(tmp_config_dir)
        rules = config.get_rules()

        assert isinstance(rules, list)
        assert len(rules) > 0
        assert 'id' in rules[0]

    def test_get_rules_hot_reload_when_file_modified(self, tmp_path):
        """Should reload rules when file is modified."""
        import time

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create initial rules
        rules_file = config_dir / "rules.yml"
        rules_file.write_text("""
rules:
  - id: initial-rule
    conditions:
      all: []
    actions:
      - type: stop
""")
        (config_dir / "config.yml").write_text("qbittorrent: {host: 'http://localhost:8080'}")

        # Load config
        config = Config(config_dir)
        initial_rules = config.get_rules()
        assert len(initial_rules) == 1
        assert initial_rules[0]['id'] == "initial-rule"

        # Modify rules file (need to ensure mtime changes)
        time.sleep(0.01)  # Ensure mtime difference
        rules_file.write_text("""
rules:
  - id: updated-rule
    conditions:
      all: []
    actions:
      - type: start
  - id: new-rule
    conditions:
      all: []
    actions:
      - type: recheck
""")

        # Touch the file to update mtime
        rules_file.touch()

        # Get rules again - should reload
        updated_rules = config.get_rules()
        assert len(updated_rules) == 2
        assert updated_rules[0]['id'] == "updated-rule"
        assert updated_rules[1]['id'] == "new-rule"

    def test_get_rules_skips_reload_when_file_unchanged(self, tmp_path, mocker):
        """Should not reload rules when file hasn't changed."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "rules.yml").write_text("""
rules:
  - id: test-rule
    conditions:
      all: []
    actions:
      - type: stop
""")
        (config_dir / "config.yml").write_text("qbittorrent: {host: 'http://localhost:8080'}")

        config = Config(config_dir)

        # Spy on _load_rules to track calls
        load_rules_spy = mocker.spy(config, '_load_rules')

        # First call - should load (mtime not set yet)
        config.get_rules()
        assert load_rules_spy.call_count == 1

        # Second call without file modification - should NOT reload
        config.get_rules()
        # Should still be 1 because file hasn't changed
        assert load_rules_spy.call_count == 1

    def test_get_rules_keeps_old_rules_on_reload_error(self, tmp_path, caplog):
        """Should keep existing rules when reload fails."""
        import time

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Create valid initial rules
        rules_file = config_dir / "rules.yml"
        rules_file.write_text("""
rules:
  - id: good-rule
    conditions:
      all: []
    actions:
      - type: stop
""")
        (config_dir / "config.yml").write_text("qbittorrent: {host: 'http://localhost:8080'}")

        config = Config(config_dir)
        good_rules = config.get_rules()
        assert len(good_rules) == 1
        assert good_rules[0]['id'] == "good-rule"

        # Corrupt the rules file
        time.sleep(0.01)
        rules_file.write_text("rules: {invalid yaml syntax: [unclosed bracket")
        rules_file.touch()

        # Try to get rules - should keep old rules and log warning
        with caplog.at_level(logging.WARNING):
            rules_after_error = config.get_rules()

        # Should still have the good rules
        assert len(rules_after_error) == 1
        assert rules_after_error[0]['id'] == "good-rule"

        # Should have logged a warning
        assert "Failed to reload rules" in caplog.text
        assert "Using cached rules" in caplog.text

    def test_get_rules_reloads_a_file_with_an_older_mtime(self, tmp_path):
        """A restored backup (cp -p, git checkout) can carry an older mtime
        than the file it replaces -- any change on disk must reload, not
        only a newer mtime."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text("qbittorrent: {host: 'http://localhost:8080'}")
        rules_file = config_dir / "rules.yml"
        rules_file.write_text("rules:\n  - {id: current, conditions: [], actions: []}\n")

        config = Config(config_dir)
        assert config.get_rules()[0]['id'] == 'current'

        old = rules_file.stat().st_mtime - 3600
        rules_file.write_text("rules:\n  - {id: restored-backup, conditions: [], actions: []}\n")
        os.utime(rules_file, (old, old))

        assert config.get_rules()[0]['id'] == 'restored-backup'

    def test_failed_reload_keeps_raw_rules_too(self, tmp_path):
        """A reload that fails validation must not leave the new, invalid
        raw rules behind next to the old resolved ones."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text("qbittorrent: {host: 'http://localhost:8080'}")
        rules_file = config_dir / "rules.yml"
        rules_file.write_text("rules:\n  - {id: good, conditions: [], actions: []}\n")

        config = Config(config_dir)
        config.get_rules()

        rules_file.write_text("rules:\n  - {id: dup, conditions: [], actions: []}\n"
                              "  - {id: dup, conditions: [], actions: []}\n")
        os.utime(rules_file, (rules_file.stat().st_mtime + 5,) * 2)

        assert [r['id'] for r in config.get_rules()] == ['good']
        assert [r['id'] for r in config.get_rules(resolved=False)] == ['good']

    def test_reload_rules_force(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text("qbittorrent: {host: 'http://localhost:8080'}")
        (config_dir / "rules.yml").write_text("rules: []\n")

        config = Config(config_dir)
        assert config.reload_rules() is True       # first call after init
        assert config.reload_rules() is False      # unchanged
        assert config.reload_rules(force=True) is True

    def test_empty_refs_block_loads(self, tmp_path):
        """refs: with everything under it commented out parses as None"""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text("qbittorrent: {host: 'http://localhost:8080'}")
        (config_dir / "rules.yml").write_text("refs:\n  vars:\nrules:\n  - {id: a, conditions: [], actions: []}\n")

        assert Config(config_dir).get_rules()[0]['id'] == 'a'

    def test_env_var_expansion_in_config(self, tmp_path):
        """Environment variables are expanded in config."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("""
qbittorrent:
  host: ${QB_HOST:-http://localhost:8080}
  username: ${QB_USER:-admin}
""")
        (config_dir / "rules.yml").write_text("rules: []")

        with patch.dict(os.environ, {'QB_HOST': 'http://custom:9090', 'QB_USER': 'myuser'}):
            config = Config(config_dir)
            assert config.get('qbittorrent.host') == 'http://custom:9090'
            assert config.get('qbittorrent.username') == 'myuser'

    def test_env_var_expansion_with_defaults(self, tmp_path):
        """Environment variable defaults work in config."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        (config_dir / "config.yml").write_text("""
qbittorrent:
  host: ${QB_HOST:-http://localhost:8080}
""")
        (config_dir / "rules.yml").write_text("rules: []")

        with patch.dict(os.environ, {}, clear=True):
            config = Config(config_dir)
            assert config.get('qbittorrent.host') == 'http://localhost:8080'


# ============================================================================
# load_config()
# ============================================================================

class TestLoadConfig:
    """Test load_config() convenience function."""

    def test_load_config_returns_config_object(self, tmp_config_dir):
        """load_config() returns Config object."""
        config = load_config(tmp_config_dir)
        assert isinstance(config, Config)
        assert config.config_dir == tmp_config_dir

    def test_load_config_without_args(self, tmp_config_dir):
        """load_config() without args uses environment/default."""
        with patch.dict(os.environ, {'CONFIG_DIR': str(tmp_config_dir)}):
            config = load_config()
            assert isinstance(config, Config)
