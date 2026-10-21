"""
Reference and variable resolution for qBittorrent automation rules

Implements the resolver layer that provides:
- Variable substitution: ${vars.name} → value
- Rule field substitution: ${rule.id} → this rule's own id/context/etc
- Reference expansion: $ref: conditions.name → condition structure
- Instance-scoped variable overrides
- Type-aware value substitution
- Circular dependency detection

${vars.*} and ${rule.*} are both resolved here, once per rule, at
config-load time -- unlike ${info.*}/${trackers.*}/etc (torrent field
references used in condition values and notify messages), which vary
per torrent and can only be resolved at runtime, in engine.py. This
module's TOKEN_PATTERN deliberately matches only the `vars.`/`rule.`
prefixes, so those runtime tokens pass through untouched and remain in
the string for the engine to resolve later.
"""

import copy
import re
from typing import Any, Dict, List, Optional, Set

from qbt_rules.errors import (
    CircularRefError,
    InvalidRefError,
    InvalidRuleFieldError,
    InvalidVariableError,
    RefTypeMismatchError,
    UnknownRefError,
    UnknownRuleFieldError,
    UnknownVariableError,
)
from qbt_rules.logging import get_logger

logger = get_logger(__name__)

# Pattern for load-time token substitution: ${vars.name} / ${rule.field} /
# ${rule.meta.description} (nested rule fields). One-or-more `.segment`
# repeats so a multi-level path like rule.meta.description matches too, not
# just single-dot fields. Deliberately scoped to just the vars/rule prefixes
# -- ${info.*}/${trackers.*}/etc must NOT match here, since those are
# runtime-only (see module docstring).
TOKEN_PATTERN = re.compile(r'\$\{((?:vars|rule)(?:\.\w+)+)\}')


