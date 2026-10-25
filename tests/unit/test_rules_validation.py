"""
Tests for rules_validation.py - the rules editor's validator

Covers the three layers (loader-equivalent structure, $ref/var resolution,
lint) and that every issue lands on the right line of the document.
"""

from pathlib import Path
from textwrap import dedent, indent

import pytest

from qbt_rules.rules_validation import check_rules_structure, validate_rules_text

REPO = Path(__file__).resolve().parents[2]


def validate(text):
    return validate_rules_text(dedent(text).lstrip('\n'))


def messages(result, severity=None):
    return [i['message'] for i in result['issues'] if severity is None or i['severity'] == severity]


def find(result, needle):
    """The single issue whose message contains needle"""
    hits = [i for i in result['issues'] if needle in i['message']]
    assert len(hits) == 1, f"expected one issue containing {needle!r}, got {messages(result)}"
    return hits[0]


VALID_RULE = '''
rules:
  - id: tidy
    conditions:
      all:
        - field: info.ratio
          operator: ">="
          value: 2
    actions:
      - type: stop
'''


class TestShippedFiles:
    @pytest.mark.parametrize('path', ['config/rules.default.yml', 'advanced-rules-example.yml'])
    def test_shipped_rule_files_are_clean(self, path):
        result = validate_rules_text((REPO / path).read_text())
        assert result['valid'], messages(result)
        assert result['counts'] == {'error': 0, 'warning': 0, 'info': 0}

    def test_minimal_rule_is_valid(self):
        result = validate(VALID_RULE)
        assert result['valid']
        assert result['issues'] == []


class TestSyntax:
    def test_yaml_syntax_error_has_position(self):
        result = validate('''
            rules:
              - id: a
                conditions: [
        ''')
        assert not result['valid']
        [issue] = result['issues']
        assert issue['message'].startswith('YAML syntax error')
        assert issue['line'] is not None

    def test_empty_file(self):
        result = validate_rules_text('# just a comment\n')
        assert messages(result) == ['File is empty']

    def test_duplicate_key_is_flagged_on_the_key(self):
        result = validate('''
            rules:
              - id: a
                conditions: []
                actions: []
                actions:
                  - type: stop
        ''')
        issue = find(result, "Duplicate key 'actions'")
        assert issue['severity'] == 'warning'
        assert issue['line'] == 5


class TestStructure:
    """Layer 1 must match exactly what Config._load_rules() rejects"""

    @pytest.mark.parametrize('raw, expected', [
        ([], "rules.yml must be a mapping"),
        ({'rules': {}}, "'rules' must be a list"),
        ({'rules': ['x']}, "Rule #1 must be a dictionary"),
        ({'rules': [{'conditions': [], 'actions': []}]}, "Rule #1 missing required field: 'id'"),
        ({'rules': [{'id': '', 'conditions': [], 'actions': []}]}, "Rule #1 has an invalid 'id'"),
        ({'rules': [{'id': 'a', 'conditions': [], 'actions': []}] * 2}, "Duplicate rule id: 'a'"),
        ({'rules': [{'id': 'a', 'actions': []}]}, "Rule 'a' missing required field: 'conditions'"),
        ({'rules': [{'id': 'a', 'conditions': []}]}, "Rule 'a' missing required field: 'actions'"),
        ({'refs': [], 'rules': []}, "'refs' must be a mapping"),
        ({'refs': {'vars': []}, 'rules': []}, "'refs.vars' must be a mapping"),
    ])
    def test_loader_errors(self, raw, expected):
        problems = check_rules_structure(raw)
        assert problems and expected in problems[0].message

    def test_empty_refs_groups_are_allowed(self):
        assert check_rules_structure({'refs': None, 'rules': []}) == []
        assert check_rules_structure({'refs': {'vars': None, 'conditions': None}, 'rules': []}) == []

    def test_structure_error_points_at_the_rule(self):
        result = validate('''
            rules:
              - id: fine
                conditions: []
                actions: []
              - id: broken
                actions: []
        ''')
        issue = find(result, "missing required field: 'conditions'")
        assert issue['line'] == 5
        assert issue['path'] == 'rules[1]'


class TestResolution:
    def test_unknown_ref_points_at_the_ref(self):
        result = validate('''
            rules:
              - id: a
                conditions:
                  all:
                    - $ref: conditions.missing
                actions:
                  - type: stop
        ''')
        issue = find(result, 'Unknown reference: conditions.missing')
        assert issue['severity'] == 'error'
        assert issue['line'] == 5
        assert "rule 'a'" in issue['message']

    def test_unknown_var_inside_a_ref_block_is_located_in_refs(self):
        result = validate('''
            refs:
              conditions:
                old:
                  all:
                    - field: info.added_on
                      operator: older_than
                      value: ${vars.nope}
            rules:
              - id: a
                conditions: {$ref: conditions.old}
                actions: [{type: stop}]
              - id: b
                conditions: {$ref: conditions.old}
                actions: [{type: stop}]
        ''')
        issue = find(result, 'Unknown variable')
        assert issue['line'] == 7
        # Reported once, naming every rule it breaks
        assert "'a'" in issue['message'] and "'b'" in issue['message']

    def test_circular_ref(self):
        result = validate('''
            refs:
              conditions:
                x: {all: [{$ref: conditions.y}]}
                y: {all: [{$ref: conditions.x}]}
            rules:
              - id: a
                conditions: {$ref: conditions.x}
                actions: [{type: stop}]
        ''')
        assert any('Circular reference' in m for m in messages(result, 'error'))


