"""
Configuration loader with environment variable expansion and universal _FILE support

Resolution order (highest to lowest priority):
1. CLI arguments
2. Environment variable _FILE variant (reads from file)
3. Environment variable (direct value)
4. Config file
5. Default value
"""

import os
import re
import sys
import shutil
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Union

from croniter import croniter

from qbt_rules.errors import ConfigurationError
from qbt_rules.resolver import RuleResolver
from qbt_rules.rules_validation import check_rules_structure


# Environment variable mapping
#
# Only genuine exceptions live here now. Every section resolved through
# cli.py's resolve_section_config() (server, client, queue, notifications,
# and any future section) derives its env var name mechanically as
# QBT_RULES_<SECTION>_<FIELD> and never needs an entry here -- this map
# exists only for the handful of variables that don't follow that
# convention.
ENV_VAR_MAP = {
    # Rules & queue misc
    'rules.file': 'QBT_RULES_RULES_FILE',
    'config.dir': 'QBT_RULES_CONFIG_DIR',
    'queue.cleanup_after': 'QBT_RULES_QUEUE_CLEANUP_AFTER',

    # engine.dry_run has no section prefix at all
    'engine.dry_run': 'QBT_RULES_DRY_RUN',
}

# Note: logging.* is NOT resolved through this map or resolve_section_config()
# -- cli.py's get_logging_config() hand-resolves each field directly (its CLI
# flag names, --log-level/--trace, don't match the mechanical <section>_<field>
# convention this map/helper assume). Its env vars are QBT_RULES_LOGGING_*,
# matching the convention exactly -- no exception entry needed here.

# Default config location (Linux FHS standard)
DEFAULT_CONFIG_SHARE_PATH = Path('/usr/share/qbt-rules')


def copy_default_if_missing(target_path: Path, default_filename: str) -> bool:
    """
    Copy default config from /usr/share if target doesn't exist.

    Args:
        target_path: Target file path (e.g., /config/config.yml)
        default_filename: Default filename (e.g., 'config.default.yml')

    Returns:
        True if file was copied, False otherwise
    """
    if target_path.exists():
        return False

    default_path = DEFAULT_CONFIG_SHARE_PATH / default_filename
    if not default_path.exists():
        return False

    try:
        # Ensure parent directory exists
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Copy default to target location
        shutil.copy2(default_path, target_path)
        print(f"INFO: Created {target_path} from {default_path}", file=sys.stderr)

        return True
    except Exception as e:
        print(f"WARNING: Failed to copy default config to {target_path}: {e}", file=sys.stderr)
        return False


def get_nested_config(config: Dict[str, Any], key: str, default: Any = None) -> Optional[Any]:
    """
    Get nested configuration value using dot notation

    Args:
        config: Configuration dictionary
        key: Dot-notation key (e.g., 'server.host')
        default: Value to return if the key path isn't found

    Returns:
        Configuration value or default if not found

    Examples:
        >>> config = {'server': {'host': 'localhost', 'port': 5000}}
        >>> get_nested_config(config, 'server.host')
        'localhost'
        >>> get_nested_config(config, 'server.missing')
        None
    """
    keys = key.split('.')
    value = config

    for k in keys:
        if isinstance(value, dict) and k in value:
            value = value[k]
        else:
            return default

    return value


def parse_bool(value: Any) -> bool:
    """
    Parse boolean from various formats

    Args:
        value: Value to parse (str, int, bool, None)

    Returns:
        Boolean value

    Examples:
        >>> parse_bool('true')
        True
        >>> parse_bool('1')
        True
        >>> parse_bool(0)
        False
        >>> parse_bool(None)
        False
    """
    if value is None:
        return False

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value != 0

    if isinstance(value, str):
        return value.lower() in ('true', '1', 'yes', 'on')

    return bool(value)


def parse_int(value: Any, default: int = 0) -> int:
    """
    Parse integer from various formats

    Args:
        value: Value to parse
        default: Default value if parsing fails

    Returns:
        Integer value
    """
    if value is None:
        return default

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default

    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def parse_duration(value: Union[str, int]) -> str:
    """
    Parse duration string to standardized format

    Args:
        value: Duration (e.g., '7d', '30 days', '2w', 86400)

    Returns:
        Standardized duration string

    Examples:
        >>> parse_duration('7d')
        '7d'
        >>> parse_duration('30 days')
        '30d'
        >>> parse_duration(86400)
        '1d'
    """
    if isinstance(value, int):
        # Convert seconds to days
        days = value // 86400
        return f'{days}d'

    if isinstance(value, str):
        # Already in format like '7d', '2w', etc.
        value = value.strip().lower()

        # Handle "30 days" format
        if ' day' in value:
            days = int(value.split()[0])
            return f'{days}d'

        return value

    return str(value)