class RuleResolver:
    """
    Resolves references and variables in rules configuration

    Two-phase resolution:
    1. Reference expansion ($ref: path) - recursive structural replacement
    2. Variable substitution (${vars.name}) - type-aware value replacement

    Example:
        >>> refs = {
        ...     'vars': {'min_ratio': 1.0},
        ...     'conditions': {
        ...         'well-seeded': {
        ...             'all': [
        ...                 {'field': 'info.ratio', 'operator': '>=', 'value': '${vars.min_ratio}'}
        ...             ]
        ...         }
        ...     }
        ... }
        >>> resolver = RuleResolver(refs)
        >>> rule = {'conditions': [{'$ref': 'conditions.well-seeded'}]}
        >>> resolved = resolver.resolve_rule(rule)
        >>> # Result: conditions expanded and variables substituted
    """

    def __init__(
        self,
        refs: Dict[str, Any],
        instance_id: Optional[str] = None,
        instances: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize resolver with refs block and optional instance override

        Args:
            refs: The refs block from config (vars, conditions, actions)
            instance_id: Optional instance ID for scoped variable overrides
            instances: Optional instances dict containing instance-scoped refs
        """
        # Extract typed groups from refs
        # `or {}`: a group present but left empty (`vars:` with everything
        # under it commented out) parses as None, and means "none defined"
        self.vars = dict(refs.get('vars') or {})
        self.conditions = refs.get('conditions') or {}
        self.actions = refs.get('actions') or {}

        # Optional descriptive metadata for reusable blocks (description,
        # tags, etc), e.g. refs.meta.conditions.<name>/refs.meta.actions.<name>.
        # Deliberately never read by _expand_refs/_lookup_ref -- a $ref still
        # resolves to self.conditions[name]/self.actions[name] directly, so
        # this is pure storage for future tooling (e.g. a block-library UI)
        # to read/write, with zero effect on rule evaluation. Absent from a
        # refs block with no 'meta' key, same as today.
        self.meta = refs.get('meta') or {}

        # Apply instance-scoped variable overrides
        # Fully implemented - awaiting Config class integration to pass instance_id
        # Currently Config always passes instance_id=None (see config.py:496)
        if instance_id and instances:
            instance = instances.get(instance_id, {})
            instance_refs = instance.get('refs', {})
            instance_vars = instance_refs.get('vars', {})
            self.vars.update(instance_vars)
            logger.debug(f"Applied instance '{instance_id}' variable overrides: {list(instance_vars.keys())}")

    def get_block_meta(self, kind: str, name: str) -> Dict[str, Any]:
        """
        Look up optional metadata for a reusable $ref block.

        Args:
            kind: 'conditions' or 'actions'
            name: Block name (the part after 'conditions.'/'actions.' in a $ref path)

        Returns:
            The block's refs.meta.<kind>.<name> dict, or {} if none was defined.
            Never raises -- unlike _lookup_ref, a missing metadata entry isn't
            an error, since metadata is optional descriptive information, not
            something rule evaluation depends on.
        """
        return self.meta.get(kind, {}).get(name, {})

    def resolve_rule(self, rule: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fully resolve a rule: expand refs, then substitute variables

        Args:
            rule: Raw rule dictionary from config

        Returns:
            Resolved rule ready for evaluation

        Raises:
            InvalidRefError: Invalid reference path format
            UnknownRefError: Reference not found in refs
            InvalidVariableError: Invalid variable path format
            UnknownVariableError: Variable not found in refs.vars
            CircularRefError: Circular reference dependency detected
            RefTypeMismatchError: Reference type does not match context
        """
        # Phase 1: Deep copy to avoid mutating original
        resolved = copy.deepcopy(rule)

        # Get rule id for error messages
        rule_id = rule.get('id', 'unknown')

        # Phase 2: Expand $ref references with context validation
        # Process conditions separately (only allow conditions.* refs)
        if 'conditions' in resolved:
            resolved['conditions'] = self._expand_refs(
                resolved['conditions'],
                ref_stack=set(),
                allowed_groups=['conditions'],
                path=f"rules['{rule_id}'].conditions"
            )

        # Process actions separately (only allow actions.* refs)
        if 'actions' in resolved:
            resolved['actions'] = self._expand_refs(
                resolved['actions'],
                ref_stack=set(),
                allowed_groups=['actions'],
                path=f"rules['{rule_id}'].actions"
            )

        # Process other fields without ref type restrictions
        for key in resolved:
            if key not in ('conditions', 'actions', 'id'):
                resolved[key] = self._expand_refs(
                    resolved[key],
                    ref_stack=set(),
                    allowed_groups=None,  # No restrictions for other fields
                    path=f"rules['{rule_id}'].{key}"
                )

        # Phase 3: Substitute all ${vars.*} and ${rule.*} tokens (type-aware).
        # ${rule.*} resolves against `rule` (the original, pre-substitution
        # dict) rather than `resolved` -- rule.id/rule.context are always
        # plain scalars in practice, so this is equivalent either way, but
        # using the original avoids a self-referential edge case if a
        # rule's own id field somehow itself contained a ${vars.*} token.
        resolved = self._substitute_tokens(resolved, rule)

        return resolved

    def _expand_refs(
        self,
        node: Any,
        ref_stack: Set[str],
        allowed_groups: Optional[List[str]] = None,
        path: str = ""
    ) -> Any:
        """
        Recursively expand $ref references in a data structure

        Args:
            node: Current node being processed (dict, list, or scalar)
            ref_stack: Stack of reference paths to detect circular dependencies
            allowed_groups: List of allowed ref groups (e.g., ['conditions', 'actions'])
                          None = no restrictions (used for vars and other contexts)
            path: Current path in the rule for error messages (e.g., "rules['test'].conditions[0]")

        Returns:
            Node with all $ref references expanded

        Raises:
            RefTypeMismatchError: Reference type does not match allowed_groups
        """
        if isinstance(node, dict):
            # Check if this dict is a $ref node
            if '$ref' in node:
                ref_path = node['$ref']

                # Validate path format
                if not isinstance(ref_path, str) or '.' not in ref_path:
                    raise InvalidRefError(
                        ref_path=str(ref_path),
                        reason="Path must be in format 'group.name'"
                    )

                # Extract ref group (e.g., 'conditions' from 'conditions.private-tracker')
                ref_group = ref_path.split('.', 1)[0]

                # First, validate that the group is a known group
                valid_groups = ['conditions', 'actions']
                if ref_group not in valid_groups:
                    raise InvalidRefError(
                        ref_path=ref_path,
                        reason=f"Unknown group '{ref_group}'. Valid groups: conditions, actions"
                    )

                # Then, validate ref type against allowed_groups (context validation)
                if allowed_groups is not None and ref_group not in allowed_groups:
                    # Get available refs of the correct type
                    available_refs = []
                    if 'conditions' in allowed_groups and hasattr(self, 'conditions'):
                        available_refs = list(self.conditions.keys())
                    elif 'actions' in allowed_groups and hasattr(self, 'actions'):
                        available_refs = list(self.actions.keys())

                    raise RefTypeMismatchError(
                        ref_path=ref_path,
                        allowed_groups=allowed_groups,
                        actual_group=ref_group,
                        location=path,
                        available_refs=available_refs
                    )

                # Detect circular dependencies
                if ref_path in ref_stack:
                    raise CircularRefError(ref_path=ref_path, ref_stack=list(ref_stack))

                # Look up and expand reference
                expanded = self._lookup_ref(ref_path)

                # Recursively expand the referenced content
                new_stack = ref_stack | {ref_path}
                return self._expand_refs(expanded, new_stack, allowed_groups, path)
            else:
                # Regular dict - recursively process each value
                return {
                    key: self._expand_refs(
                        value,
                        ref_stack,
                        allowed_groups,
                        f"{path}.{key}" if path else key
                    )
                    for key, value in node.items()
                }

        elif isinstance(node, list):
            # Recursively process each list item.
            #
            # A $ref that resolves to a list (e.g. an actions.* block, which is
            # itself an action sequence) is spliced into the parent list rather
            # than nested as a single element -- otherwise a rule's `actions:`
            # list ends up containing a list-shaped item instead of an action
            # dict, and the engine crashes trying to treat it as one.
            # Only applies when the *item itself* was a $ref node; a literal
            # nested list already present in the source YAML is left untouched.
            result = []
            for i, item in enumerate(node):
                was_ref = isinstance(item, dict) and '$ref' in item
                expanded = self._expand_refs(
                    item,
                    ref_stack,
                    allowed_groups,
                    f"{path}[{i}]"
                )
                if was_ref and isinstance(expanded, list):
                    result.extend(expanded)
                else:
                    result.append(expanded)
            return result

        else:
            # Scalar value - return as-is
            return node

    # Endpoints _get_field_value() (engine.py) handles at runtime -- a whole-
    # string ${namespace.x} token using one of these prefixes must be left
    # untouched here rather than treated as a malformed vars/rule token, since
    # it's meant for the engine to resolve later, once a torrent is known.
    RUNTIME_TOKEN_NAMESPACES = (
        'info', 'trackers', 'files', 'peers', 'properties', 'webseeds', 'transfer', 'app', 'stats',
        'tracker',
    )

    def _substitute_tokens(self, node: Any, rule: Dict[str, Any]) -> Any:
        """
        Recursively substitute ${vars.*} and ${rule.*} tokens in a data structure

        Type-aware substitution:
        - If the token is the entire string value → preserve original type
        - If embedded in a larger string → interpolate as string

        ${info.*}/${trackers.*}/etc (runtime, per-torrent fields -- see
        RUNTIME_TOKEN_NAMESPACES) are deliberately left untouched: as a
        whole-string value they're recognized and passed through as-is;
        embedded in a larger string they're never matched by TOKEN_PATTERN
        in the first place, so they pass through for free.

        Args:
            node: Current node being processed
            rule: The rule ${rule.*} tokens resolve against

        Returns:
            Node with all vars/rule tokens substituted

        Raises:
            InvalidVariableError/UnknownVariableError: Malformed or unknown
                ${vars.*} token
            InvalidRuleFieldError/UnknownRuleFieldError: Malformed or unknown
                ${rule.*} token
        """
        if isinstance(node, dict):
            return {key: self._substitute_tokens(value, rule) for key, value in node.items()}

        elif isinstance(node, list):
            return [self._substitute_tokens(item, rule) for item in node]

        elif isinstance(node, str):
            # Check if entire string is a single token
            if node.startswith('${') and node.endswith('}') and node.count('${') == 1:
                token_path = node[2:-1]  # Remove ${ and }
                if token_path.split('.', 1)[0] in self.RUNTIME_TOKEN_NAMESPACES:
                    return node
                # Preserve original type
                return self._resolve_token(token_path, rule)

            # String with embedded tokens - interpolate. TOKEN_PATTERN only
            # matches vars./rule. prefixes, so a ${runtime.x} token embedded
            # in a larger string is never matched here -- it passes through
            # untouched with no special-casing needed.
            def replace_token(match):
                return str(self._resolve_token(match.group(1), rule))

            return TOKEN_PATTERN.sub(replace_token, node)

        else:
            # Scalar non-string value - return as-is
            return node

    def _lookup_ref(self, ref_path: str) -> Any:
        """
        Look up a reference by dot-notation path

        Args:
            ref_path: Reference path like 'conditions.private-tracker'

        Returns:
            Referenced structure

        Raises:
            InvalidRefError: Invalid path format or unknown group
            UnknownRefError: Reference not found

        Note:
            Context validation (e.g., ensuring actions.* refs aren't used in conditions)
            is handled by _expand_refs() before calling this method.
        """
        parts = ref_path.split('.', 1)
        if len(parts) != 2:
            raise InvalidRefError(
                ref_path=ref_path,
                reason="Expected format 'group.name' (e.g., 'conditions.private-tracker')"
            )

        group, name = parts

        if group == 'conditions':
            if name not in self.conditions:
                raise UnknownRefError(ref_path=ref_path, available_refs=list(self.conditions.keys()))
            return self.conditions[name]

        elif group == 'actions':
            if name not in self.actions:
                raise UnknownRefError(ref_path=ref_path, available_refs=list(self.actions.keys()))
            return self.actions[name]

        else:
            raise InvalidRefError(
                ref_path=ref_path,
                reason=f"Unknown group '{group}'. Valid groups: conditions, actions"
            )

    def _resolve_var(self, var_path: str) -> Any:
        """
        Resolve a variable by dot-notation path

        Args:
            var_path: Variable path like 'vars.min_ratio'

        Returns:
            Variable value (preserving original type)

        Raises:
            InvalidVariableError: Invalid path format
            UnknownVariableError: Variable not found
        """
        parts = var_path.split('.', 1)
        if len(parts) != 2 or parts[0] != 'vars':
            raise InvalidVariableError(
                var_path=var_path,
                reason="Expected format 'vars.name' (e.g., 'vars.min_ratio')"
            )

        name = parts[1]
        if name not in self.vars:
            raise UnknownVariableError(var_name=name, available_vars=list(self.vars.keys()))

        return self.vars[name]

    def _resolve_token(self, token_path: str, rule: Dict[str, Any]) -> Any:
        """Dispatch a vars.x or rule.x token path to the matching resolver"""
        if token_path == 'rule' or token_path.startswith('rule.'):
            return self._resolve_rule_field(token_path, rule)
        return self._resolve_var(token_path)

    def _resolve_rule_field(self, field_path: str, rule: Dict[str, Any]) -> Any:
        """
        Resolve a rule's own field by dot-notation path -- one or more
        segments deep, e.g. 'rule.id' or the nested 'rule.meta.description'.

        Unlike ${vars.*} (a global lookup into refs.vars), ${rule.*} is
        self-referential -- it resolves against the specific rule currently
        being processed, not a shared cross-rule table.

        Args:
            field_path: Field path like 'rule.id' or 'rule.meta.description'
            rule: The current rule dict

        Returns:
            Field value (preserving original type)

        Raises:
            InvalidRuleFieldError: Invalid path format
            UnknownRuleFieldError: A segment isn't present on this rule
        """
        parts = field_path.split('.')
        if len(parts) < 2 or parts[0] != 'rule':
            raise InvalidRuleFieldError(
                field_path=field_path,
                reason="Expected format 'rule.id' (e.g., 'rule.id', 'rule.context', 'rule.meta.description')"
            )

        value: Any = rule
        for depth, segment in enumerate(parts[1:], start=1):
            if not isinstance(value, dict) or segment not in value:
                raise UnknownRuleFieldError(
                    field_name='.'.join(parts[1:depth + 1]),
                    rule_id=rule.get('id', 'unknown'),
                    available_fields=list(value.keys()) if isinstance(value, dict) else [],
                )
            value = value[segment]

        return value