class TestConditionLint:
    def rule_with(self, condition_yaml):
        return validate(f'''
            rules:
              - id: a
                conditions:
                  all:
                    - {condition_yaml}
                actions:
                  - type: stop
        ''')

    def test_unknown_operator(self):
        issue = find(self.rule_with('{field: info.ratio, operator: bigger, value: 1}'), "Unknown operator 'bigger'")
        assert issue['severity'] == 'error'
        assert issue['line'] == 5

    def test_unknown_namespace(self):
        result = self.rule_with('{field: torrent.ratio, operator: "==", value: 1}')
        assert find(result, "Unknown field namespace 'torrent'")['severity'] == 'error'

    def test_unknown_trackie_field(self):
        result = self.rule_with('{field: tracker.nonsense, operator: "==", value: 1}')
        assert find(result, "Unknown trackie field")['severity'] == 'error'

    def test_known_trackie_fields_pass(self):
        assert self.rule_with('{field: tracker.account.ratio_ok, operator: "==", value: true}')['valid']

    def test_short_duration_is_an_error(self):
        """'7d' parses as 0 seconds -- older_than would then match everything"""
        issue = find(self.rule_with("{field: info.added_on, operator: older_than, value: '7d'}"), 'not a duration')
        assert issue['severity'] == 'error'

    def test_long_duration_passes(self):
        assert self.rule_with("{field: info.added_on, operator: older_than, value: '7 days'}")['valid']

    def test_embedded_var_is_substituted_before_checking(self):
        result = validate('''
            refs:
              vars:
                days: 30
            rules:
              - id: a
                conditions:
                  all:
                    - field: info.added_on
                      operator: older_than
                      value: "${vars.days} days"
                actions: [{type: stop}]
        ''')
        assert result['valid'], messages(result)

    def test_bits_unit_warns(self):
        issue = find(self.rule_with("{field: info.size, operator: larger_than, value: '10 Gb'}"), 'means bits')
        assert issue['severity'] == 'warning'

    def test_bad_regex(self):
        result = self.rule_with("{field: info.name, operator: matches, value: '(unclosed'}")
        assert find(result, 'Invalid regular expression')['severity'] == 'error'

    def test_non_numeric_comparison(self):
        result = self.rule_with("{field: info.ratio, operator: '>', value: lots}")
        assert find(result, 'needs a number')['severity'] == 'error'

    def test_missing_value(self):
        result = self.rule_with("{field: info.ratio, operator: '>'}")
        assert find(result, "missing 'value'")['severity'] == 'error'

    def test_runtime_token_in_condition_value_warns(self):
        result = self.rule_with("{field: info.category, operator: '==', value: '${info.tags}'}")
        assert find(result, 'Runtime tokens')['severity'] == 'warning'

    def test_nested_group_with_two_operators_is_an_error(self):
        """Only the first of all/any/none is evaluated in a nested group"""
        result = validate('''
            rules:
              - id: a
                conditions:
                  all:
                    - any:
                        - {field: info.ratio, operator: ">", value: 1}
                      none:
                        - {field: info.category, operator: "==", value: keep}
                actions: [{type: stop}]
        ''')
        issue = find(result, "only 'any' is")
        assert issue['severity'] == 'error'

    def test_top_level_conditions_without_a_group_match_everything(self):
        result = validate('''
            rules:
              - id: a
                conditions:
                  field: info.ratio
                  operator: ">"
                  value: 1
                actions: [{type: stop}]
        ''')
        assert any('matches every torrent' in m for m in messages(result, 'error'))

    def test_empty_conditions_list_warns(self):
        result = validate('''
            rules:
              - id: a
                conditions: []
                actions: [{type: stop}]
        ''')
        assert find(result, 'match every torrent')['severity'] == 'warning'


class TestRuleLint:
    def test_quoted_false_enabled_is_an_error(self):
        result = validate('''
            rules:
              - id: a
                enabled: "false"
                conditions: []
                actions: [{type: stop}]
        ''')
        issue = find(result, 'counts as TRUE')
        assert issue['severity'] == 'error'
        assert issue['line'] == 3

    def test_meta_must_be_a_mapping(self):
        result = validate('''
            rules:
              - id: a
                meta: just text
                conditions: []
                actions: [{type: stop}]
        ''')
        assert find(result, "'meta' must be a mapping")['severity'] == 'error'

    def test_unknown_top_level_key_warns(self):
        result = validate(VALID_RULE + 'extras: 1\n')
        assert find(result, "Unknown top-level key 'extras'")['severity'] == 'warning'


