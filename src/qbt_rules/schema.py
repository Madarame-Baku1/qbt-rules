"""
Catalogue of the rules.yml vocabulary the engine actually implements

One place that names every field namespace, operator and action type
engine.py understands, so the rules editor's validator (rules_validation.py)
and its autocomplete (/api/rules/catalog) don't each keep their own copy.
engine.py itself still dispatches through its own if/elif chains -- this
module doesn't drive evaluation -- so tests/unit/test_schema.py checks the
two stay in step: every operator/action/namespace listed here must appear
as a branch in engine.py, and every branch there must be listed here.

Names under the passthrough namespaces (info.*, trackers.*, files.*, ...)
come straight from qBittorrent's API responses and vary by qBittorrent
version, so they're listed only as autocomplete hints -- an unlisted name
there is never reported as a problem. tracker.* is the exception: its
names are a closed set, defined in trackie.py.
"""

from typing import Any, Dict

from qbt_rules.engine import FILE_PRIORITIES
from qbt_rules.trackie import (
    ACCOUNT_FIELDS,
    COMPUTED_ACCOUNT_FIELDS,
    COMPUTED_TORRENT_FIELDS,
    TORRENT_FIELDS,
)

# namespace -> (description, autocomplete hints). Hints are qBittorrent's
# own field names, from its WebUI API docs -- not exhaustive.
FIELD_NAMESPACES: Dict[str, Dict[str, Any]] = {
    'info': {
        'description': 'Torrent info from /torrents/info',
        'hints': [
            'name', 'hash', 'state', 'category', 'tags', 'size', 'total_size',
            'progress', 'ratio', 'added_on', 'completion_on', 'last_activity',
            'seeding_time', 'time_active', 'num_seeds', 'num_leechs',
            'num_complete', 'num_incomplete', 'dlspeed', 'upspeed',
            'downloaded', 'uploaded', 'amount_left', 'eta', 'save_path',
            'content_path', 'tracker', 'force_start', 'auto_tmm', 'priority',
            'max_ratio', 'ratio_limit', 'seen_complete',
        ],
    },
    'trackers': {
        'description': 'Tracker list (http/https trackers only); a list per torrent',
        'hints': ['url', 'status', 'tier', 'num_peers', 'num_seeds', 'num_leeches', 'num_downloaded', 'msg'],
    },
    'files': {
        'description': 'File list; a list per torrent. Rules skip torrents whose file list is not loaded yet',
        'hints': ['index', 'name', 'size', 'progress', 'priority', 'is_seed', 'availability', 'piece_range'],
    },
    'peers': {
        'description': 'Connected peers; a list per torrent',
        'hints': ['ip', 'port', 'client', 'country', 'country_code', 'dl_speed', 'up_speed', 'progress', 'flags', 'connection'],
    },
    'properties': {
        'description': 'Torrent properties from /torrents/properties',
        'hints': [
            'save_path', 'comment', 'creation_date', 'addition_date',
            'completion_date', 'share_ratio', 'seeding_time', 'time_elapsed',
            'peers', 'seeds', 'total_size',
        ],
    },
    'webseeds': {
        'description': 'Web seeds; a list per torrent',
        'hints': ['url'],
    },
    'transfer': {
        'description': 'Global transfer info, read once per run',
        'hints': ['dl_info_speed', 'up_info_speed', 'dl_info_data', 'up_info_data', 'dht_nodes', 'connection_status'],
    },
    'app': {
        'description': 'qBittorrent application preferences, read once per run',
        'hints': [],
    },
    'stats': {
        'description': 'Counts across all torrents this run; unknown names read as 0',
        'hints': ['total', 'dlspeed', 'upspeed', 'seeds', 'leechs', 'state.<state>', 'category.<category>', 'tag.<tag>'],
    },
    'tracker': {
        'description': 'Private-tracker stats from trackie (needs integrations.trackie.url)',
        'hints': (
            list(TORRENT_FIELDS) + list(COMPUTED_TORRENT_FIELDS)
            + [f'account.{name}' for name in list(ACCOUNT_FIELDS) + list(COMPUTED_ACCOUNT_FIELDS)]
        ),
    },
}

# Closed set: tracker.<name> outside this raises FieldError at runtime
TRACKER_FIELD_NAMES = frozenset(FIELD_NAMESPACES['tracker']['hints'])

# operator -> (description, the kind of value it expects). value kinds:
# 'any', 'string', 'regex', 'list', 'number', 'size', 'duration'
OPERATORS: Dict[str, Dict[str, str]] = {
    '==': {'value': 'any', 'description': 'Equal (no type coercion)'},
    '!=': {'value': 'any', 'description': 'Not equal'},
    'contains': {'value': 'string', 'description': 'Case-sensitive substring; a list value means any item'},
    'not_contains': {'value': 'string', 'description': 'No case-sensitive substring; a list value means no item'},
    'matches': {'value': 'regex', 'description': 'Regex search; prefix (?i) for case-insensitive'},
    'in': {'value': 'list', 'description': 'Value is one of the list'},
    'not_in': {'value': 'list', 'description': 'Value is none of the list'},
    '>': {'value': 'number', 'description': 'Greater than (numeric)'},
    '<': {'value': 'number', 'description': 'Less than (numeric)'},
    '>=': {'value': 'number', 'description': 'Greater than or equal (numeric)'},
    '<=': {'value': 'number', 'description': 'Less than or equal (numeric)'},
    'larger_than': {'value': 'size', 'description': "Size above, e.g. '10 GB' (lowercase b means bits)"},
    'smaller_than': {'value': 'size', 'description': "Size below, e.g. '500 MiB'"},
    'older_than': {'value': 'duration', 'description': "Timestamp older than, e.g. '30 days'"},
    'newer_than': {'value': 'duration', 'description': "Timestamp newer than, e.g. '12 hours'"},
}