def resolve_config(
    cli_value: Optional[Any],
    env_var: str,
    config: Dict[str, Any],
    config_key: str,
    default: Optional[Any] = None
) -> Any:
    """
    Universal configuration resolver with _FILE support

    Resolution order:
    1. CLI argument (if provided)
    2. Environment variable _FILE variant (reads file content)
    3. Environment variable (direct value)
    4. Config file value
    5. Default value

    ALL environment variables support _FILE variants automatically.

    Args:
        cli_value: Value from CLI argument (None if not provided)
        env_var: Environment variable name (without _FILE suffix)
        config: Loaded configuration dictionary
        config_key: Dot-notation key for config file (e.g., 'server.port')
        default: Default value if no source provides a value

    Returns:
        Resolved configuration value

    Examples:
        >>> resolve_config(
        ...     cli_value=None,
        ...     env_var='QBT_RULES_SERVER_PORT',
        ...     config={'server': {'port': 5000}},
        ...     config_key='server.port',
        ...     default=8080
        ... )
        5000

        >>> os.environ['QBT_RULES_SERVER_API_KEY_FILE'] = '/secrets/key'
        >>> # If /secrets/key contains "my-secret-key"
        >>> resolve_config(
        ...     cli_value=None,
        ...     env_var='QBT_RULES_SERVER_API_KEY',
        ...     config={},
        ...     config_key='server.api_key',
        ...     default=None
        ... )
        'my-secret-key'
    """
    # 1. CLI argument takes highest priority
    if cli_value is not None:
        return cli_value

    # 2. Check _FILE variant (universal support)
    file_var = f"{env_var}_FILE"
    if file_var in os.environ:
        file_path = os.environ[file_var]
        try:
            with open(file_path, 'r') as f:
                content = f.read().strip()
            logging.debug(f"Loaded config from file: {file_var}={file_path}")
            return content
        except FileNotFoundError:
            logging.warning(f"File not found for {file_var}: {file_path}")
        except PermissionError:
            logging.warning(f"Permission denied reading {file_var}: {file_path}")
        except Exception as e:
            logging.warning(f"Error reading {file_var} from {file_path}: {e}")

    # 3. Direct environment variable
    if env_var in os.environ:
        value = os.environ[env_var]
        logging.debug(f"Loaded config from env: {env_var}={value}")
        return value

    # 4. Config file value
    if config:
        value = get_nested_config(config, config_key)
        if value is not None:
            logging.debug(f"Loaded config from file: {config_key}={value}")
            return value

    # 5. Default value
    logging.debug(f"Using default config: {config_key}={default}")
    return default


def expand_env_vars(value: Any) -> Any:
    """
    Recursively expand environment variables in configuration values

    Supports format: ${VAR_NAME:-default_value}

    Args:
        value: Configuration value (can be str, dict, list, or primitive)

    Returns:
        Value with environment variables expanded

    Examples:
        >>> os.environ['TEST_VAR'] = 'hello'
        >>> expand_env_vars('${TEST_VAR:-default}')
        'hello'
        >>> expand_env_vars('${MISSING_VAR:-default}')
        'default'
    """
    if isinstance(value, str):
        # Pattern: ${VAR_NAME:-default} or ${VAR_NAME}
        pattern = r'\$\{([^}:]+)(?::-([^}]*))?\}'

        def replacer(match):
            var_name = match.group(1)
            default_value = match.group(2) if match.group(2) is not None else ''
            return os.environ.get(var_name, default_value)

        return re.sub(pattern, replacer, value)

    elif isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]

    else:
        # Primitive type (int, bool, None, etc.)
        return value


def load_yaml_file(file_path: Path) -> Dict[str, Any]:
    """
    Load YAML file with error handling

    Args:
        file_path: Path to YAML file

    Returns:
        Parsed YAML as dictionary

    Raises:
        ConfigurationError: If file cannot be loaded
    """
    try:
        if not file_path.exists():
            raise ConfigurationError(
                str(file_path),
                f"File does not exist"
            )

        with open(file_path, 'r') as f:
            content = yaml.safe_load(f)

        if content is None:
            raise ConfigurationError(
                str(file_path),
                "File is empty"
            )

        return content

    except ConfigurationError:
        raise
    except yaml.YAMLError as e:
        raise ConfigurationError(
            str(file_path),
            f"Invalid YAML syntax: {str(e)}"
        )
    except PermissionError:
        raise ConfigurationError(
            str(file_path),
            "Permission denied - cannot read file"
        )
    except Exception as e:
        raise ConfigurationError(
            str(file_path),
            f"Cannot read file: {str(e)}"
        )


