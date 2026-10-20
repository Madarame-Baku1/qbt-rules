"""
qBittorrent Rules Engine
Core logic for evaluating conditions and executing actions
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple

import requests

from qbt_rules.api import QBittorrentAPI
from qbt_rules.utils import parse_tags, is_older_than, is_newer_than, is_larger_than, is_smaller_than
from qbt_rules.errors import DataNotReadyError, FieldError, OperatorError
from qbt_rules.trackie import (
    ACCOUNT_FIELDS,
    COMPUTED_ACCOUNT_FIELDS,
    COMPUTED_TORRENT_FIELDS,
    TORRENT_FIELDS,
    TrackerDataNotReadyError,
    TrackieClient,
    TrackieSnapshot,
    parse_max_age,
)
from qbt_rules.logging import get_logger
from qbt_rules import metrics

logger = get_logger(__name__)

# Pattern for runtime action-param templating: ${info.name}, ${trackers.url},
# etc -- the per-torrent counterpart to resolver.py's TOKEN_PATTERN, which
# handles the load-time-only ${vars.*}/${rule.*} tokens and deliberately
# leaves these untouched for ActionExecutor to resolve here instead.
# One-or-more `.segment` repeats (not just one) so multi-level paths like
# ${stats.state.queuedUP} match too, not just single-dot fields.
ACTION_TEMPLATE_PATTERN = re.compile(r'\$\{(\w+(?:\.\w+)+)\}')


@dataclass
class RuleStats:
    """Statistics for rule execution"""
    total_torrents: int = 0
    processed: int = 0
    rules_matched: int = 0
    actions_executed: int = 0
    actions_skipped: int = 0
    errors: int = 0
    # Per-rule breakdown of the same counters above, keyed by rule['id']
    # (required on every rule -- see Config._load_rules). Additive: existing
    # consumers reading only the aggregate fields above are unaffected by
    # this key's presence.
    by_rule: Dict[str, Dict[str, int]] = field(default_factory=dict)


class ConditionEvaluator:
    """Evaluates rule conditions against torrents using dot notation"""

    def __init__(self, api: QBittorrentAPI):
        """
        Initialize condition evaluator

        Args:
            api: qBittorrent API client
        """
        self.api = api

        # Caches for per-torrent API calls
        self.trackers_cache: Dict[str, List[Dict]] = {}
        self.files_cache: Dict[str, List[Dict]] = {}
        self.peers_cache: Dict[str, List[Dict]] = {}
        self.properties_cache: Dict[str, Dict] = {}
        self.webseeds_cache: Dict[str, List[Dict]] = {}

        # Global context cache (shared across all torrents)
        self.transfer_info: Optional[Dict] = None
        self.app_preferences: Optional[Dict] = None

        # Aggregate torrent-count/sum snapshot for the stats.* namespace --
        # set once per run by RulesEngine.run() before any rule is evaluated
        # (see its docstring for snapshot semantics). Defaults here so
        # stats.* still resolves to 0 rather than erroring if the evaluator
        # is ever used standalone, outside of RulesEngine.run().
        self.stats: Dict[str, Any] = {
            'state': {},
            'category': {},
            'tag': {},
            'total': 0,
            'dlspeed': 0,
            'upspeed': 0,
            'seeds': 0,
            'leechs': 0,
        }

        # tracker.* namespace (trackie integration). Set by RulesEngine when
        # integrations.trackie.url is configured; the snapshot is fetched
        # lazily on the first tracker.* lookup of a run and then reused for
        # every torrent and rule in that run -- see reset_trackie().
        self.trackie: Optional[TrackieClient] = None
        self._trackie_snapshot: Optional[TrackieSnapshot] = None
        self._trackie_error: Optional[str] = None
        self._trackie_loaded = False

    def reset_trackie(self):
        """Forget the previous run's trackie snapshot (call once per run)"""
        self._trackie_snapshot = None
        self._trackie_error = None
        self._trackie_loaded = False

    def clear_caches(self):
        """Clear all caches (call between rule executions)"""
        self.trackers_cache.clear()
        self.files_cache.clear()
        self.peers_cache.clear()
        self.properties_cache.clear()
        self.webseeds_cache.clear()
        self.transfer_info = None
        self.app_preferences = None

    def evaluate(
        self,
        torrent: Dict,
        conditions: Any,
        current_context: Optional[str] = None,
        required_context: Optional[Any] = None,
        trace: Optional[List[Dict]] = None,
    ) -> bool:
        """
        Evaluate all conditions for a torrent

        Args:
            torrent: Torrent dictionary from qBittorrent API
            conditions: Conditions from rule. Either a dict with 'all'/'any'/'none' keys,
                        or a bare list of condition entries (treated as an implicit 'all')
            current_context: Current runtime context (torrent-imported, download-finished, weekly-cleanup, adhoc-run, custom, or None)
            required_context: Context requirement from rule level (string or list of strings)
                            When None, rule has no context requirement and executes regardless of runtime context
            trace: Optional list to append per-leaf-condition evaluation records to
                   ({'field', 'operator', 'value', 'actual', 'passed'}), for a future
                   "preview this rule against the live fleet" feature. Every existing
                   caller passes nothing, leaving this None, so behavior/perf is
                   unchanged unless a caller opts in. Short-circuiting in all/any/none
                   is unaffected -- conditions skipped by short-circuit (e.g. after
                   the first failing entry in an `all` block) don't appear in the
                   trace, same as they never ran without tracing either.

        Returns:
            True if all conditions match

        Notes:
            - Rules WITHOUT context (required_context=None) execute regardless of runtime context
            - Rules WITH context only execute when runtime context matches their requirement
            - A bare list for `conditions` (no 'all'/'any'/'none' wrapper) is treated as an
              implicit 'all': every entry must match. Without this, a bare list silently
              matched nothing in 'all'/'any'/'none' and always evaluated to True.
        """
        try:
            # Check context requirement first (from rule level, not inside conditions)
            if required_context is not None:
                if not self._evaluate_context(current_context, required_context):
                    return False
            # If required_context is None, rule has no context requirement → continue to conditions

            # A bare list of conditions is shorthand for {'all': conditions}
            if isinstance(conditions, list):
                return self._evaluate_all(torrent, conditions, trace)

            # Evaluate logical groups
            if 'all' in conditions:
                if not self._evaluate_all(torrent, conditions['all'], trace):
                    return False

            if 'any' in conditions:
                if not self._evaluate_any(torrent, conditions['any'], trace):
                    return False

            if 'none' in conditions:
                if not self._evaluate_none(torrent, conditions['none'], trace):
                    return False

            return True

        except DataNotReadyError as e:
            # Expected/benign -- e.g. the files list right after an OnTorrentAdded
            # webhook, before qBittorrent has indexed the torrent. Not a real error,
            # so keep it out of the ERROR-level log noise every other rule failure uses.
            logger.debug(f"Skipping rule for {torrent.get('name', 'unknown')}: {e.message} ({e.details.get('Field')})")
            return False

        except Exception as e:
            logger.error(f"Error evaluating conditions for {torrent.get('name', 'unknown')}: {e}")
            return False

    def _evaluate_context(self, current_context: Optional[str], required_context: Any) -> bool:
        """
        Evaluate if current runtime context matches the rule's context requirement

        Args:
            current_context: Current runtime context (torrent-imported, weekly-cleanup, adhoc-run, etc.)
                           Can be None if running without a specific context
            required_context: Rule's context requirement (string or list of strings)
                            This is always non-None when this method is called

        Returns:
            True if runtime context matches the requirement, False otherwise

        Notes:
            - This method is only called when a rule HAS a context requirement (required_context is not None)
            - If current_context is None, the rule cannot match (no runtime context to match against)
            - If required_context is a list, checks if current_context is in that list
            - If required_context is a string, checks for exact match
        """
        if current_context is None:
            # No runtime context provided, but rule requires one → no match
            return False

        if isinstance(required_context, list):
            return current_context in required_context
        else:
            return current_context == required_context

    def _evaluate_all(self, torrent: Dict, conditions: List[Dict], trace: Optional[List[Dict]] = None) -> bool:
        """All conditions must match (AND)"""
        return all(self._evaluate_condition(torrent, cond, trace) for cond in conditions)

    def _evaluate_any(self, torrent: Dict, conditions: List[Dict], trace: Optional[List[Dict]] = None) -> bool:
        """Any condition must match (OR)"""
        return any(self._evaluate_condition(torrent, cond, trace) for cond in conditions)

    def _evaluate_none(self, torrent: Dict, conditions: List[Dict], trace: Optional[List[Dict]] = None) -> bool:
        """No conditions must match (NOT)"""
        return not any(self._evaluate_condition(torrent, cond, trace) for cond in conditions)

    def _evaluate_condition(self, torrent: Dict, condition: Dict, trace: Optional[List[Dict]] = None) -> bool:
        """
        Evaluate a single condition

        Args:
            torrent: Torrent dictionary
            condition: Condition dictionary with field, operator, value OR nested logical operator
            trace: Optional list to append a leaf-condition evaluation record to -- see evaluate()

        Returns:
            True if condition matches
        """
        # Handle nested logical operators
        if 'all' in condition:
            return self._evaluate_all(torrent, condition['all'], trace)
        if 'any' in condition:
            return self._evaluate_any(torrent, condition['any'], trace)
        if 'none' in condition:
            return self._evaluate_none(torrent, condition['none'], trace)

        # Handle regular field conditions
        field = condition['field']
        operator = condition['operator']
        value = condition['value']

        # Get field value using dot notation
        actual = self._get_field_value(torrent, field)

        # Evaluate based on operator
        passed = self._apply_operator(actual, operator, value, field)

        if trace is not None:
            trace.append({
                'field': field,
                'operator': operator,
                'value': value,
                'actual': actual,
                'passed': passed,
            })

        return passed

    def get_field_value(self, torrent: Dict, field: str) -> Any:
        """
        Public entry point for resolving a dot-notation torrent field
        (info.*, trackers.*, files.*, peers.*, properties.*, webseeds.*,
        transfer.*, app.*, stats.*)

        Exposed so ActionExecutor's notify templating (${info.x}/
        ${trackers.x}/etc in a message) can share this evaluator's
        lazy-loaded, per-run caches instead of independently re-fetching
        trackers/files/peers from the qBittorrent API -- see RulesEngine.
        __init__, which passes this evaluator to ActionExecutor as its
        field_resolver.
        """
        return self._get_field_value(torrent, field)

    def _get_field_value(self, torrent: Dict, field: str) -> Any:
        """
        Get field value with automatic API endpoint routing

        Args:
            torrent: Torrent dictionary
            field: Field name in dot notation (e.g., 'info.name', 'trackers.url')

        Returns:
            Field value

        Raises:
            FieldError: If field format is invalid
        """
        if '.' not in field:
            raise FieldError(
                field,
                "Field must use dot notation with API prefix"
            )

        # Split into API endpoint and property
        endpoint, property_name = field.split('.', 1)
        torrent_hash = torrent.get('hash', '')

        # Route to appropriate API endpoint
        if endpoint == 'info':
            # Base torrent info - already available, no API call
            if property_name == 'tags':
                return parse_tags(torrent)
            return torrent.get(property_name)

        elif endpoint == 'trackers':
            # Trackers collection - lazy load with cache
            if torrent_hash not in self.trackers_cache:
                self.trackers_cache[torrent_hash] = self.api.get_trackers(torrent_hash)
            items = self.trackers_cache[torrent_hash]
            # Filter out special entries (DHT, PeX, LSD)
            items = [t for t in items if t.get('url', '').startswith('http')]
            # Return list of property values
            return [item.get(property_name) for item in items if property_name in item]

        elif endpoint == 'files':
            # Files collection - lazy load with cache
            if torrent_hash not in self.files_cache:
                self.files_cache[torrent_hash] = self.api.get_files(torrent_hash)
            items = self.files_cache[torrent_hash]
            if not items:
                # A torrent always has >=1 file once qBittorrent has loaded its
                # metadata -- an empty response here means metadata isn't loaded
                # yet (e.g. right after an OnTorrentAdded webhook, before qBittorrent
                # has indexed the torrent's files), not that the torrent genuinely
                # has zero files. Returning [] here would let `none: [...]` conditions
                # (e.g. "has no video file") vacuously match on missing data instead
                # of on a confirmed absence, misidentifying real content as fake.
                raise DataNotReadyError(field, torrent_hash)
            return [item.get(property_name) for item in items if property_name in item]

        elif endpoint == 'peers':
            # Peers collection - lazy load with cache
            if torrent_hash not in self.peers_cache:
                self.peers_cache[torrent_hash] = self.api.get_peers(torrent_hash)
            items = self.peers_cache[torrent_hash]
            return [item.get(property_name) for item in items if property_name in item]

        elif endpoint == 'properties':
            # Extended properties - lazy load with cache
            if torrent_hash not in self.properties_cache:
                self.properties_cache[torrent_hash] = self.api.get_properties(torrent_hash)
            props = self.properties_cache[torrent_hash]
            return props.get(property_name)

        elif endpoint == 'webseeds':
            # Web seeds collection - lazy load with cache
            if torrent_hash not in self.webseeds_cache:
                self.webseeds_cache[torrent_hash] = self.api.get_webseeds(torrent_hash)
            items = self.webseeds_cache[torrent_hash]
            return [item.get(property_name) for item in items if property_name in item]

        elif endpoint == 'transfer':
            # Global transfer info - single call cached
            if self.transfer_info is None:
                self.transfer_info = self.api.get_transfer_info()
            return self.transfer_info.get(property_name)

        elif endpoint == 'app':
            # Global app preferences - single call cached
            if self.app_preferences is None:
                self.app_preferences = self.api.get_app_preferences()
            return self.app_preferences.get(property_name)

        elif endpoint == 'stats':
            # Aggregate torrent counts/sums - snapshot taken once per run by
            # RulesEngine.run(), not recomputed per rule (see self.stats
            # docstring in __init__). Unlike other endpoints, missing keys
            # default to 0 rather than raising/None, since "0 torrents in
            # this state" and "this state doesn't exist" are the same thing
            # to a numeric condition operator. stats.dlspeed/upspeed/seeds/
            # leechs are plain scalars (fleet-wide sums), not nested like
            # state/category/tag, so they fall through to the final
            # self.stats.get(property_name, 0) below.
            parts = property_name.split('.', 1)
            if len(parts) == 2 and parts[0] in ('state', 'category', 'tag'):
                return self.stats.get(parts[0], {}).get(parts[1], 0)
            return self.stats.get(property_name, 0)

        elif endpoint == 'tracker':
            return self._get_tracker_field(torrent, property_name, field)

        else:
            raise FieldError(
                field,
                f"Unknown API endpoint: '{endpoint}'"
            )

    def _trackie(self, field: str, torrent_hash: str) -> TrackieSnapshot:
        """This run's trackie snapshot, fetched on first use"""
        if not self._trackie_loaded:
            self._trackie_loaded = True
            assert self.trackie is not None
            self._trackie_snapshot, self._trackie_error = self.trackie.fetch()
            if self._trackie_error:
                # Once per run, not once per torrent: every tracker.* lookup
                # this run will be skipped for the same reason
                logger.warning(f"tracker.* rules skipped this run: {self._trackie_error}")
        if self._trackie_snapshot is None:
            raise TrackerDataNotReadyError(field, torrent_hash, self._trackie_error or "no snapshot")
        return self._trackie_snapshot

    def _get_tracker_field(self, torrent: Dict, property_name: str, field: str) -> Any:
        """
        tracker.<field> and tracker.account.<field> from trackie's snapshot

        tracker.* reads the torrent's own record (looked up by hash);
        tracker.account.* reads the account of whichever tracker the torrent
        is on -- from its record when matched, else from its tracker URLs
        against each account's announce_hosts, so account checks work for
        torrents trackie hasn't matched (e.g. manual adds).

        Raises TrackerDataNotReadyError (rule skipped, not failed) when the
        data is unreachable, stale or missing, and FieldError for an unknown
        field or when the integration isn't configured.
        """
        if self.trackie is None:
            raise FieldError(
                field,
                "tracker.* fields need the trackie integration: set integrations.trackie.url"
            )

        is_account = property_name.startswith('account.')
        name = property_name[len('account.'):] if is_account else property_name
        known = (
            set(ACCOUNT_FIELDS) | set(COMPUTED_ACCOUNT_FIELDS) if is_account
            else set(TORRENT_FIELDS) | set(COMPUTED_TORRENT_FIELDS)
        )
        if name not in known:
            raise FieldError(
                field,
                f"Unknown tracker field '{name}' (valid: {', '.join(sorted(known))})"
            )

        torrent_hash = torrent.get('hash', '')
        snap = self._trackie(field, torrent_hash)
        record = snap.record(torrent_hash)

        if is_account:
            key = record['tracker'] if record else snap.tracker_for_urls(
                self._get_field_value(torrent, 'trackers.url')
            )
            if key is None:
                raise TrackerDataNotReadyError(
                    field, torrent_hash, "torrent is not on a tracker trackie covers"
                )
            stale = snap.stale_reason(key)
            account = snap.accounts.get(key)
            if stale or account is None:
                raise TrackerDataNotReadyError(field, torrent_hash, stale or f"no account data for '{key}'")
            if name == 'ratio_ok':
                ratio, minimum = account.get('ratio'), account.get('min_ratio')
                return None if ratio is None or minimum is None else ratio >= minimum
            if name == 'age':
                return snap.age_s(account.get('seen_at'))
            return account.get(ACCOUNT_FIELDS[name])

        if record is None:
            # Not on a covered tracker, not on the tracker's pages (so its
            # standing is unknown), or not matched to this hash yet
            raise TrackerDataNotReadyError(field, torrent_hash, "trackie has no record for this torrent")
        stale = snap.stale_reason(record.get('tracker', ''))
        if stale:
            raise TrackerDataNotReadyError(field, torrent_hash, stale)
        if name == 'age':
            return snap.age_s(record.get('seen_at'))
        return record.get(TORRENT_FIELDS[name])

    def _apply_operator(self, actual: Any, operator: str, expected: Any, field: str) -> bool:
        """
        Apply comparison operator

        Args:
            actual: Actual value from torrent
            operator: Comparison operator
            expected: Expected value from rule
            field: Field name (for error messages)

        Returns:
            True if comparison matches

        Raises:
            OperatorError: If operator is unknown
        """
        # Handle None/missing values
        if actual is None:
            return operator in ['!=', 'not_in', 'not_contains']

        # Handle list values (from collection fields)
        if isinstance(actual, list):
            if not actual:  # Empty list
                return operator in ['!=', 'not_in', 'not_contains']

            # Negation operators need ALL items to satisfy the check (i.e. none
            # of the items positively match) -- applying ANY here would mean
            # "at least one item doesn't match", which is true for almost any
            # multi-item collection and defeats the point of a negation check.
            # E.g. tags ['private', 'iptorrent.com'] with not_contains 'private':
            # the 'iptorrent.com' tag alone satisfies not_contains, so ANY
            # incorrectly returns True even though the torrent IS tagged
            # private. Positive operators keep ANY semantics: "does at least
            # one item match" is the natural meaning for e.g. contains/==/in.
            if operator in ('!=', 'not_in', 'not_contains'):
                return all(self._apply_operator(item, operator, expected, field) for item in actual)

            return any(self._apply_operator(item, operator, expected, field) for item in actual)

        # String operators
        if operator == '==':
            return actual == expected
        elif operator == '!=':
            return actual != expected
        elif operator == 'contains':
            if isinstance(expected, list):
                return any(str(item) in str(actual) for item in expected)
            return expected in str(actual)
        elif operator == 'not_contains':
            if isinstance(expected, list):
                return not any(str(item) in str(actual) for item in expected)
            return expected not in str(actual)
        elif operator == 'matches':
            return re.search(str(expected), str(actual)) is not None

        # List operators
        elif operator == 'in':
            if isinstance(expected, list):
                return actual in expected
            return actual == expected
        elif operator == 'not_in':
            if isinstance(expected, list):
                return actual not in expected
            return actual != expected

        # Numeric operators
        elif operator in ['>', '<', '>=', '<=']:
            try:
                actual_num = float(actual)
                expected_num = float(expected)

                if operator == '>':
                    return actual_num > expected_num
                elif operator == '<':
                    return actual_num < expected_num
                elif operator == '>=':
                    return actual_num >= expected_num
                elif operator == '<=':
                    return actual_num <= expected_num
            except (ValueError, TypeError):
                logger.warning(f"Cannot compare non-numeric values for field {field}")
                return False

        # Size operators
        elif operator == 'smaller_than':
            return is_smaller_than(int(actual), str(expected))
        elif operator == 'larger_than':
            return is_larger_than(int(actual), str(expected))

        # Time operators
        elif operator == 'older_than':
            return is_older_than(int(actual), str(expected))
        elif operator == 'newer_than':
            return is_newer_than(int(actual), str(expected))

        else:
            raise OperatorError(operator, field)