NOTIFY_SERVICES = ('discord', 'slack', 'ntfy', 'generic')
ARR_SERVICES = ('sonarr', 'radarr')

# action type -> params. Each param: type ('bool', 'string', 'int',
# 'list', 'regex', 'enum', 'template', 'any'), required, default, and
# choices for enums.
ACTION_TYPES: Dict[str, Dict[str, Any]] = {
    'stop': {'description': 'Stop (pause) the torrent', 'params': {}},
    'start': {'description': 'Start (resume) the torrent', 'params': {}},
    'force_start': {'description': 'Force-start the torrent', 'params': {}},
    'release_force_start': {'description': 'Clear force-start', 'params': {}},
    'recheck': {'description': 'Recheck torrent data', 'params': {}},
    'reannounce': {'description': 'Reannounce to trackers', 'params': {}},
    'delete_torrent': {
        'description': 'Delete the torrent',
        'params': {'delete_files': {'type': 'bool', 'default': True, 'description': 'Also delete downloaded data'}},
    },
    'set_category': {
        'description': 'Set the category',
        'params': {'category': {'type': 'string', 'default': '', 'description': "Category name ('' clears it)"}},
    },
    'add_tag': {
        'description': 'Add tags',
        'params': {'tags': {'type': 'list', 'required': True, 'description': 'Tags to add'}},
    },
    'remove_tag': {
        'description': 'Remove tags',
        'params': {'tags': {'type': 'list', 'required': True, 'description': 'Tags to remove'}},
    },
    'set_tags': {
        'description': 'Replace all tags',
        'params': {'tags': {'type': 'list', 'required': True, 'description': 'The complete new tag list'}},
    },
    'set_upload_limit': {
        'description': 'Set the upload limit',
        'params': {'limit': {'type': 'int', 'default': -1, 'description': 'Bytes per second, -1 for unlimited'}},
    },
    'set_download_limit': {
        'description': 'Set the download limit',
        'params': {'limit': {'type': 'int', 'default': -1, 'description': 'Bytes per second, -1 for unlimited'}},
    },
    'increase_priority': {'description': 'Move up the queue', 'params': {}},
    'decrease_priority': {'description': 'Move down the queue', 'params': {}},
    'set_top_priority': {'description': 'Move to the top of the queue', 'params': {}},
    'set_bottom_priority': {'description': 'Move to the bottom of the queue', 'params': {}},
    'set_file_priority': {
        'description': 'Set file download priority',
        'params': {
            'priority': {
                'type': 'enum', 'required': True,
                'choices': list(FILE_PRIORITIES) + sorted(set(FILE_PRIORITIES.values())),
                'description': 'skip/normal/high/maximum or 0/1/6/7',
            },
            'pattern': {'type': 'regex', 'description': 'Only files whose name matches; all files if absent'},
        },
    },
    'notify': {
        'description': 'Send a notification',
        'params': {
            'service': {'type': 'enum', 'choices': list(NOTIFY_SERVICES), 'description': 'Defaults to notifications.service'},
            'url': {'type': 'string', 'description': 'Webhook URL; defaults to notifications.webhook_url'},
            'message': {'type': 'template', 'description': 'Message text; ${info.name} etc. are filled per torrent'},
            'body': {'type': 'any', 'description': 'generic only: JSON body posted as-is (not templated)'},
        },
    },
    'arr_blocklist': {
        'description': 'Blocklist the release in Sonarr/Radarr',
        'params': {
            'service': {'type': 'enum', 'required': True, 'choices': list(ARR_SERVICES), 'description': 'Which *arr to use'},
            'remove_from_client': {'type': 'bool', 'default': False, 'description': 'Also remove from qBittorrent'},
            'search': {'type': 'bool', 'default': True, 'description': 'Search for a replacement'},
        },
    },
}

# Keys a rule may carry. Anything else is accepted but ignored by the engine.
RULE_KEYS: Dict[str, str] = {
    'id': 'Unique, stable identifier (required)',
    'enabled': 'false to skip this rule (default true)',
    'context': 'Only run in this context (string or list); absent means every context',
    'stop_on_match': 'Later rules skip a torrent this rule matched',
    'meta': 'Descriptive metadata, e.g. meta.description',
    'conditions': 'all/any/none groups of field/operator/value leaves (required)',
    'actions': 'Actions to run on a match (required)',
}

TOP_LEVEL_KEYS: Dict[str, str] = {
    'version': 'Schema marker (default 1)',
    'refs': 'Reusable vars, conditions and actions',
    'instances': 'Per-instance overrides (not used yet)',
    'rules': 'The rules, run top to bottom',
}

CONDITION_GROUPS = ('all', 'any', 'none')


def catalog() -> Dict[str, Any]:
    """The whole vocabulary as one JSON-serialisable dict, for the editor"""
    return {
        'top_level_keys': TOP_LEVEL_KEYS,
        'rule_keys': RULE_KEYS,
        'condition_groups': list(CONDITION_GROUPS),
        'fields': FIELD_NAMESPACES,
        'operators': OPERATORS,
        'actions': ACTION_TYPES,
    }