class Config:
    """Configuration manager"""

    def __init__(self, config_dir: Optional[Path] = None):
        """
        Initialize configuration

        Args:
            config_dir: Directory containing config.yml and rules.yml
                       Defaults to /config
        """
        if config_dir is None:
            config_dir = Path(os.environ.get('CONFIG_DIR', '/config'))

        self.config_dir = config_dir
        self.config_file = config_dir / 'config.yml'
        self.rules_file = config_dir / 'rules.yml'

        # Auto-copy defaults if missing
        copy_default_if_missing(self.config_file, 'config.default.yml')
        copy_default_if_missing(self.rules_file, 'rules.default.yml')

        # Resolver and cache for resolved rules (set by _load_rules)
        self._resolver: Optional[RuleResolver] = None
        self._resolved_rules_cache: Optional[list] = None

        # Load configurations
        self._load_config()
        self._load_rules()
        self._load_schedule()

        # rules.yml's identity when last loaded, for hot-reload. None makes
        # the first get_rules() re-read it.
        self._rules_stamp: Optional[tuple] = None

    def _load_config(self):
        """Load config.yml with environment variable expansion"""
        logging.debug(f"Loading config from {self.config_file}")

        raw_config = load_yaml_file(self.config_file)
        self.config = expand_env_vars(raw_config)

        logging.debug(f"Configuration loaded successfully")

    def _load_rules(self):
        """Load rules.yml and initialize resolver

        Everything is validated before any of it is assigned, so a reload
        that fails leaves the previously loaded rules, refs and resolver
        fully intact (get_rules() relies on that to keep serving them).
        """
        logging.debug(f"Loading rules from {self.rules_file}")

        raw_rules = load_yaml_file(self.rules_file)

        # The same checks the rules editor runs before it will save a file
        # (rules_validation.check_rules_structure) -- here only the first
        # problem is reported, as a ConfigurationError.
        problems = check_rules_structure(raw_rules)
        if problems:
            raise ConfigurationError(str(self.rules_file), problems[0].message)

        rules = raw_rules.get('rules', [])
        logging.debug(f"Loaded {len(rules)} rules")

        # Extract refs block and initialize resolver
        refs = raw_rules.get('refs') or {}
        instances = raw_rules.get('instances') or {}

        # Create resolver with global refs
        # TODO: Support per-instance resolvers when running against specific instances
        resolver = RuleResolver(refs=refs, instance_id=None, instances=instances)

        # Schema marker for rules.yml, defaulting to 1 for files written
        # before this existed. Not consulted by anything today -- purely a
        # forward-compat hook so a future migration can tell "this file
        # predates schema versioning" from "this file already declared one"
        # without sniffing structure. Refreshed on every hot-reload since
        # this is set inside _load_rules(), which get_rules() re-invokes
        # when rules.yml changes on disk.
        self.rules_version = raw_rules.get('version', 1)
        self.rules = rules
        self._resolver = resolver

        # Invalidate resolved rules cache
        self._resolved_rules_cache = None

        if refs:
            logging.debug(f"Initialized resolver with refs: vars={len(resolver.vars)}, "
                         f"conditions={len(resolver.conditions)}, "
                         f"actions={len(resolver.actions)}")

    def _load_schedule_from_env(self) -> Optional[List[Dict[str, str]]]:
        """
        Build a schedule list from indexed QBT_RULES_SCHEDULE_<N>_CRON /
        _<N>_CONTEXT environment variables (each supporting the usual
        _FILE variant via resolve_config). Indices must be contiguous
        starting at 0; enumeration stops at the first missing _CRON.

        Returns None if none are set, so the caller falls back to
        config.yml's 'schedule' list. If any entries ARE found here, they
        entirely replace config.yml's list rather than merging with it --
        consistent with how every other field's env var overrides its
        config.yml value outright.
        """
        entries = []
        i = 0
        while True:
            cron = resolve_config(None, f'QBT_RULES_SCHEDULE_{i}_CRON', {}, 'unused', default=None)
            if cron is None:
                break

            context = resolve_config(None, f'QBT_RULES_SCHEDULE_{i}_CONTEXT', {}, 'unused', default=None)
            if context is None:
                raise ConfigurationError(
                    str(self.config_file),
                    f"QBT_RULES_SCHEDULE_{i}_CRON is set but QBT_RULES_SCHEDULE_{i}_CONTEXT is missing"
                )

            entries.append({'cron': cron, 'context': context})
            i += 1

        return entries if entries else None

    def _load_schedule(self):
        """
        Load and validate the optional 'schedule' section

        Sourced from indexed QBT_RULES_SCHEDULE_<N>_CRON/_<N>_CONTEXT
        environment variables if any are set (see _load_schedule_from_env),
        otherwise from config.yml's 'schedule' list. Each entry is a dict
        with 'cron' (a standard 5-field cron expression) and 'context'
        (the context string to enqueue when the cron fires). Not
        hot-reloaded -- read once at startup, same as every other
        top-level config.yml section.
        """
        env_schedule = self._load_schedule_from_env()
        self.schedule = env_schedule if env_schedule is not None else (self.config.get('schedule') or [])

        if not isinstance(self.schedule, list):
            raise ConfigurationError(
                str(self.config_file),
                "'schedule' must be a list"
            )

        for i, entry in enumerate(self.schedule):
            if not isinstance(entry, dict):
                raise ConfigurationError(
                    str(self.config_file),
                    f"Schedule entry #{i+1} must be a dictionary"
                )

            if 'cron' not in entry:
                raise ConfigurationError(
                    str(self.config_file),
                    f"Schedule entry #{i+1} missing required field: 'cron'"
                )

            if 'context' not in entry:
                raise ConfigurationError(
                    str(self.config_file),
                    f"Schedule entry #{i+1} missing required field: 'context'"
                )

            if not croniter.is_valid(entry['cron']):
                raise ConfigurationError(
                    str(self.config_file),
                    f"Schedule entry #{i+1} has an invalid cron expression: '{entry['cron']}'"
                )

        logging.debug(f"Loaded {len(self.schedule)} schedule entries")

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value by dot-notation key

        Args:
            key: Configuration key (e.g., 'qbittorrent.host')
            default: Default value if key not found

        Returns:
            Configuration value
        """
        return get_nested_config(self.config, key, default)

    def is_dry_run(self) -> bool:
        """Check if dry-run mode is enabled"""
        return parse_bool(resolve_config(None, 'QBT_RULES_DRY_RUN', self.config, 'engine.dry_run', default=False))

    def _current_rules_stamp(self) -> tuple:
        """
        rules.yml's identity on disk: any change to it means reload

        Compared for inequality rather than "mtime went up", so a file
        swapped for an older one (a restored backup, `cp -p`, a git
        checkout) still reloads. Inode catches an atomic replace even
        within one mtime tick; size catches an in-place rewrite.
        """
        st = self.rules_file.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def reload_rules(self, force: bool = False) -> bool:
        """
        Re-read rules.yml if it changed on disk since it was last loaded
        (or unconditionally with force=True)

        Raises whatever loading raises -- ConfigurationError for a file the
        loader rejects -- leaving the previously loaded rules in place.

        Returns:
            True if the rules were reloaded
        """
        stamp = self._current_rules_stamp()
        if not force and stamp == self._rules_stamp:
            return False
        self._load_rules()
        self._rules_stamp = stamp
        logging.info(f"Rules reloaded from {self.rules_file}")
        return True

    def get_rules(self, resolved: bool = True) -> list:
        """
        Get list of rules with hot-reload support

        Re-reads rules.yml if the file has been modified since last load.
        This allows rules to be updated without restarting the server.

        Args:
            resolved: If True, return rules with refs/vars resolved (default: True)
                     If False, return raw rules without resolution

        Returns:
            List of rule dictionaries

        Note:
            If reload fails (syntax error, file deleted, etc.), the previously
            loaded rules are retained and a warning is logged.

        Examples:
            >>> config.get_rules()  # Resolved rules (default)
            [{'id': 'cleanup', 'conditions': [...], 'actions': [...]}]

            >>> config.get_rules(resolved=False)  # Raw rules
            [{'id': 'cleanup', 'conditions': [{'$ref': 'conditions.well-seeded'}], ...}]
        """
        try:
            self.reload_rules()
        except Exception as e:
            # Keep existing rules on error - graceful degradation
            logging.warning(f"Failed to reload rules from {self.rules_file}: {e}. Using cached rules.")

        # Return raw rules if requested
        if not resolved:
            return self.rules

        # Return cached resolved rules if available
        if self._resolved_rules_cache is not None:
            return self._resolved_rules_cache

        # Resolve rules and cache the result
        if self._resolver is None:
            # No refs block - return raw rules
            self._resolved_rules_cache = self.rules
        else:
            # Resolve each rule
            resolved_rules = []
            for rule in self.rules:
                try:
                    resolved_rule = self._resolver.resolve_rule(rule)
                    resolved_rules.append(resolved_rule)
                except Exception as e:
                    # Log error with rule id for debugging
                    rule_id = rule.get('id', 'unknown')
                    logging.error(f"Failed to resolve rule '{rule_id}': {e}")
                    raise  # Re-raise to surface the error

            self._resolved_rules_cache = resolved_rules
            logging.debug(f"Resolved and cached {len(resolved_rules)} rules")

        return self._resolved_rules_cache


def load_config(config_dir: Optional[Path] = None) -> Config:
    """
    Load configuration from directory

    Args:
        config_dir: Directory containing config.yml and rules.yml

    Returns:
        Config object
    """
    return Config(config_dir)
