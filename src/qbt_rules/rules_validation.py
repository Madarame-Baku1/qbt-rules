"""
rules.yml validation for the web rules editor

Three layers, each stricter than the last:

1. check_rules_structure() -- exactly what Config._load_rules() enforces when
   it loads rules.yml (and on every hot-reload). Config raises the first of
   these as a ConfigurationError; the editor reports all of them. Sharing
   the one function is what keeps "the editor accepted it" and "the server
   loads it" from drifting apart.
2. Resolution -- every rule run through RuleResolver, the same as
   Config.get_rules() does on first use. Catches unknown/circular $refs and
   unknown ${vars.*}/${rule.*} before they can fail every job.
3. Lint -- problems the loader accepts but the engine only discovers while
   a job runs: unknown operators/actions/fields, durations that silently
   parse as 0 seconds, `delete_files: "false"` (a truthy string), an
   action with no `type` (fails the whole job), and so on. Severity
   follows consequence: anything that crashes a job, can never work, or
   makes a rule act on torrents it wasn't meant to is an error; things
   that are merely suspicious are warnings.

Errors block a save; warnings and info don't. validate_rules_text() runs
all three and attaches a 1-based line/column to each issue, found by
walking yaml.compose()'s node tree -- PyYAML is used for both, so the
positions describe exactly the document the server will load (ruamel.yaml
would parse some scalars differently -- YAML 1.2 vs 1.1).
"""

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

from qbt_rules import schema
from qbt_rules.errors import ResolverError
from qbt_rules.resolver import TOKEN_PATTERN, RuleResolver

ERROR = 'error'
WARNING = 'warning'
INFO = 'info'

Path = Tuple[Any, ...]