class ActionExecutor:
    """Executes actions on torrents with idempotency checks"""

    def __init__(
        self,
        api: QBittorrentAPI,
        dry_run: bool,
        notifications_config: Optional[Dict[str, str]] = None,
        integrations_config: Optional[Dict[str, Dict[str, str]]] = None,
        field_resolver: Optional['ConditionEvaluator'] = None,
    ):
        """
        Initialize action executor

        Args:
            api: qBittorrent API client
            dry_run: If True, only log actions without executing
            notifications_config: Pre-resolved notifications config
                ({'webhook_url': ..., 'service': ...}) for the notify
                action, already _FILE-resolved at server startup
            integrations_config: Pre-resolved Sonarr/Radarr config
                ({'sonarr': {'url', 'api_key'}, 'radarr': {...}}) for the
                arr_blocklist action, already _FILE-resolved
                at server startup
            field_resolver: Object exposing get_field_value(torrent, field)
                (a ConditionEvaluator, duck-typed) -- used to resolve
                ${info.x}/${trackers.x}/etc tokens in notify messages.
                Optional: RulesEngine always passes its own evaluator, so
                notify shares the same lazy-loaded trackers/files/peers
                caches conditions already populated for this torrent
                rather than re-fetching from the API; falls back to a
                private, unshared ConditionEvaluator if none is given
                (e.g. direct construction in a test)
        """
        self.api = api
        self.dry_run = dry_run
        self.notifications_config = notifications_config or {}
        self.integrations_config = integrations_config or {}
        self.field_resolver = field_resolver or ConditionEvaluator(api)

    def execute(self, torrent: Dict, action: Dict) -> Tuple[bool, bool]:
        """
        Execute an action on a torrent

        Args:
            torrent: Torrent dictionary
            action: Action dictionary with type and params

        Returns:
            Tuple of (success, skipped_due_to_idempotency)
        """
        action_type = action['type']
        params = action.get('params', {})

        try:
            # Check idempotency before executing
            if self._should_skip_idempotent(torrent, action_type, params):
                logger.info(f"  {torrent['name']} - {action_type} already in desired state (skipped)")
                return True, True
            if self.dry_run:
                self._log_dry_run(torrent, action_type, params)
                return True, True  # Dry run actions count as "skipped" (not actually executed)
            # Execute action
            success = self._execute_action(torrent, action_type, params)
            metrics.record_action_executed(action_type, success)
            return success, False

        except Exception as e:
            logger.error(f"Action {action_type} failed for {torrent['name']}: {e}")
            metrics.record_action_executed(action_type, False)
            return False, False

    def _should_skip_idempotent(self, torrent: Dict, action_type: str, params: Dict) -> bool:
        """Check if action is idempotent and already applied"""
        if action_type == 'stop':
            return 'paused' in torrent.get('state', '').lower()

        elif action_type == 'start':
            return 'paused' not in torrent.get('state', '').lower()

        elif action_type == 'set_category':
            return torrent.get('category') == params.get('category')

        elif action_type == 'add_tag':
            current_tags = set(parse_tags(torrent))
            new_tags = set(params.get('tags', []))
            return new_tags.issubset(current_tags)

        elif action_type == 'remove_tag':
            current_tags = set(parse_tags(torrent))
            remove_tags = set(params.get('tags', []))
            return not remove_tags.intersection(current_tags)

        elif action_type == 'force_start':
            return bool(torrent.get('force_start'))

        elif action_type == 'release_force_start':
            return not torrent.get('force_start')

        # Non-idempotent actions
        return False

    def _log_dry_run(self, torrent: Dict, action_type: str, params: Dict):
        """Log what would happen in dry run"""
        if action_type == 'delete_torrent':
            delete_files = self._resolve_delete_files(params)
            logger.info(f"  Would delete {torrent['name']} (delete_files={delete_files})")
        elif action_type == 'notify':
            service = params.get('service', self.notifications_config.get('service', 'generic'))
            message = self._render_notify_message(torrent, params.get('message'))
            logger.info(f"  Would notify via {service}: {message}")
        elif action_type == 'arr_blocklist':
            service = params.get('service')
            search = bool(params.get('search', True))
            action_desc = "blocklist and search" if search else "blocklist (no search)"
            logger.info(f"  Would {action_desc} for {torrent['name']} via {service}")
        else:
            logger.info(f"  Would {action_type} {torrent['name']} (params={params})")

    def _resolve_delete_files(self, params: Dict) -> bool:
        """
        Resolve delete_torrent's delete_files parameter

        Args:
            params: Action params dict, possibly containing 'delete_files'

        Returns:
            True if the underlying files should be deleted, False to keep them
        """
        return bool(params.get('delete_files', True))

    def _execute_action(self, torrent: Dict, action_type: str, params: Dict) -> bool:
        """Execute the actual action"""
        torrent_hash = torrent['hash']

        if action_type == 'stop':
            success = self.api.stop_torrents([torrent_hash])
            if success:
                logger.info(f"  Stopped {torrent['name']}")
            return success

        elif action_type == 'start':
            success = self.api.start_torrents([torrent_hash])
            if success:
                logger.info(f"  Started {torrent['name']}")
            return success

        elif action_type == 'force_start':
            success = self.api.force_start_torrents([torrent_hash])
            if success:
                logger.info(f"  Force started {torrent['name']}")
            return success

        elif action_type == 'release_force_start':
            success = self.api.force_start_torrents([torrent_hash], enable=False)
            if success:
                logger.info(f"  Released force-start for {torrent['name']}")
            return success

        elif action_type == 'recheck':
            success = self.api.recheck_torrents([torrent_hash])
            if success:
                logger.info(f"  Rechecking {torrent['name']}")
            return success

        elif action_type == 'reannounce':
            success = self.api.reannounce_torrents([torrent_hash])
            if success:
                logger.info(f"  Reannouncing {torrent['name']}")
            return success

        elif action_type == 'delete_torrent':
            delete_files = self._resolve_delete_files(params)
            success = self.api.delete_torrents([torrent_hash], delete_files=delete_files)
            if success:
                logger.info(f"  Deleted {torrent['name']} (delete_files={delete_files})")
            return success

        elif action_type == 'set_category':
            category = params.get('category', '')
            success = self.api.set_category([torrent_hash], category)
            if success:
                logger.info(f"  Set category for {torrent['name']} to {category}")
            return success

        elif action_type == 'add_tag':
            tags = params.get('tags', [])
            success = self.api.add_tags([torrent_hash], tags)
            if success:
                logger.info(f"  Added tags {tags} to {torrent['name']}")
            return success

        elif action_type == 'remove_tag':
            tags = params.get('tags', [])
            success = self.api.remove_tags([torrent_hash], tags)
            if success:
                logger.info(f"  Removed tags {tags} from {torrent['name']}")
            return success

        elif action_type == 'set_tags':
            # Remove all existing tags first, then set new ones
            current_tags = parse_tags(torrent)
            new_tags = params.get('tags', [])
            if current_tags:
                self.api.remove_tags([torrent_hash], current_tags)
            success = self.api.add_tags([torrent_hash], new_tags)
            if success:
                logger.info(f"  Set tags for {torrent['name']} to {new_tags}")
            return success

        elif action_type == 'set_upload_limit':
            limit = params.get('limit', -1)
            success = self.api.set_upload_limit([torrent_hash], limit)
            if success:
                logger.info(f"  Set upload limit for {torrent['name']} to {limit}")
            return success

        elif action_type == 'set_download_limit':
            limit = params.get('limit', -1)
            success = self.api.set_download_limit([torrent_hash], limit)
            if success:
                logger.info(f"  Set download limit for {torrent['name']} to {limit}")
            return success

        elif action_type == 'increase_priority':
            success = self.api.increase_priority([torrent_hash])
            if success:
                logger.info(f"  Increased priority for {torrent['name']}")
            return success

        elif action_type == 'decrease_priority':
            success = self.api.decrease_priority([torrent_hash])
            if success:
                logger.info(f"  Decreased priority for {torrent['name']}")
            return success

        elif action_type == 'set_top_priority':
            success = self.api.set_top_priority([torrent_hash])
            if success:
                logger.info(f"  Set top priority for {torrent['name']}")
            return success

        elif action_type == 'set_bottom_priority':
            success = self.api.set_bottom_priority([torrent_hash])
            if success:
                logger.info(f"  Set bottom priority for {torrent['name']}")
            return success

        elif action_type == 'notify':
            return self._execute_notify(torrent, params)

        elif action_type == 'arr_blocklist':
            return self._execute_arr_blocklist(torrent, params)

        else:
            logger.error(f"  Unknown action type: {action_type}")
            return False

    def _render_notify_message(self, torrent: Dict, message: Optional[str]) -> Optional[str]:
        """
        Render a notify action's message template against torrent fields

        Uses the same ${namespace.field} dot-notation conditions already
        use (info.*, trackers.*, files.*, peers.*, properties.*,
        webseeds.*, transfer.*, app.*, stats.*), resolved via self.field_resolver
        so trackers/files/peers share the same lazy-loaded, per-run cache
        the condition evaluator already populated for this torrent,
        rather than re-fetching from the qBittorrent API. A collection
        field's list result (e.g. ${trackers.url} with several trackers,
        or ${info.tags}) is comma-joined for a readable message.

        ${vars.*} and ${rule.*} tokens are NOT handled here -- those are
        already gone from the string by the time an action's params reach
        the engine, resolved once per rule at config-load time by
        resolver.py (see its module docstring).

        Args:
            torrent: Torrent dictionary
            message: Message template, e.g.
                "[${rule.id}] ${info.name} matched" -- ${rule.id} is
                already substituted away by the time this runs; only
                ${info.name} is resolved here, per-torrent

        Returns:
            Rendered message, or None if message was None or templating failed
        """
        if message is None:
            return None

        def replace(match: re.Match) -> str:
            try:
                value = self.field_resolver.get_field_value(torrent, match.group(1))
            except DataNotReadyError:
                # e.g. ${tracker.status} for a torrent trackie has no record of:
                # render it like any other missing value rather than failing
                # the whole notification
                value = None
            if isinstance(value, list):
                return ', '.join(str(v) for v in value)
            return '' if value is None else str(value)

        try:
            return ACTION_TEMPLATE_PATTERN.sub(replace, message)
        except FieldError as e:
            logger.warning(f"  notify: message template references an unknown field ({e}), skipping")
            return None

    def _execute_notify(self, torrent: Dict, params: Dict) -> bool:
        """
        Send an outbound webhook notification (Discord/Slack/ntfy/generic)

        Always fires -- no idempotency tracking, since there's no existing
        mechanism to record "already notified" state. A rule re-evaluated
        across multiple contexts will re-fire this every time it still
        matches; pair with add_tag + a condition excluding already-tagged
        torrents if that's not desired.
        """
        service = params.get('service', self.notifications_config.get('service', 'generic'))
        url = params.get('url') or self.notifications_config.get('webhook_url')

        if not url:
            logger.error("  notify: no webhook URL configured (set params.url or notifications.webhook_url)")
            return False

        message = self._render_notify_message(torrent, params.get('message'))
        if params.get('message') is not None and message is None:
            # Template rendering failed (already logged a warning) -- don't fire
            return False
        if message is None and not (service == 'generic' and 'body' in params):
            logger.error("  notify: no message provided (set params.message, or params.body for generic)")
            return False

        if service == 'discord':
            kwargs = {'json': {'content': message}}
        elif service == 'slack':
            kwargs = {'json': {'text': message}}
        elif service == 'ntfy':
            kwargs = {'data': message.encode('utf-8')}
        elif service == 'generic':
            if 'body' in params:
                kwargs = {'json': params['body']}
            else:
                kwargs = {'json': {'message': message}}
        else:
            logger.error(f"  notify: unknown service '{service}' (expected discord, slack, ntfy, or generic)")
            return False

        try:
            response = requests.post(url, timeout=10, **kwargs)
            response.raise_for_status()
            logger.info(f"  Sent {service} notification for {torrent['name']}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"  notify: failed to send {service} notification for {torrent['name']}: {e}")
            return False

    def _find_arr_queue_records(self, base_url: str, api_key: str, torrent_hash: str) -> List[Dict]:
        """
        Page through GET /api/v3/queue collecting every record whose
        downloadId matches torrent_hash (already uppercased)

        A season-pack/multi-episode download can produce multiple queue
        records sharing the same downloadId (one per episode in Sonarr) --
        all matches are collected so the caller can blocklist and search
        for every one of them, not just the first.
        """
        matches = []
        page = 1
        page_size = 250

        while True:
            response = requests.get(
                f"{base_url}/api/v3/queue",
                params={'apikey': api_key, 'page': page, 'pageSize': page_size},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            records = data.get('records', [])

            for record in records:
                if record.get('downloadId', '').upper() == torrent_hash:
                    matches.append(record)

            total_records = data.get('totalRecords', len(records))
            if not records or page * page_size >= total_records:
                return matches
            page += 1

    def _execute_arr_blocklist(self, torrent: Dict, params: Dict) -> bool:
        """
        Blocklist a torrent's Sonarr/Radarr queue entry, and optionally
        trigger a re-search, correlating via GET /api/v3/queue's
        downloadId (not category matching -- that's a separate concept,
        auto-tagging on import, see Advanced-Topics)

        Non-fatal if the torrent isn't found in the arr's queue -- most
        torrents aren't arr-managed, so this logs a warning and returns
        True (not an error) rather than failing the rule.

        params:
            service: 'sonarr' or 'radarr' (required)
            remove_from_client: bool, default False -- False assumes a
                preceding delete_torrent action already removed it from
                qBittorrent; override to True if this action runs without one
            search: bool, default True -- whether to also trigger a
                replacement search after blocklisting. Set False for a
                blocklist-only action (e.g. reviewing candidates manually
                before letting the arr re-grab anything)
        """
        service = params.get('service')
        if service not in ('sonarr', 'radarr'):
            logger.error(
                f"  arr_blocklist: params.service must be 'sonarr' or 'radarr', got {service!r}"
            )
            return False

        integration = self.integrations_config.get(service, {})
        base_url = integration.get('url')
        api_key = integration.get('api_key')
        if not base_url or not api_key:
            logger.error(
                f"  arr_blocklist: {service} is not configured "
                f"(set integrations.{service}.url and integrations.{service}.api_key)"
            )
            return False

        base_url = base_url.rstrip('/')
        torrent_hash = torrent['hash'].upper()

        try:
            records = self._find_arr_queue_records(base_url, api_key, torrent_hash)
        except requests.exceptions.RequestException as e:
            logger.error(f"  arr_blocklist: failed to query {service}'s queue: {e}")
            return False

        if not records:
            logger.warning(
                f"  arr_blocklist: {torrent['name']} not found in {service}'s queue, skipping"
            )
            return True

        remove_from_client = bool(params.get('remove_from_client', False))

        for record in records:
            try:
                response = requests.delete(
                    f"{base_url}/api/v3/queue/{record['id']}",
                    params={
                        'apikey': api_key,
                        'removeFromClient': str(remove_from_client).lower(),
                        'blocklist': 'true',
                    },
                    timeout=10
                )
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                logger.error(
                    f"  arr_blocklist: failed to blocklist queue entry "
                    f"{record.get('id')} in {service}: {e}"
                )
                return False

        if not bool(params.get('search', True)):
            logger.info(f"  Blocklisted {torrent['name']} in {service} (search=false, no search triggered)")
            return True

        if service == 'sonarr':
            episode_ids = [r['episodeId'] for r in records if 'episodeId' in r]
            command = {'name': 'EpisodeSearch', 'episodeIds': episode_ids}
        else:
            movie_ids = sorted({r['movieId'] for r in records if 'movieId' in r})
            command = {'name': 'MoviesSearch', 'movieIds': movie_ids}

        try:
            response = requests.post(
                f"{base_url}/api/v3/command",
                params={'apikey': api_key},
                json=command,
                timeout=10
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"  arr_blocklist: failed to trigger {service} search: {e}")
            return False

        logger.info(f"  Blocklisted and triggered {service} search for {torrent['name']}")
        return True


class RulesEngine:
    """Main qBittorrent automation engine"""

    def __init__(
        self,
        api: QBittorrentAPI,
        config: 'Config',
        dry_run: bool = False,
        notifications_config: Optional[Dict[str, str]] = None,
        integrations_config: Optional[Dict[str, Dict[str, str]]] = None
    ):
        """
        Initialize engine

        Args:
            api: qBittorrent API client
            config: Configuration object
            dry_run: If True, only log actions without executing
            notifications_config: Pre-resolved notifications config for the
                notify action -- see ActionExecutor.__init__
            integrations_config: Pre-resolved Sonarr/Radarr config for the
                arr_blocklist action -- see ActionExecutor.__init__
        """
        self.api = api
        self.config = config
        self.dry_run = dry_run
        self.evaluator = ConditionEvaluator(api)
        self.executor = ActionExecutor(
            api, dry_run,
            notifications_config=notifications_config,
            integrations_config=integrations_config,
            field_resolver=self.evaluator,
        )
        trackie = (integrations_config or {}).get('trackie') or {}
        if trackie.get('url'):
            self.evaluator.trackie = TrackieClient(
                trackie['url'],
                max_age_s=parse_max_age(trackie.get('max_age')),
                timeout_s=float(trackie.get('timeout') or 10),
            )
        self.stats = RuleStats()

    def run(self, context: Optional[str] = None, torrent_hash: Optional[str] = None):
        """
        Execute rules engine

        Args:
            context: Context type (torrent-imported, download-finished, weekly-cleanup, adhoc-run)
            torrent_hash: Optional torrent hash to process only one torrent
        """
        logger.info("=" * 60)
        logger.info("Starting qBittorrent automation engine")
        logger.info(f"Context: {context or 'none'}")
        logger.info(f"Dry run mode: {self.dry_run}")
        logger.info("=" * 60)

        try:
            # Fetch torrents. Always a single API call, even in single-torrent
            # (webhook) mode below -- the full list is also what stats.* is
            # computed from, so the fleet-wide aggregate is correct even when
            # only one torrent is actually being evaluated this run.
            all_torrents = self.api.get_torrents()

            if torrent_hash:
                # Single torrent mode (webhook)
                torrents = [t for t in all_torrents if t['hash'] == torrent_hash]
                if not torrents:
                    logger.warning(f"Torrent not found: {torrent_hash}")
                    return
            else:
                # All torrents mode (weekly-cleanup/adhoc-run)
                torrents = all_torrents

            self.stats.total_torrents = len(torrents)
            logger.info(f"Fetched {len(torrents)} torrent(s)")

            # stats.* namespace: a once-per-run snapshot of aggregate counts
            # and sums across ALL torrents (not just `torrents` above, which
            # may be filtered to one torrent in webhook mode). Computed from
            # data already in memory -- no additional qBittorrent API calls,
            # since state/category/tags/speeds/seeds/leechs are all already
            # present on each torrent dict from the initial /torrents/info
            # fetch (tags via the same parse_tags() helper info.tags uses).
            # dlspeed/upspeed/seeds/leechs are fleet-wide sums of the same
            # per-torrent info.dlspeed/upspeed/num_seeds/num_leechs values --
            # named after the underlying qBittorrent API fields they sum
            # (num_seeds/num_leechs), not the Web UI's "Seeds"/"Peers" column
            # labels, to avoid reading as an alias for the different
            # properties.peers concept (total connected peers, not just
            # leechers). Also not the same figures as qBittorrent's own
            # transfer.dl_info_speed/etc -- they total only the torrents this
            # run actually fetched, and won't include protocol overhead/
            # DHT traffic the global transfer figures do.
            # Snapshot semantics: this is NOT recomputed as rules run, so an
            # earlier rule's action (e.g. force_start) won't be reflected in
            # stats.* for a later rule's check within the same run.
            # tracker.* reads a fresh trackie snapshot each run, fetched on
            # first use -- a run whose rules never mention tracker.* makes no
            # request to trackie at all
            self.evaluator.reset_trackie()

            self.evaluator.stats = {
                'state': Counter(t.get('state') for t in all_torrents),
                'category': Counter(t.get('category') for t in all_torrents if t.get('category')),
                'tag': Counter(tag for t in all_torrents for tag in parse_tags(t)),
                'total': len(all_torrents),
                'dlspeed': sum(t.get('dlspeed', 0) for t in all_torrents),
                'upspeed': sum(t.get('upspeed', 0) for t in all_torrents),
                'seeds': sum(t.get('num_seeds', 0) for t in all_torrents),
                'leechs': sum(t.get('num_leechs', 0) for t in all_torrents),
            }

            # Get rules (execute in YAML file order)
            rules = self.config.get_rules()
            logger.info(f"Loaded {len(rules)} rules (execute in file order)")

            # Process each rule
            processed_torrents = set()

            for rule in rules:
                # 'id' is this rule's stable identity (required + unique --
                # see Config._load_rules). 'meta.description', if set, is
                # the human-readable text for logs; falls back to the id
                # itself when absent, since id is required and description
                # isn't.
                rule_id = rule.get('id', 'unnamed')
                rule_label = rule.get('meta', {}).get('description') or rule_id

                if not rule.get('enabled', True):
                    logger.debug(f"Skipping disabled rule: {rule_label}")
                    continue

                rule_totals = self.stats.by_rule.setdefault(
                    rule_id,
                    {'matched': 0, 'actions_executed': 0, 'actions_skipped': 0, 'errors': 0}
                )

                logger.info(f"Processing rule: {rule_label}")

                matched_count = 0
                for torrent in torrents:
                    # Skip if already processed by stop_on_match rule
                    if torrent['hash'] in processed_torrents:
                        continue

                    # Skip if torrent was deleted by a previous rule
                    if torrent.get('_deleted'):
                        continue

                    # Evaluate conditions
                    # Pass runtime context and rule's context requirement separately
                    if self.evaluator.evaluate(torrent, rule.get('conditions', {}), context, rule.get('context')):
                        matched_count += 1
                        self.stats.rules_matched += 1
                        rule_totals['matched'] += 1

                        logger.debug(f"Rule '{rule_label}' matched: {torrent.get('name', 'unknown')}")

                        # Execute actions
                        for action in rule.get('actions', []):
                            # Snapshot state/tags before executing so stats.* (the
                            # fleet-wide Counter snapshot taken once at the top of
                            # run() -- see its docstring) can be kept live for the
                            # rest of THIS run. Without this, a quota condition like
                            # stats.tag.force-seeding stays frozen at its
                            # start-of-run value all the way through the run, so a
                            # budget check that should stop admitting new torrents
                            # once full would instead admit every remaining match in
                            # one pass. Must be captured before execute() runs, not
                            # after -- execute() is what mutates the torrent (via
                            # the API call it makes), so capturing afterward would
                            # already see the post-action value as "pre".
                            pre_state = torrent.get('state')
                            pre_tags = set(parse_tags(torrent))

                            success, skipped = self.executor.execute(torrent, action)
                            if success:
                                if skipped:
                                    self.stats.actions_skipped += 1
                                    rule_totals['actions_skipped'] += 1
                                else:
                                    self.stats.actions_executed += 1
                                    rule_totals['actions_executed'] += 1

                                    # Re-fetch torrent to update cache for subsequent rules
                                    # This allows later rules to see changes (tags, category, etc.)
                                    try:
                                        updated = self.api.get_torrent(torrent['hash'])
                                        if updated:
                                            torrent.update(updated)
                                            logger.debug(f"Updated cache for {torrent.get('name', 'unknown')} after action")

                                            post_state = torrent.get('state')
                                            if post_state != pre_state:
                                                self.evaluator.stats['state'][pre_state] -= 1
                                                self.evaluator.stats['state'][post_state] += 1

                                            post_tags = set(parse_tags(torrent))
                                            for tag in post_tags - pre_tags:
                                                self.evaluator.stats['tag'][tag] += 1
                                            for tag in pre_tags - post_tags:
                                                self.evaluator.stats['tag'][tag] -= 1
                                        else:
                                            # Torrent was deleted
                                            logger.debug(f"Torrent {torrent['hash']} no longer exists (likely deleted)")
                                            torrent['_deleted'] = True

                                            self.evaluator.stats['state'][pre_state] -= 1
                                            self.evaluator.stats['total'] -= 1
                                            for tag in pre_tags:
                                                self.evaluator.stats['tag'][tag] -= 1
                                            if torrent.get('category'):
                                                self.evaluator.stats['category'][torrent['category']] -= 1
                                    except Exception as e:
                                        logger.warning(f"Failed to update cache for {torrent['hash']}: {e}")
                                        # Continue execution - don't fail the rule
                            else:
                                self.stats.errors += 1
                                rule_totals['errors'] += 1

                        # Mark as processed if stop_on_match
                        if rule.get('stop_on_match', False):
                            processed_torrents.add(torrent['hash'])

                if matched_count > 0:
                    logger.info(f"  Rule '{rule_label}' matched {matched_count} torrent(s)")

            self.stats.processed = len(processed_torrents)

        except Exception as e:
            logger.error(f"Fatal error during execution: {e}", exc_info=True)
            raise

        finally:
            self._print_summary()

    def _print_summary(self):
        """Print execution summary"""
        logger.info("=" * 60)
        logger.info("Execution complete - Summary:")
        logger.info(f"  Total torrents: {self.stats.total_torrents}")
        logger.info(f"  Processed: {self.stats.processed}")
        logger.info(f"  Rules matched: {self.stats.rules_matched}")
        logger.info(f"  Actions executed: {self.stats.actions_executed}")
        logger.info(f"  Actions skipped (idempotent): {self.stats.actions_skipped}")
        if self.stats.errors > 0:
            logger.warning(f"  Errors: {self.stats.errors}")
        logger.info("=" * 60)
