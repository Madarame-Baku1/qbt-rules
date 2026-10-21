# qbt-rules — TODO

Currently-open items only. Resolved bugs and their postmortems live in
`BUGS.md`; `PLAN.md` is the (mostly complete) v0.4.0 architecture doc.

## Open

- **Kubernetes-style `kind:`/`metadata:`/`spec:` envelope for rules.yml
  resources** — deferred, not decided against. Rationale for waiting: k8s's
  `kind` earns its keep by routing polymorphic resources through one
  API/validation pipeline, and its `spec`/`status` split exists to protect
  user-declared state from system-written state. Neither applies yet --
  `rules.yml` has one resource shape (`rules:`) and no `status` subresource
  (job results live in the separate `jobs` table's `by_rule`, not written
  back onto the rule). Revisit when qbt-ui's CRUD API becomes real: at that
  point `refs.conditions.<name>`/`refs.actions.<name>` (dict-keyed reusable
  blocks) and `rules:` (list of rule objects) are two structurally
  different resource shapes that a real API needs to route/validate
  differently anyway -- that's when `kind: Rule`/`kind: ConditionBlock`/
  `kind: ActionBlock` stops being cosmetic. Doing it now, with only one
  kind in existence, would just add an indentation level with no new
  capability. Also worth revisiting then: further collapsing config.yml/
  rules.yml toward reusable, addressable elements generally (not just this
  one schema shape) once there's a concrete CRUD surface driving the
  requirements, rather than guessing at the shape in advance.

- **Rules editor: what's next.** The editor (`/rules/edit`, `dev/rules-editor`)
  edits rules.yml as a whole document -- GET/PUT `/api/rules/source` with
  ETag/If-Match -- which is deliberately *not* the per-resource CRUD API the
  `kind:`/`spec:` item above is waiting for: one document, one shape, no
  routing. The trigger for revisiting that item is the next step here, a
  structured builder (the `ui/demo-rule-builder*.html` concepts) that edits
  one rule or one `refs` block at a time -- that needs addressable
  resources, and ruamel.yaml round-tripping to patch them without losing
  comments (PyYAML stays the parser that decides meaning; ruamel reads some
  scalars differently). Other follow-ups:
  - config.yml editing. Read-only today: it holds credentials (shown
    masked), is only read at startup, and a bad save stops the server from
    starting instead of leaving old rules running. Needs masked values
    merged back on save, a restart-required flow, and stricter validation
    than YAML-parses.
  - Dry-run an unsaved buffer. The editor's Dry run runs the saved file
    through the normal job queue; testing a candidate needs a job that
    carries its own rules.
  - Schema autocomplete beyond keys/fields/operators/actions (ace-linters +
    a JSON Schema would add hover docs and structural completion).

- **`start`/`stop` idempotency check on qBittorrent 5.** `engine.py`'s
  `_should_skip_idempotent()` tests for `'paused'` in the torrent's state,
  but qBittorrent 5 reports `stoppedUP`/`stoppedDL`: `start` is always
  skipped as "already running" and `stop` never is.

## Decided against (don't re-propose without reading this first)

- **`priority:` field on rules** — documented in the advanced example,
  never implemented (`engine.py` never reads it; rules run in strict file
  order). Decided not to build it: any `rules.yml` without explicit
  priorities needs a deterministic tie-break, and a subtle bug there
  could silently reorder the security-critical rules to run after
  everything else. Not worth the risk at the current rule count, where
  file order is already sufficient. Revisit only if the ruleset grows
  large enough that physical reordering becomes the actual pain point.
- **Instance-scoped variable overrides** — `resolver.py` fully implements
  per-instance variable overrides, but `config.py` hardcodes
  `instance_id=None`, so it's unreachable. Would matter for running one
  shared ruleset across multiple qBittorrent instances. Decided not to
  wire it up: the rest of the app (single `QBittorrentAPI` client, job
  queue schema, webhook routes, every `AutoRun` hook URL) is built around
  exactly one instance per server process — real support means threading
  an `&instance=` dimension through jobs/webhooks/CLI, with no way to test
  it without an actual second qBittorrent instance. Pure speculative
  infrastructure until a second instance actually exists.