class TestActionLint:
    def rule_with_actions(self, actions_yaml):
        head = 'rules:\n  - id: a\n    conditions: []\n    actions:\n'
        return validate_rules_text(head + indent(dedent(actions_yaml).strip('\n'), '      ') + '\n')

    def test_action_without_type(self):
        result = self.rule_with_actions('''
                  - params: {tags: [x]}
        ''')
        assert find(result, "no 'type'")['severity'] == 'error'

    def test_unknown_action_type(self):
        result = self.rule_with_actions('''
                  - type: explode
        ''')
        assert find(result, "Unknown action type 'explode'")['severity'] == 'error'

    def test_delete_files_string_false_is_an_error(self):
        result = self.rule_with_actions('''
                  - type: delete_torrent
                    params: {delete_files: "false"}
        ''')
        assert find(result, 'counts as TRUE')['severity'] == 'error'

    def test_keep_files_warns_about_the_ignored_parameter(self):
        """The real-world mistake: keep_files is ignored and files ARE deleted"""
        result = self.rule_with_actions('''
                  - type: delete_torrent
                    params: {keep_files: true}
        ''')
        assert find(result, 'files ARE deleted')['severity'] == 'warning'
        assert result['valid']

    def test_unknown_param_warns(self):
        result = self.rule_with_actions('''
                  - type: set_category
                    params: {category: x, colour: red}
        ''')
        assert find(result, "no param 'colour'")['severity'] == 'warning'

    def test_required_param(self):
        result = self.rule_with_actions('''
                  - type: set_file_priority
                    params: {pattern: '\\.nfo$'}
        ''')
        assert find(result, 'needs params.priority')['severity'] == 'error'

    def test_enum_param(self):
        result = self.rule_with_actions('''
                  - type: arr_blocklist
                    params: {service: lidarr}
        ''')
        assert find(result, 'must be one of')['severity'] == 'error'

    def test_notify_needs_a_message(self):
        result = self.rule_with_actions('''
                  - type: notify
                    params: {service: discord}
        ''')
        assert find(result, 'notify needs params.message')['severity'] == 'error'

    def test_generic_notify_with_body_is_fine(self):
        result = self.rule_with_actions('''
                  - type: notify
                    params: {service: generic, body: {text: hi}}
        ''')
        assert result['valid'], messages(result)

    def test_generic_notify_body_uses_the_default_service(self):
        result = self.rule_with_actions('''
                  - type: notify
                    params: {body: [1, 2]}
        ''')
        assert result['valid'], messages(result)

    @pytest.mark.parametrize('action_type', ['[stop]', '{bad: stop}'])
    def test_unhashable_action_type_is_a_diagnostic(self, action_type):
        result = self.rule_with_actions(f'- type: {action_type}')
        assert find(result, 'Unknown action type')['severity'] == 'error'

    def test_tags_as_string_warns(self):
        result = self.rule_with_actions('''
                  - type: add_tag
                    params: {tags: seeding}
        ''')
        assert find(result, 'should be a list')['severity'] == 'warning'

    def test_actions_must_be_a_list(self):
        result = validate('''
            rules:
              - id: a
                conditions: []
                actions:
        ''')
        assert find(result, "'actions' must be a list")['severity'] == 'error'


class TestRefsLint:
    def test_unused_refs_are_info(self):
        refs = dedent('''
            refs:
              vars:
                spare: 1
              conditions:
                unused: {all: [{field: info.ratio, operator: ">", value: 1}]}
        ''')
        result = validate_rules_text(refs + VALID_RULE)
        assert result['valid']
        infos = messages(result, 'info')
        assert any("'conditions.unused' is not used" in m for m in infos)
        assert any("'vars.spare' is not used" in m for m in infos)

    def test_ref_blocks_are_linted_where_defined(self):
        result = validate('''
            refs:
              actions:
                bad:
                  - type: delete_torrent
                    params: {delete_files: "no"}
            rules:
              - id: a
                conditions: []
                actions: [{$ref: actions.bad}]
        ''')
        issue = find(result, 'counts as TRUE')
        assert issue['line'] == 5


class TestSummary:
    def test_outline(self):
        result = validate('''
            refs:
              vars:
                r: 1
            rules:
              - id: first
                meta: {description: The first}
                context: nightly
                conditions: [{field: info.ratio, operator: ">", value: "${vars.r}"}]
                actions: [{type: stop}]
              - id: second
                enabled: false
                stop_on_match: true
                context: [hourly, nightly]
                conditions: []
                actions: [{type: start}]
        ''')
        summary = result['summary']
        assert [r['id'] for r in summary['rules']] == ['first', 'second']
        assert summary['rules'][0]['line'] == 5
        assert summary['rules'][0]['description'] == 'The first'
        assert summary['rules'][1]['enabled'] is False
        assert summary['rules'][1]['stop_on_match'] is True
        assert summary['contexts'] == ['hourly', 'nightly']
        assert summary['refs']['vars'] == [{'name': 'r', 'line': 3}]
