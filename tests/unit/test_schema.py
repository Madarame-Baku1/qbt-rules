"""
schema.py is a hand-written catalogue of what engine.py implements; these
tests fail the moment the two drift apart (a new operator or action added
to the engine but not the catalogue, or vice versa), so the rules editor
never rejects something the engine supports.
"""

import inspect
import json
import re

from qbt_rules import engine, schema


def engine_branches(pattern):
    return set(re.findall(pattern, inspect.getsource(engine)))


def test_every_engine_operator_is_catalogued():
    ops = engine_branches(r"operator == '([^']+)'") | {'>', '<', '>=', '<='}
    assert ops == set(schema.OPERATORS)


def test_every_engine_action_is_catalogued():
    actions = engine_branches(r"action_type == '([^']+)'")
    assert actions == set(schema.ACTION_TYPES)


def test_every_engine_field_namespace_is_catalogued():
    namespaces = engine_branches(r"endpoint == '([^']+)'")
    assert namespaces == set(schema.FIELD_NAMESPACES)


def test_notify_and_arr_services_match_engine():
    source = inspect.getsource(engine)
    for service in schema.NOTIFY_SERVICES:
        assert f"service == '{service}'" in source
    assert "service not in ('sonarr', 'radarr')" in source
    assert set(schema.ARR_SERVICES) == {'sonarr', 'radarr'}


def test_file_priorities_come_from_engine():
    choices = schema.ACTION_TYPES['set_file_priority']['params']['priority']['choices']
    assert set(engine.FILE_PRIORITIES) <= set(choices)
    assert set(engine.FILE_PRIORITIES.values()) <= set(choices)


def test_catalog_is_json_serialisable():
    data = json.loads(json.dumps(schema.catalog()))
    assert 'info' in data['fields']
    assert 'delete_torrent' in data['actions']