# Mirrors utils.parse_duration's own pattern -- anything that doesn't match
# it parses as 0 seconds
_DURATION_PATTERN = re.compile(r'(\d+)\s*(second|minute|hour|day|week|month|year)s?')
_SIZE_PATTERN = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)?\s*$')
_SIZE_UNITS = {
    prefix + kind
    for prefix in ('', 'k', 'K', 'm', 'M', 'g', 'G', 't', 'T', 'p', 'P', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi')
    for kind in ('B', 'b')
}
_WHOLE_VAR_TOKEN = re.compile(r'^\$\{vars\.([^}]+)\}$')
_VAR_TOKEN = re.compile(r'\$\{vars\.([^}]+)\}')
_RUNTIME_TOKEN = re.compile(r'\$\{(?!vars\.|rule\.)\w+(?:\.\w+)+\}')
_FALSY_STRINGS = {'false', 'no', 'off', '0', 'n'}


@dataclass
class Issue:
    """One validation finding, optionally pinned to a node of the document"""
    severity: str
    message: str
    path: Path = ()
    # Point at the mapping key rather than its value (e.g. an unknown key)
    on_key: bool = False
    line: Optional[int] = None
    column: Optional[int] = None
    end_line: Optional[int] = None
    end_column: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop('on_key')
        d['path'] = format_path(self.path)
        return d


def format_path(path: Path) -> str:
    """('rules', 2, 'actions', 0) -> 'rules[2].actions[0]'"""
    out = ''
    for part in path:
        if isinstance(part, int):
            out += f'[{part}]'
        else:
            out += f'.{part}' if out else str(part)
    return out


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

class Locator:
    """
    Maps a data path to the YAML node it came from

    Built from yaml.compose(), so it sees the same document safe_load()
    does. A path that doesn't exist in the node tree (a missing key, or
    anything reached through a merge key) falls back to its nearest
    ancestor that does, so every issue still lands somewhere sensible.
    Also collects duplicate mapping keys along the way -- PyYAML silently
    keeps the last one, which is easy to do by accident in a long file.
    """

    def __init__(self, root: Optional[yaml.Node]):
        self.values: Dict[Path, yaml.Node] = {}
        self.keys: Dict[Path, yaml.Node] = {}
        self.duplicates: List[Issue] = []
        if root is not None:
            self._walk(root, (), set())

    def _walk(self, node: yaml.Node, path: Path, stack: set):
        if id(node) in stack:
            return  # recursive alias
        self.values[path] = node
        stack = stack | {id(node)}

        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                key = key_node.value
                if key in seen:
                    self.duplicates.append(Issue(
                        WARNING,
                        f"Duplicate key '{key}' -- only the last one is used",
                        path + (key,), on_key=True,
                        **_span(key_node),
                    ))
                seen.add(key)
                self.keys[path + (key,)] = key_node
                self._walk(value_node, path + (key,), stack)

        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                self._walk(item, path + (i,), stack)

    def node(self, path: Path, on_key: bool = False) -> Optional[yaml.Node]:
        if on_key and path in self.keys:
            return self.keys[path]
        while True:
            if path in self.values:
                return self.values[path]
            if not path:
                return None
            path = path[:-1]

    def place(self, issue: Issue) -> Issue:
        if issue.line is None:
            node = self.node(issue.path, issue.on_key)
            if node is not None:
                for k, v in _span(node).items():
                    setattr(issue, k, v)
        return issue

    def line_of(self, path: Path) -> Optional[int]:
        node = self.values.get(path)
        return node.start_mark.line + 1 if node is not None else None

    def find_scalar(self, path: Path, needle: str) -> Optional[Path]:
        """First path at or under `path` whose scalar value contains needle"""
        for p, node in self.values.items():
            if p[:len(path)] == path and isinstance(node, yaml.ScalarNode) and needle in str(node.value):
                return p
        return None


def _span(node: yaml.Node) -> Dict[str, int]:
    """1-based line/column range of a node. A block mapping/sequence's
    end mark sits at the start of the following line, so only its first
    line is used -- underlining an entire rule would be noise."""
    start, end = node.start_mark, node.end_mark
    if isinstance(node, (yaml.MappingNode, yaml.SequenceNode)) or end.line != start.line:
        end_line, end_col = start.line + 1, None
    else:
        end_line, end_col = end.line + 1, end.column + 1
    return {
        'line': start.line + 1,
        'column': start.column + 1,
        'end_line': end_line,
        'end_column': end_col,
    }


# ---------------------------------------------------------------------------
# Layer 1: what the loader enforces
# ---------------------------------------------------------------------------

def check_rules_structure(raw: Any) -> List[Issue]:
    """
    The checks Config._load_rules() applies to a parsed rules.yml, as a list
    of error Issues (empty means the loader will accept it). Messages are
    the loader's own, word for word.
    """
    if not isinstance(raw, dict):
        return [Issue(ERROR, "rules.yml must be a mapping (expected keys like 'rules:' and 'refs:')")]

    issues: List[Issue] = []

    # An empty `refs:` (or group) parses as None, which means "none defined"
    refs = raw.get('refs')
    if refs is not None and not isinstance(refs, dict):
        issues.append(Issue(ERROR, "'refs' must be a mapping", ('refs',)))
    elif isinstance(refs, dict):
        for group in ('vars', 'conditions', 'actions'):
            if refs.get(group) is not None and not isinstance(refs[group], dict):
                issues.append(Issue(ERROR, f"'refs.{group}' must be a mapping", ('refs', group)))

    rules = raw.get('rules', [])
    if not isinstance(rules, list):
        issues.append(Issue(ERROR, "'rules' must be a list", ('rules',)))
        return issues

    seen_ids = set()
    for i, rule in enumerate(rules):
        path = ('rules', i)
        if not isinstance(rule, dict):
            issues.append(Issue(ERROR, f"Rule #{i+1} must be a dictionary", path))
            continue

        if 'id' not in rule:
            issues.append(Issue(ERROR, f"Rule #{i+1} missing required field: 'id'", path))
            continue

        if not isinstance(rule['id'], str) or not rule['id']:
            issues.append(Issue(ERROR, f"Rule #{i+1} has an invalid 'id': must be a non-empty string", path + ('id',)))
            continue

        # id is this rule's stable identity everywhere (by_rule stats,
        # ${rule.id} templating, the editor's outline and deep links) -- a
        # silent collision would merge two different rules' history/state
        # under one key, so it's rejected at load time rather than left as
        # an easy-to-miss footgun.
        if rule['id'] in seen_ids:
            issues.append(Issue(
                ERROR, f"Duplicate rule id: '{rule['id']}' -- rule ids must be unique", path + ('id',),
            ))
        seen_ids.add(rule['id'])

        for required in ('conditions', 'actions'):
            if required not in rule:
                issues.append(Issue(ERROR, f"Rule '{rule['id']}' missing required field: '{required}'", path))

    return issues


# ---------------------------------------------------------------------------
# Layer 2: $ref / ${vars.*} / ${rule.*} resolution
# ---------------------------------------------------------------------------

def _resolver_needle(e: ResolverError) -> Optional[str]:
    """The text in the document an error is about, to find where it is"""
    if e.code in ('REF-002', 'REF-004'):
        return str(e.details.get('Reference', '')) or None
    if e.code == 'VAR-002':
        return str(e.details.get('Variable', '')) or None
    if e.code == 'RULEFIELD-002':
        return f"rule.{e.details.get('Field')}"
    if e.code in ('REF-001', 'VAR-001', 'RULEFIELD-001') and ': ' in e.message:
        return e.message.split(': ', 1)[1]
    return None


def check_resolution(raw: Dict[str, Any], locator: Locator) -> List[Issue]:
    """Resolve every rule exactly as Config.get_rules() would"""
    refs = raw.get('refs') or {}
    try:
        resolver = RuleResolver(refs=refs, instance_id=None, instances=raw.get('instances') or {})
    except Exception as e:  # malformed refs already reported by layer 1
        return [Issue(ERROR, f"Cannot build resolver: {e}", ('refs',))]

    # The same broken $ref/var can surface once per rule using it -- group
    # those into one issue listing every affected rule
    grouped: Dict[Tuple[Path, str], List[str]] = {}
    for i, rule in enumerate(raw.get('rules') or []):
        if not isinstance(rule, dict) or not isinstance(rule.get('id'), str):
            continue
        try:
            resolver.resolve_rule(rule)
        except ResolverError as e:
            needle = _resolver_needle(e)
            path = ('rules', i)
            if needle:
                path = (locator.find_scalar(('rules', i), needle)
                        or locator.find_scalar(('refs',), needle)
                        or path)
            text = e.message
            if e.fix:
                text += f" -- {e.fix}"
            grouped.setdefault((path, text), []).append(rule['id'])
        except Exception as e:
            grouped.setdefault((('rules', i), f"Cannot resolve rule: {e}"), []).append(rule['id'])

    issues = []
    for (path, text), rule_ids in grouped.items():
        who = f"rule '{rule_ids[0]}'" if len(rule_ids) == 1 else f"rules {', '.join(repr(r) for r in rule_ids)}"
        issues.append(Issue(ERROR, f"{text} (in {who})", path))
    return issues


# ---------------------------------------------------------------------------
# Layer 3: lint
# ---------------------------------------------------------------------------

class _Linter:
    def __init__(self, raw: Dict[str, Any], notifications: Optional[Dict[str, Any]] = None):
        self.raw = raw
        self.notifications = notifications
        refs = raw.get('refs') if isinstance(raw.get('refs'), dict) else {}
        self.vars = refs.get('vars') if isinstance(refs.get('vars'), dict) else {}
        self.issues: List[Issue] = []

    def add(self, severity: str, message: str, path: Path, on_key: bool = False):
        self.issues.append(Issue(severity, message, path, on_key=on_key))

    def effective(self, value: Any) -> Tuple[Any, bool]:
        """A whole-string ${vars.x} becomes the var's value (it keeps its
        type at load time); returns (value, known) -- known is False for a
        var that doesn't exist, which resolution already reports"""
        if not isinstance(value, str) or '${' not in value:
            return value, True
        m = _WHOLE_VAR_TOKEN.match(value)
        if m:
            return (self.vars[m.group(1)], True) if m.group(1) in self.vars else (value, False)
        if '${rule.' in value:
            return value, False

        # Embedded tokens interpolate as text, exactly as the resolver does
        unknown = []

        def substitute(match):
            name = match.group(1)[len('vars.'):]
            if name not in self.vars:
                unknown.append(name)
                return match.group(0)
            return str(self.vars[name])

        return TOKEN_PATTERN.sub(substitute, value), not unknown

    def bool_value(self, raw_value: Any, label: str, path: Path):
        """The engine only tests truthiness, so a quoted "false" is true"""
        value, known = self.effective(raw_value)
        if not known or isinstance(value, bool):
            return
        if isinstance(value, str) and value.strip().lower() in _FALSY_STRINGS:
            self.add(ERROR, f"{label} is the string {value!r}, which counts as TRUE -- "
                            f"write false without quotes", path)
        else:
            self.add(WARNING, f"{label} should be true or false", path)

    # -- top level ----------------------------------------------------------

    def run(self) -> List[Issue]:
        for key in self.raw:
            if key not in schema.TOP_LEVEL_KEYS:
                self.add(WARNING, f"Unknown top-level key '{key}' (ignored)", (key,), on_key=True)

        refs = self.raw.get('refs')
        if isinstance(refs, dict):
            for key in refs:
                if key not in ('vars', 'conditions', 'actions', 'meta'):
                    self.add(WARNING, f"Unknown refs group '{key}' (ignored)", ('refs', key), on_key=True)
            conditions = refs.get('conditions')
            if isinstance(conditions, dict):
                for name, block in conditions.items():
                    self.condition_block(block, ('refs', 'conditions', name))
            actions = refs.get('actions')
            if isinstance(actions, dict):
                for name, block in actions.items():
                    self.action_list(block, ('refs', 'actions', name))
            self.unused_refs(refs)

        rules = self.raw.get('rules')
        if isinstance(rules, list):
            for i, rule in enumerate(rules):
                if isinstance(rule, dict):
                    self.rule(rule, ('rules', i))
        return self.issues

    def unused_refs(self, refs: Dict[str, Any]):
        used_vars, used_refs = set(), set()
        for node in _walk_values([self.raw.get('rules'), refs.get('conditions'), refs.get('actions')]):
            if isinstance(node, str):
                used_vars.update(_VAR_TOKEN.findall(node))
            elif isinstance(node, dict) and isinstance(node.get('$ref'), str):
                used_refs.add(node['$ref'])

        for group in ('conditions', 'actions'):
            block = refs.get(group)
            if isinstance(block, dict):
                for name in block:
                    if f'{group}.{name}' not in used_refs:
                        self.add(INFO, f"'{group}.{name}' is not used by any rule", ('refs', group, name), on_key=True)
        if isinstance(refs.get('vars'), dict):
            for name in refs['vars']:
                if name not in used_vars:
                    self.add(INFO, f"'vars.{name}' is not used anywhere", ('refs', 'vars', name), on_key=True)

    # -- rules --------------------------------------------------------------

    def rule(self, rule: Dict[str, Any], path: Path):
        for key in rule:
            if key not in schema.RULE_KEYS:
                self.add(INFO, f"Unknown rule key '{key}' (ignored by the engine)", path + (key,), on_key=True)

        for flag in ('enabled', 'stop_on_match'):
            if flag in rule:
                self.bool_value(rule[flag], f"'{flag}'", path + (flag,))

        if 'meta' in rule and not isinstance(rule['meta'], dict):
            self.add(ERROR, "'meta' must be a mapping (e.g. meta: {description: ...}) -- "
                            "anything else fails every job", path + ('meta',))

        context = rule.get('context')
        if context is not None and not isinstance(context, str) and not (
                isinstance(context, list) and all(isinstance(c, str) for c in context)):
            self.add(WARNING, "'context' should be a string or a list of strings", path + ('context',))

        if 'conditions' in rule:
            self.top_conditions(rule['conditions'], path + ('conditions',))
        if 'actions' in rule:
            self.action_list(rule['actions'], path + ('actions',), in_rule=True)

    # -- conditions ---------------------------------------------------------

    def top_conditions(self, node: Any, path: Path):
        """A rule's `conditions:` -- a list (implicit all) or a dict whose
        all/any/none keys are each evaluated and ANDed"""
        if node is None:
            self.add(ERROR, "'conditions' is empty -- this rule can never match "
                            "(use [] deliberately to match every torrent)", path)
        elif isinstance(node, list):
            if not node:
                self.add(WARNING, "Empty conditions match every torrent", path)
            for i, child in enumerate(node):
                self.condition(child, path + (i,))
        elif isinstance(node, dict):
            if '$ref' in node:
                return
            groups = [g for g in schema.CONDITION_GROUPS if g in node]
            for key in node:
                if key not in schema.CONDITION_GROUPS:
                    self.add(WARNING, f"'{key}' is ignored here -- top-level conditions only read all/any/none",
                             path + (key,), on_key=True)
            if not groups:
                self.add(ERROR, "No all/any/none group, so this rule matches every torrent -- "
                                "wrap the conditions in all:, or make conditions a list", path)
            for group in groups:
                self.group(node[group], path + (group,))
        else:
            self.add(ERROR, "'conditions' must be a list or a mapping of all/any/none", path)

    def condition_block(self, node: Any, path: Path):
        """A refs.conditions.<name> block -- spliced in where it's used"""
        if isinstance(node, list):
            for i, child in enumerate(node):
                self.condition(child, path + (i,))
        elif isinstance(node, dict):
            if '$ref' in node:
                return
            groups = [g for g in schema.CONDITION_GROUPS if g in node]
            if groups:
                for group in groups:
                    self.group(node[group], path + (group,))
                if len(groups) > 1:
                    self.add(INFO, f"Has {' and '.join(groups)}: as a rule's whole `conditions` all are "
                                   f"checked, but inside a list only '{groups[0]}' is", path)
            else:
                self.condition(node, path)
        else:
            self.add(ERROR, "A condition block must be a list or a mapping", path)

    def group(self, node: Any, path: Path):
        if not isinstance(node, list):
            self.add(ERROR, f"'{path[-1]}' must be a list of conditions", path)
            return
        if not node:
            meaning = {'all': 'always passes', 'any': 'never passes', 'none': 'always passes'}[path[-1]]
            self.add(WARNING, f"Empty '{path[-1]}' group {meaning}", path)
        for i, child in enumerate(node):
            self.condition(child, path + (i,))

    def condition(self, node: Any, path: Path):
        """One item inside a group: a leaf, a nested group, or a $ref"""
        if not isinstance(node, dict):
            self.add(ERROR, "A condition must be a mapping (field/operator/value, all/any/none, or $ref)", path)
            return
        if '$ref' in node:
            return

        groups = [g for g in schema.CONDITION_GROUPS if g in node]
        if groups:
            if len(groups) > 1:
                self.add(ERROR, f"Nested group has {' and '.join(groups)}, but only '{groups[0]}' is "
                                f"evaluated here -- split them into separate list items", path)
            for group in groups:
                self.group(node[group], path + (group,))
            return

        missing = [k for k in ('field', 'operator', 'value') if k not in node]
        if missing:
            self.add(ERROR, f"Condition is missing {', '.join(repr(m) for m in missing)} -- "
                            f"the whole rule would never match", path)
        for key in node:
            if key not in ('field', 'operator', 'value'):
                self.add(WARNING, f"'{key}' is ignored in a condition", path + (key,), on_key=True)

        if 'field' in node:
            self.field(node['field'], path + ('field',))
        operator = node.get('operator')
        if 'operator' in node:
            if operator not in schema.OPERATORS:
                self.add(ERROR, f"Unknown operator '{operator}' (expected one of: "
                                f"{', '.join(schema.OPERATORS)})", path + ('operator',))
                operator = None
        if operator and 'value' in node:
            self.value(operator, node['value'], path + ('value',))

    def field(self, field: Any, path: Path):
        if not isinstance(field, str) or '.' not in field:
            self.add(ERROR, "Field must be '<namespace>.<name>', e.g. info.ratio", path)
            return
        namespace, name = field.split('.', 1)
        if namespace not in schema.FIELD_NAMESPACES:
            self.add(ERROR, f"Unknown field namespace '{namespace}' (expected one of: "
                            f"{', '.join(schema.FIELD_NAMESPACES)})", path)
        elif namespace == 'tracker' and name not in schema.TRACKER_FIELD_NAMES:
            self.add(ERROR, f"Unknown trackie field 'tracker.{name}'", path)

    def value(self, operator: str, raw_value: Any, path: Path):
        value, known = self.effective(raw_value)
        if not known:
            return
        if isinstance(value, str) and _RUNTIME_TOKEN.search(value):
            self.add(WARNING, "Runtime tokens like ${info.name} are not filled in condition values -- "
                              "this is compared as literal text", path)

        kind = schema.OPERATORS[operator]['value']
        if kind == 'number':
            try:
                float(value)
            except (TypeError, ValueError):
                self.add(ERROR, f"'{operator}' needs a number, so this never matches", path)
        elif kind == 'duration':
            if not isinstance(value, str) or not _DURATION_PATTERN.match(value.lower().strip()):
                self.add(ERROR, f"{value!r} is not a duration qbt-rules understands, so it is read as "
                                f"0 seconds and '{operator}' matches almost everything -- "
                                f"write it like '30 days' or '12 hours'", path)
        elif kind == 'size':
            m = _SIZE_PATTERN.match(str(value)) if value is not None else None
            if not m or (m.group(2) and m.group(2) not in _SIZE_UNITS):
                self.add(ERROR, f"{value!r} is not a size qbt-rules understands -- write it like '10 GB' or '500 MiB'", path)
            elif m.group(2) and m.group(2).endswith('b'):
                self.add(WARNING, f"'{m.group(2)}' means bits; use '{m.group(2)[:-1]}B' for bytes", path)
        elif kind == 'regex':
            try:
                re.compile(str(value))
            except re.error as e:
                self.add(ERROR, f"Invalid regular expression: {e}", path)
        elif kind == 'string' and not isinstance(value, (str, list)):
            self.add(ERROR, f"'{operator}' needs text (or a list of text), so a {type(value).__name__} never matches", path)

    # -- actions ------------------------------------------------------------

    def action_list(self, node: Any, path: Path, in_rule: bool = False):
        if isinstance(node, dict) and '$ref' in node:
            return
        if isinstance(node, dict) and 'type' in node and not in_rule:
            self.action(node, path)  # a refs.actions block may be a single action
            return
        if not isinstance(node, list):
            self.add(ERROR, "'actions' must be a list -- anything else fails the job "
                            "when this rule matches", path)
            return
        if in_rule and not node:
            self.add(WARNING, "No actions -- this rule matches but does nothing", path)
        for i, action in enumerate(node):
            self.action(action, path + (i,))

    def action(self, node: Any, path: Path):
        if not isinstance(node, dict):
            self.add(ERROR, "An action must be a mapping with a 'type'", path)
            return
        if '$ref' in node:
            return
        if 'type' not in node:
            self.add(ERROR, "Action has no 'type' -- this fails the whole job when the rule matches", path)
            return
        for key in node:
            if key not in ('type', 'params'):
                self.add(WARNING, f"'{key}' is ignored here -- action settings go under params:",
                         path + (key,), on_key=True)

        action_type = node['type']
        spec = schema.ACTION_TYPES.get(action_type) if isinstance(action_type, str) else None
        if spec is None:
            self.add(ERROR, f"Unknown action type '{action_type}'", path + ('type',))
            return

        params = node.get('params', {})
        if 'params' in node and not isinstance(params, dict):
            self.add(ERROR, "'params' must be a mapping", path + ('params',))
            return

        for name in params:
            if name in spec['params']:
                continue
            if action_type == 'delete_torrent' and 'keep' in str(name).lower():
                self.add(WARNING, f"delete_torrent has no '{name}' param, so it is ignored and the files ARE "
                                f"deleted (delete_files defaults to true) -- use delete_files: false",
                         path + ('params', name), on_key=True)
            else:
                self.add(WARNING, f"'{action_type}' has no param '{name}' (ignored)",
                         path + ('params', name), on_key=True)
        for name, pspec in spec['params'].items():
            ppath = path + ('params', name)
            if name not in params:
                if pspec.get('required'):
                    self.add(ERROR, f"'{action_type}' needs params.{name}", path)
                continue
            self.param(action_type, name, pspec, params[name], ppath)

        if action_type == 'notify':
            default_service = (self.notifications or {}).get('service', 'generic')
            service, known = self.effective(params.get('service', default_service))
            if known and 'service' not in params and service not in schema.NOTIFY_SERVICES:
                self.add(ERROR, "notifications.service must be one of: "
                                f"{', '.join(schema.NOTIFY_SERVICES)}", path)
            if known and params.get('message') is None and not (service == 'generic' and 'body' in params):
                self.add(ERROR, "notify needs params.message (or, for service: generic, params.body)", path)
            if self.notifications is not None and not params.get('url') and not self.notifications.get('webhook_url'):
                self.add(ERROR, "notify needs params.url or notifications.webhook_url", path)

    def param(self, action_type: str, name: str, pspec: Dict[str, Any], raw_value: Any, path: Path):
        value, known = self.effective(raw_value)
        if not known:
            return
        ptype = pspec['type']
        if ptype == 'bool':
            self.bool_value(value, f"params.{name}", path)
        elif ptype == 'list' and not isinstance(value, list):
            self.add(WARNING, f"params.{name} should be a list, e.g. [{value!r}]", path)
        elif ptype == 'int' and (isinstance(value, bool) or not isinstance(value, int)):
            self.add(WARNING, f"params.{name} should be a whole number", path)
        elif ptype == 'enum' and value not in pspec['choices']:
            self.add(ERROR, f"params.{name} must be one of: {', '.join(str(c) for c in pspec['choices'])}", path)
        elif ptype == 'regex':
            try:
                re.compile(str(value))
            except re.error as e:
                self.add(ERROR, f"params.{name} is not a valid regular expression: {e}", path)


def _walk_values(node: Any, _seen: Optional[set] = None) -> Iterator[Any]:
    """Every value in a parsed document, depth first (aliases visited once)"""
    seen = _seen if _seen is not None else set()
    yield node
    if isinstance(node, (dict, list)):
        if id(node) in seen:
            return
        seen.add(id(node))
        for v in (node.values() if isinstance(node, dict) else node):
            yield from _walk_values(v, seen)


def lint_rules(raw: Dict[str, Any], notifications: Optional[Dict[str, Any]] = None) -> List[Issue]:
    """Lint rules, optionally checking against resolved notification defaults."""
    return _Linter(raw, notifications).run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _summary(raw: Dict[str, Any], locator: Locator) -> Dict[str, Any]:
    """Outline of the document for the editor's sidebar"""
    rules = []
    for i, rule in enumerate(raw['rules'] if isinstance(raw.get('rules'), list) else []):
        if not isinstance(rule, dict):
            continue
        meta = rule.get('meta') if isinstance(rule.get('meta'), dict) else {}
        context = rule.get('context')
        rules.append({
            'index': i,
            'id': rule.get('id'),
            'description': meta.get('description'),
            'enabled': bool(rule.get('enabled', True)),  # the engine's own test
            'context': context if isinstance(context, (str, list)) else None,
            'stop_on_match': bool(rule.get('stop_on_match')),
            'line': locator.line_of(('rules', i)),
        })

    refs = raw.get('refs') if isinstance(raw.get('refs'), dict) else {}
    ref_names = {}
    for group in ('vars', 'conditions', 'actions'):
        block = refs.get(group) if isinstance(refs.get(group), dict) else {}
        ref_names[group] = [
            {'name': str(name), 'line': (locator.keys.get(('refs', group, name)).start_mark.line + 1
                                         if ('refs', group, name) in locator.keys else None)}
            for name in block
        ]

    contexts = set()
    for rule in rules:
        c = rule['context']
        contexts.update([c] if isinstance(c, str) else (c or []))

    return {'rules': rules, 'refs': ref_names, 'contexts': sorted(str(c) for c in contexts)}


def validate_rules_text(text: str) -> Dict[str, Any]:
    """
    Validate a candidate rules.yml

    Returns {'valid': bool, 'issues': [...], 'counts': {...}, 'summary': {...}}.
    valid is True when there are no errors -- i.e. the server would load it,
    every rule resolves, and nothing in it is known to fail or misfire.
    """
    issues: List[Issue] = []
    summary: Dict[str, Any] = {'rules': [], 'refs': {'vars': [], 'conditions': [], 'actions': []}, 'contexts': []}

    try:
        raw = yaml.safe_load(text)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        message = ' '.join(filter(None, [e.context, e.problem])) or str(e)
        issue = Issue(ERROR, f"YAML syntax error: {message}")
        if mark is not None:
            issue.line, issue.column = mark.line + 1, mark.column + 1
        return _result([issue], summary)
    except yaml.YAMLError as e:
        return _result([Issue(ERROR, f"YAML syntax error: {e}")], summary)

    if raw is None:
        return _result([Issue(ERROR, "File is empty")], summary)

    locator = Locator(yaml.compose(text))
    issues.extend(locator.duplicates)

    structural = check_rules_structure(raw)
    issues.extend(structural)
    if isinstance(raw, dict):
        if not structural:
            issues.extend(check_resolution(raw, locator))
        issues.extend(lint_rules(raw))
        summary = _summary(raw, locator)

    for issue in issues:
        locator.place(issue)
    return _result(issues, summary)


_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


def _result(issues: List[Issue], summary: Dict[str, Any]) -> Dict[str, Any]:
    issues = sorted(issues, key=lambda i: (i.line or 0, i.column or 0, _SEVERITY_ORDER[i.severity]))
    counts = {s: sum(1 for i in issues if i.severity == s) for s in (ERROR, WARNING, INFO)}
    return {
        'valid': counts[ERROR] == 0,
        'issues': [i.to_dict() for i in issues],
        'counts': counts,
        'summary': summary,
    }
