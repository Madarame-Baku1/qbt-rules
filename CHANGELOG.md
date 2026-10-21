# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **This file is maintained automatically by [`release.yml`](.github/workflows/release.yml)
> from conventional commit messages (`feat:`, `fix:`, `refactor:`, `chore:`) at tag time.
> Do not hand-edit entries — the release workflow doesn't check for an existing entry
> before inserting its own, so a manual entry for a version gets duplicated when that
> version is tagged.**

## [0.8.0] - 2026-10-21

### ✨ New Features
- Merge pull request #5 from andronics/dev/trackie-integration
- feat: tracker.* fields from trackie

### 🐛 Bug Fixes
- Merge pull request #4 from andronics/dev/file-log-level
- fix: log file honours logging.level instead of always DEBUG

### 🔧 Maintenance
- chore: Bump version to 0.8.0
- chore: ignore saved tracker pages and graphify output
- chore: ignore .graphify and add VS Code workspace


## [0.7.1] - 2026-09-29

### ✨ New Features
- feat: add release_force_start action and live-updating stats.* for quota-gated rules

### 🔧 Maintenance
- chore: Bump version to 0.7.1


## [0.7.0] - 2026-09-28

### ✨ New Features
- feat!: require unique rule id, drop name, add meta.description
- feat: v0.7 forward-compat foundations (rule id, per-rule stats, ref metadata, eval trace, schema version)
- feat(engine): add stats.dlspeed/upspeed/seeds/peers fleet-wide sums
- feat(engine): add stats.* namespace for aggregate torrent counts

### ♻️ Refactoring
- refactor(engine): rename stats.peers to stats.leechs
- refactor(engine): rename stats.count_by_* to stats.*, add stats.tag.*

### 🔧 Maintenance
- chore: Bump version to 0.7.0


## [0.6.2] - 2026-09-24

### ✨ New Features
- feat(notify): reference rule/torrent fields via ${namespace.field} templating
- feat(dashboard): session-based auth, stop putting the API key in every URL
- feat(dashboard): link Rules summary names to their card, auto-expand on arrival
- feat(dashboard): show Created as relative time ("2 hours ago")
- feat(dashboard): move routes to site root, add login form for missing/invalid key
- feat(dashboard): rewrite templates with the new dark UI design
- feat(ui): YAML preview panel in all four dashboard demos, hidden by default
- feat(ui): add dashboard-overview, dashboard-jobs, dashboard-rules concept demos
- feat(ui): add metrics + config/rules YAML dashboard concept demo
- feat: dedicated CLI UI layer for data-display commands

### 🐛 Bug Fixes
- fix(engine): don't treat unloaded torrent metadata as zero files
- fix(dashboard): remove Status card, show version in topbar instead
- fix(dashboard): remove Performance card, show all rules in Rules summary
- fix(dashboard): render every all/any/none gate present, not just one
- fix(ui): default $ref nodes to collapsed in dashboard-rules tree
- fix: keep diagnostic detail in the log file, keep console bare in client mode

### 🔧 Maintenance
- chore: Bump version to 0.6.2
- chore: add web UI concept demos as baseline for the webui rebuild


## [0.6.1] - 2026-09-17

### Fixed
- **`--job-status` crashed with `AttributeError`** — `job_status_command` read `args.job_id`, but argparse stores the flag's value in `args.job_status` (no explicit `dest` was set). Every invocation failed before reaching the server.
- **`--list-jobs` crashed on any job with no context** — the jobs API returns `"context": null` explicitly rather than omitting the key, so `dict.get('context', 'none')` never fell back to its default; formatting the resulting `None` with a column-width spec raised `TypeError`.
- **`--job-status` printed `Context: None` / `Hash: None`** instead of the intended `none`/`all` placeholders, same root cause as above.
- **Config load errors were duplicated and garbled** — `load_yaml_file`'s broad `except Exception` re-caught and re-wrapped its own file-not-found/file-empty `ConfigurationError`, producing a nested, doubled error message.
- **Default-config-copy failure warning didn't name the file** — `config.yml` and `rules.yml` copy failures printed an identical, ambiguous message.

## [0.6.0] - 2026-09-14

**⚠️ BREAKING CHANGES:**
- `delete_torrent`'s `keep_files` parameter removed entirely — use `delete_files` instead (direct semantics, no inversion)
- qBittorrent config's undocumented `user`/`pass` YAML key aliases removed — `username`/`password` are the only accepted keys
- Hidden `--torrent-hash` CLI alias removed — use `--hash`

### Added
- **Internal cron scheduler**: `schedule:` block in `config.yml` for recurring rule execution, with indexed `QBT_RULES_SCHEDULE_<N>_*` env var support. Fork-safe with multiple Gunicorn workers.
- **Generic outbound notification action** (`notify`): Discord/Slack/ntfy/generic webhook support for rule-triggered alerts.
- **`arr_blocklist` action**: blocklist-and-research automation for Sonarr/Radarr on a matched torrent.
- **Read-only web dashboard**: browse jobs, queue state, and rule definitions in-browser, reusing the existing API-key auth.
- **Optional Prometheus metrics** (`metrics.enabled`): job/action/scheduler counters and HTTP request histograms, multiprocess-safe under Gunicorn.

### Fixed
- **`qbittorrent.username`/`.password`/`.host` and every `QBT_RULES_QBITTORRENT_*` env var were silently non-functional** — only the undocumented `user`/`pass` YAML keys ever worked, and only when set directly in `config.yml`, never via env var. Every documented deployment path (`README.md`, all `docker-compose*.yml` files) showed the broken configuration. Now resolved through the same CLI → env → `_FILE` → config.yml → default chain as every other section.
- **`QBT_RULES_LOG_*` env vars were dead** — the real code read bare, unprefixed `LOG_LEVEL`/`LOG_FILE`/`TRACE_MODE` directly, with no `_FILE` secret support, and `logging.http_access` had no env var support at all. Renamed to `QBT_RULES_LOGGING_*`, matching the documented naming convention, with full `_FILE` support gained for free.
- **`QBT_RULES_DRY_RUN` was dead** — `Config.is_dry_run()` read a bare `DRY_RUN` env var directly, bypassing the env var mapping that was already documented. Same fix applied; `--dry-run` CLI flag behavior is unchanged.

## [0.5.9] - 2026-09-11

### Fixed
- Bumped `softprops/action-gh-release` from v1 to v3 in `release.yml` to restore `make_latest` support.

## [0.5.8] - 2026-09-11

### Fixed
- `SQLiteQueue` was missing a `busy_timeout`, causing real thread-race failures under concurrent job submission.

## [0.5.7] - 2026-09-11

### Added
- Four new actions: `increase_priority`, `decrease_priority`, `set_top_priority`, `set_bottom_priority`, wired up to qBittorrent's torrent queue endpoints.

## [0.5.6] - 2026-09-07

### Fixed
- **Collection field negation operators used the wrong match semantics.** `!=`/`not_in`/`not_contains` against a collection field (`trackers.*`, `files.*`, `peers.*`, `webseeds.*`) now require **all** items to satisfy the negation, not just one — previously a single non-matching item in a multi-item collection could make a negation check trivially pass. Positive operators (`==`, `contains`, `in`, ...) are unaffected and keep ANY-match semantics.

## [0.5.5] - 2026-09-07

### Added
- `contains`/`not_contains` now accept a list as `value:`, matching if **any** item in the list is found (previously list values only worked with `in`/`not_in`).

## [0.5.4] - 2026-09-07

### Fixed
- A `$ref: actions.name` reference that resolved to a list (an action sequence) was nested as a single list-shaped item inside the parent `actions:` list instead of being spliced in — the engine crashed trying to execute it as one action.

## [0.5.3] - 2026-09-07

### Fixed
- A bare list under `conditions:` (no `all`/`any`/`none` wrapper) silently matched **every** torrent instead of being treated as an implicit `all:`. This affected any rule mixing `$ref:` entries directly under `conditions:`, a pattern the resolver layer encourages.

## [0.5.2] - 2026-09-07

### Fixed
- `release.yml` was silently self-correcting a version mismatch between a pushed tag and `__version__.py`/`pyproject.toml` instead of failing the release — it now fails loudly, since a mismatch means the tagged commit wasn't produced by `scripts/bump-version.sh`.

## [0.5.1] - 2026-09-07

### Fixed
- Repaired the CI/Docker publish pipeline: `docker-build.yml`'s `type=sha,prefix={{branch}}-` tag rule produced an invalid empty-prefix tag on every tag-triggered build, meaning no versioned image had ever actually reached GHCR despite the workflow reporting success.

## [0.5.0] - 2025-12-20

### Added
- **Reusable References (resolver layer)**: a `refs:` block in `rules.yml` for defining `vars`, `conditions`, and `actions` once and reusing them across rules via `$ref: conditions.name`/`$ref: actions.name` and `${vars.x}` substitution. Includes context-aware type validation (a `conditions.*` ref can't be used where an `actions.*` ref is expected, and vice versa) and circular-reference detection.
- **Hot-reload for `rules.yml`**: the rules file is checked for modification on each job and reloaded automatically without a server restart. If the reload fails (e.g. a YAML syntax error), the server keeps using the last known-good rules rather than crashing.

## [0.4.1] - 2025-12-19

### Added
- **Automatic Default Config Creation**: On first run, default `config.yml` and `rules.yml` files are automatically copied from `/usr/share/qbt-rules/` to the config directory if they don't exist. This eliminates manual configuration file setup for new users.
- **HTTP Access Log Control**: Added `logging.http_access` configuration option (default: false) to suppress Docker health check logs from polluting console output. Set to `true` for debugging.
- **Required Field Validation**: Rules now must have `name`, `conditions`, and `actions` fields - clear error messages if missing
- **Cache Updates After Actions**: Later rules in the same execution now see changes made by earlier rules (tags, categories, etc.)

### Changed
- **BREAKING**: Removed legacy `--trigger` parameter - use `--context` instead
- **Context is now fully user-defined**: No special context values (on_added, scheduled, etc.) - use any custom string identifier you want
- Documentation and examples updated to show custom context names (torrent-imported, weekly-cleanup, download-finished, adhoc-run)
- Fixed critical bug: Rules without `context` field now correctly execute regardless of runtime context
- Fixed bug: `context` field moved from inside `conditions` block to rule level (sibling to conditions/actions)
- Renamed example config files from `.example.yml` to `.default.yml` to reflect their new role as automatic defaults
- Updated Docker image to bundle default configs in `/usr/share/qbt-rules/` (Linux FHS standard)
- Simplified Quick Start documentation - manual config file download no longer required
- Health check endpoint (`/api/health`) logs are now suppressed by default to reduce console noise
- `--list-rules` now shows "Context" column instead of "Trigger"

### Migration Guide
- Replace `--trigger` with `--context` in scripts/cron jobs
- The `context` field (if used) should be at rule level, not inside `conditions`
- Context values are user-defined - use descriptive names that make sense for your workflow

## [0.4.0] - 2024-12-14

### 🚀 Major Release - Complete Architectural Transformation

v0.4.0 introduces a complete redesign with client-server architecture, HTTP API, persistent job queue, and Docker-first deployment.

**⚠️ BREAKING CHANGES:**
- Distribution changed from PyPI package to Docker images (ghcr.io/andronics/qbt-rules)
- Architecture changed from standalone CLI to client-server model
- Terminology changed from "trigger" to "context" (backward compatible flags exist)
- Configuration file structure updated (see config.example.yml)

### Added

#### Core Architecture
- **Client-Server Architecture**: HTTP API server with background worker thread
- **Persistent Job Queue**: Sequential job processing with state tracking
  - SQLite backend (default, zero dependencies)
  - Redis backend (optional, for high-performance scenarios)
  - Job states: pending → processing → completed/failed/cancelled
- **RESTful HTTP API**: 7 endpoints for job management and monitoring
- **Worker Thread**: Background job processor with graceful shutdown support

#### Queue Backends
- **SQLite Queue Backend** - File-based persistence with thread-safety, WAL mode, ACID transactions
- **Redis Queue Backend** - In-memory performance with connection pooling and atomic operations

#### Configuration & Security
- **Universal _FILE Support**: All environment variables support _FILE suffix for Docker secrets
- **API Key Authentication**: Secure webhook endpoints with constant-time comparison
- **Standardized Environment Variables**: All use QBT_RULES_* prefix
- **Configuration Resolution Priority**: CLI → ENV_FILE → ENV → config.yml → defaults

#### Docker & Deployment
- **Multi-stage Dockerfile**: Optimized build with Python 3.11, multi-platform (amd64, arm64)
- **Docker Compose Examples**: Minimal (SQLite), Redis backend, and full-stack configurations
- **GitHub Actions Workflows**: Automated Docker builds and publishing to ghcr.io

#### Documentation
- **Architecture Documentation** (docs/Architecture.md) - System design, components, data flow
- **HTTP API Reference** (docs/API.md) - Complete endpoint documentation with examples
- **Docker Deployment Guide** (docs/Docker.md) - Installation, scenarios, monitoring, troubleshooting
- **Implementation Plan** (PLAN.md) - 700+ line detailed specification

#### qBittorrent Integration
- **qbittorrent-api Package**: Multi-version support (v4.1+ through v5.1.4+) with zero data loss

#### CLI Enhancements
- **Server Mode**: qbt-rules --serve to run HTTP API server
- **Client Mode**: Submit jobs to remote server via HTTP API
- **Job Management**: --list-jobs, --job-status, --cancel-job, --stats, --wait
- **20+ New CLI Flags** for server, client, queue, and job management
- **Backward Compatibility**: --trigger flag mapped to --context

### Changed

#### Terminology
- **"Trigger" → "Context"** throughout codebase (backward compatible via CLI flag mapping)

#### Architecture
- **Execution Model**: Direct CLI → HTTP API + persistent queue
- **Processing**: Immediate → Sequential queue-based
- **State**: Stateless → Stateful with job tracking
- **Distribution**: PyPI → Docker images (ghcr.io)

#### Configuration
- Updated config.example.yml structure with server/client/queue sections
- All environment variables use QBT_RULES_* prefix
- Rules file: trigger: → context: (optional, backward compatible)

#### Dependencies
- **Added**: qbittorrent-api, Flask, gunicorn
- **Optional**: redis

### Deprecated

- **PyPI Distribution**: v0.3.x remains available but deprecated (v0.4.0+ is Docker-only)
- **Standalone CLI**: Direct execution still works but not recommended (use client mode or API)

### Fixed

- Race conditions via sequential queue processing
- Job persistence across restarts
- Multi-threading safety with thread-safe queues
- Clear configuration precedence order

### Migration Guide

**From v0.3.x to v0.4.0:**

1. **Rules File**: Optional - change trigger: to context: (backward compatible)
2. **Configuration**: Update structure following config.example.yml
3. **Deployment**: Replace PyPI with Docker Compose
4. **Execution**: Use HTTP API or client mode instead of direct CLI
5. **Monitoring**: Use /api/health, /api/stats, /api/jobs

See README.md for detailed migration examples.


### 🚀 Major Release - Complete Architectural Transformation

v0.4.0 introduces a complete redesign with client-server architecture, HTTP API, persistent job queue, and Docker-first deployment.

**⚠️ BREAKING CHANGES:**
- Distribution changed from PyPI package to Docker images (ghcr.io)
- Architecture changed from standalone CLI to client-server model
- Terminology changed from "trigger" to "context" (backward compatible flags exist)
- Configuration file structure updated (see config.example.yml)

### Added

#### Core Architecture
- **Client-Server Architecture**: HTTP API server with background worker thread
- **Persistent Job Queue**: Sequential job processing with state tracking
  - SQLite backend (default, zero dependencies)
  - Redis backend (optional, for high-performance scenarios)
  - Job states: pending → processing → completed/failed/cancelled
- **RESTful HTTP API**: 7 endpoints for job management and monitoring
  - `POST /api/execute` - Queue rules execution job
  - `GET /api/jobs` - List jobs with filtering and pagination
  - `GET /api/jobs/:id` - Get job status and results
  - `DELETE /api/jobs/:id` - Cancel pending job
  - `GET /api/health` - Health check (unauthenticated)
  - `GET /api/stats` - Server statistics
  - `GET /api/version` - Version information
- **Worker Thread**: Background job processor with graceful shutdown support

#### Queue Backends
- **SQLite Queue Backend** (`src/qbt_rules/queue_backends/sqlite_queue.py`):
  - File-based persistence (/config/qbt-rules.db)
  - Thread-safe with connection-per-thread pattern
  - WAL mode for improved concurrency
  - ACID transactions
  - Automatic schema migration
- **Redis Queue Backend** (`src/qbt_rules/queue_backends/redis_queue.py`):
  - In-memory performance with optional persistence
  - Connection pooling
  - Multiple data structures (LIST, HASH, SET, ZSET)
  - Atomic operations for job state management

#### Configuration & Security
- **Universal `_FILE` Support**: All environment variables support `_FILE` suffix for Docker secrets
- **API Key Authentication**: Secure webhook endpoints with constant-time comparison
- **Standardized Environment Variables**: All use `QBT_RULES_*` prefix
- **Configuration Resolution Priority**: CLI → ENV_FILE → ENV → config.yml → defaults
- **Health Check Endpoint**: Unauthenticated `/api/health` for container orchestration

#### Docker & Deployment
- **Multi-stage Dockerfile**: Optimized build with Python 3.11
- **Multi-platform Support**: Docker images for linux/amd64 and linux/arm64
- **Docker Compose Examples**:
  - `docker-compose.yml` - Minimal setup with SQLite
  - `docker-compose.redis.yml` - Redis backend setup
  - `docker-compose.full-stack.yml` - Complete stack with qBittorrent
- **GitHub Actions Workflows**:
  - `docker-build.yml` - Automated Docker image builds and publishing to ghcr.io
  - Updated `release.yml` - Docker-focused release process (removed PyPI publishing)

#### Documentation
- **Complete Architecture Documentation** (`docs/Architecture.md`):
  - System design diagrams
  - Component descriptions
  - Data flow explanations
  - Queue backend comparison
  - Configuration resolution logic
  - Security model
- **HTTP API Reference** (`docs/API.md`):
  - Complete endpoint documentation
  - Request/response examples
  - Error handling guide
  - Python client examples
  - Prometheus integration example
- **Docker Deployment Guide** (`docs/Docker.md`):
  - Installation instructions
  - Deployment scenarios (standalone, Redis, full-stack, reverse proxy)
  - Secrets management
  - Networking configuration
  - Monitoring and troubleshooting
  - Advanced topics (scheduled execution, backups, auto-updates)
- **Implementation Plan** (`PLAN.md`): 700+ line detailed implementation specification

#### qBittorrent Integration
- **qbittorrent-api Package**: Migrated from direct API calls to qbittorrent-api wrapper
  - Multi-version support (qBittorrent v4.1+ through v5.1.4+)
  - Automatic version detection
  - Zero data loss (all existing fields available)
  - Improved error handling

#### CLI Enhancements
- **Server Mode**: `qbt-rules --serve` to run HTTP API server
- **Client Mode**: Submit jobs to remote server via HTTP API
- **Job Management Commands**:
  - `--list-jobs` - List all jobs
  - `--job-status <id>` - Get job status
  - `--cancel-job <id>` - Cancel pending job
  - `--stats` - Show server statistics
  - `--wait` - Wait for job completion (synchronous behavior)
- **20+ New CLI Flags** for server, client, queue, and job management configuration
- **Backward Compatibility**: `--trigger` flag mapped to `--context` automatically

### Changed

#### Terminology
- **"Trigger" → "Context"** throughout codebase and documentation
  - `trigger:` condition in rules → `context:`
  - `--trigger` CLI flag → `--context`
  - Rules engine parameter renamed: `trigger` → `context`
  - Configuration sections updated
  - Backward compatible: old `trigger:` syntax still works

#### Architecture
- **Execution Model**: Direct CLI invocation → HTTP API + persistent queue
- **Processing**: Immediate execution → Sequential queue-based processing
- **State Management**: Stateless → Stateful with job tracking and history
- **Distribution**: PyPI package → Docker images (ghcr.io/andronics/qbt-rules)

#### Configuration
- **File Structure**: Updated config.example.yml with new sections:
  - `server:` - HTTP API server configuration
  - `client:` - CLI client configuration
  - `queue:` - Queue backend configuration
  - `qbittorrent:` - qBittorrent connection (unchanged)
- **Environment Variables**: All standardized with `QBT_RULES_*` prefix
- **Rules File**: Minimal changes (`trigger:` → `context:`, optional for compatibility)

#### Dependencies
- **Added**:
  - `qbittorrent-api>=2024.12.0` - Multi-version qBittorrent support
  - `Flask>=3.0.0` - HTTP API framework
  - `gunicorn>=21.2.0` - Production WSGI server
- **Optional Dependencies**:
  - `redis>=5.0.0` - Redis queue backend support
- **Unchanged**:
  - `requests>=2.31.0`
  - `PyYAML>=6.0.2`

### Deprecated

- **PyPI Distribution**: v0.3.x remains available on PyPI but is deprecated
  - No new PyPI releases planned (v0.4.0+ is Docker-only)
  - Users encouraged to migrate to Docker deployment
- **Standalone CLI Mode**: Direct execution without server still works but not recommended
  - Use client mode (`--client-server-url`) or HTTP API instead

### Fixed

- **Race Conditions**: Sequential queue processing prevents concurrent rule execution
- **Job Persistence**: Jobs survive server restarts and crashes
- **Multi-threading Safety**: Thread-safe queue implementations
- **Configuration Precedence**: Clear priority order (CLI → ENV → file → defaults)

### Migration Guide

**From v0.3.x to v0.4.0:**

1. **Rules File** (`rules.yml`):
   - Optional: Change `trigger:` to `context:` (backward compatible)
   - No other changes required

2. **Configuration** (`config.yml`):
   - Update structure following `config.example.yml`
   - Add server/client/queue sections
   - Set `QBT_RULES_SERVER_API_KEY`

3. **Deployment**:
   - Replace PyPI installation with Docker Compose
   - Replace cron/systemd timers with:
     - Webhook integration (qBittorrent "Run external program")
     - Cron container for scheduled execution
   - Configure volume mounts for `/config`

4. **Execution**:
   - Replace `qbt-rules --trigger scheduled` with:
     - HTTP API: `curl -X POST "http://localhost:5000/api/execute?context=scheduled&key=KEY"`
     - Client mode: `qbt-rules --context scheduled --client-server-url http://localhost:5000`

5. **Monitoring**:
   - Use `/api/health` for health checks
   - Use `/api/stats` for statistics
   - Use `/api/jobs` for execution history

**Example Migration:**

Before (v0.3.x):
```bash
# Cron job
*/5 * * * * /usr/local/bin/qbt-rules --trigger scheduled
```

After (v0.4.0):
```yaml
# docker-compose.yml
services:
  qbt-rules:
    image: ghcr.io/andronics/qbt-rules:latest
    ports:
      - "5000:5000"
    volumes:
      - ./config:/config
    environment:
      QBT_RULES_SERVER_API_KEY: "secure-key"
      QBT_RULES_QBITTORRENT_HOST: "http://qbittorrent:8080"

  qbt-rules-cron:
    image: alpine:latest
    command: >
      sh -c "
        apk add curl &&
        echo '*/5 * * * * curl -X POST http://qbt-rules:5000/api/execute?context=scheduled&key=secure-key' | crontab - &&
        crond -f
      "
```

## [0.3.2] - 2024-12-13

### Added
- Enhanced documentation with clearer trigger mechanism explanation
- Improved README with detailed examples
- Better logging for rule evaluation

### Fixed
- Build job now correctly checks out main branch for updated version
- Release workflow improved for reliability

## [0.3.1] - 2025-12-13

### Added
- Comprehensive test coverage for `api.py` module (0% → 100% coverage)
- Comprehensive test coverage for `arguments.py` module (100% coverage)
- Comprehensive test coverage for `config.py` module (100% coverage)
- Additional test coverage for `engine.py` edge cases and nested conditions
- Documentation for human-readable size operators (`larger_than`, `smaller_than`)

### Changed
- Simplified CLI structure by moving `cli/main.py` to `cli.py` (internal refactoring)

### Fixed
- Removed unreachable dead code from `engine.py` (line 341)
- CI validation workflow to reflect new CLI structure (removed deprecated cli folder path check)

## [0.3.0] - 2024-12-13

### Changed
- **BREAKING**: Consolidated four separate CLI commands into single unified `qbt-rules` command
- **BREAKING**: Removed console scripts: `qbt-manual`, `qbt-scheduled`, `qbt-on-added`, `qbt-on-completed`
- **BREAKING**: Deleted legacy CLI files: `manual.py`, `scheduled.py`, `on_added.py`, `on_completed.py`

### Added
- New `--trigger` parameter to specify trigger name (manual, scheduled, on_added, on_completed, or custom)
- New `--torrent-hash` parameter to process specific torrents
- Support for custom trigger names (completely freeform - any string accepted)
- Trigger-agnostic mode: `--torrent-hash` without `--trigger` runs rules with no trigger conditions
- Single unified `qbt-rules` console script replaces all previous commands

### Migration Guide for v0.3.0
**If upgrading from v0.2.0:**

1. **Update console script usage**:
   ```bash
   # Old (v0.2.0)
   qbt-manual
   qbt-scheduled
   qbt-on-added <hash>
   qbt-on-completed <hash>

   # New (v0.3.0)
   qbt-rules                                    # defaults to manual trigger
   qbt-rules --trigger scheduled
   qbt-rules --trigger on_added --torrent-hash <hash>
   qbt-rules --trigger on_completed --torrent-hash <hash>
   ```

2. **Update cron jobs**:
   ```bash
   # Old
   0 * * * * qbt-scheduled

   # New
   0 * * * * qbt-rules --trigger scheduled
   ```

3. **Update qBittorrent webhooks**:
   ```bash
   # Old webhook command
   qbt-on-completed %I

   # New webhook command
   qbt-rules --trigger on_completed --torrent-hash %I
   ```

4. **Custom triggers now supported**:
   ```bash
   # Use any custom trigger name
   qbt-rules --trigger my_custom_workflow
   qbt-rules --trigger nightly_cleanup
   ```

5. **Trigger-agnostic mode** (new feature):
   ```bash
   # Process specific torrent with rules that have NO trigger condition
   qbt-rules --torrent-hash abc123...
   ```

**Why this change?**
- Eliminates 98% code duplication across four nearly-identical CLI files
- Provides single, consistent command-line interface
- Enables custom trigger names for flexible workflows
- Simpler codebase maintenance
- More intuitive CLI design

**Default behavior:**
- Running `qbt-rules` without `--trigger` defaults to `manual` trigger
- Running `qbt-rules --torrent-hash <hash>` without `--trigger` uses trigger-agnostic mode (matches only rules with no trigger condition)

## [0.2.0] - 2024-12-10

### Changed
- **BREAKING**: Project renamed from `qbittorrent-automation` to `qbt-rules`
- **BREAKING**: Package name changed from `qbittorrent-automation` to `qbt-rules` on PyPI
- **BREAKING**: Import paths changed from `qbittorrent_automation.*` to `qbt_rules.*`
- **BREAKING**: Package directory renamed from `src/qbittorrent_automation/` to `src/qbt_rules/`
- All GitHub repository URLs updated to reflect new name
- All documentation links updated to point to new repository
- Console scripts remain unchanged (`qbt-manual`, `qbt-scheduled`, etc.) - perfect alignment with new name
- Configuration directory recommendation changed to `~/.config/qbt-rules/`

### Migration Guide for v0.2.0
**If upgrading from v0.1.0:**

1. **Update imports in your code** (if you imported the package directly):
   ```python
   # Old (v0.1.0)
   from qbittorrent_automation.api import QBittorrentAPI
   from qbittorrent_automation.config import load_config

   # New (v0.2.0)
   from qbt_rules.api import QBittorrentAPI
   from qbt_rules.config import load_config
   ```

2. **Reinstall package**:
   ```bash
   pip uninstall qbittorrent-automation
   pip install qbt-rules
   ```

3. **Console scripts unchanged** - `qbt-manual`, `qbt-scheduled`, etc. work exactly the same
4. **Configuration files unchanged** - No changes needed to `config.yml` or `rules.yml`
5. **Legacy trigger scripts** still work if using `python triggers/manual.py` approach

**Why this change?**
- Shorter, more memorable name
- Perfect alignment with existing `qbt-*` console script naming
- Clearer branding and easier to communicate
- More concise package name on PyPI

## [0.1.0] - 2024-12-10

### Added
- **Complete Python package restructuring to src layout**
- PyPI installable package with `pip install qbittorrent-automation`
- Console script entry points: `qbt-manual`, `qbt-scheduled`, `qbt-on-added`, `qbt-on-completed`
- `pyproject.toml` with modern build system (PEP 517/518)
- `LICENSE` file (Unlicense/Public Domain)
- `requirements-dev.txt` with development dependencies
- Test suite infrastructure with pytest
- Type hints support with `py.typed` marker
- Legacy wrapper scripts for backward compatibility
- GitHub Actions workflows for CI and releases
- Automated version bumping script
- Release automation with changelog generation

### Changed
- **BREAKING**: Project structure migrated from flat layout to src layout
- **BREAKING**: Import paths changed from `from lib.X` to `from qbittorrent_automation.X`
- Moved `lib/*` → `src/qbittorrent_automation/*`
- Moved `triggers/*` → `src/qbittorrent_automation/cli/*`
- Updated all internal imports to use new package structure
- Version bumped to 0.1.0 to reflect major restructuring

### Fixed
- Removed duplicate PyYAML entries in requirements.txt
- Updated .gitignore for build artifacts (.mypy_cache, .ruff_cache, etc.)

### Backward Compatibility
- Legacy `triggers/*.py` scripts still work (as thin wrappers)
- Existing deployments using `python triggers/manual.py` continue to function
- Configuration files and rules syntax unchanged

### Migration Guide
- **For users cloning the repo**: Run `pip install -e .` to install in editable mode
- **For new installations**: Use `pip install qbittorrent-automation` from PyPI
- **For cron jobs**: Update to use `qbt-scheduled` instead of `python triggers/scheduled.py`
- **For systemd**: Update ExecStart to use `/usr/local/bin/qbt-scheduled`
- **For Docker**: Update Dockerfile to `pip install qbittorrent-automation`

## [0.0.1] - 2024-12-10

### Added
- Initial release of qBittorrent Automation System
- Complete qBittorrent Web API v2 client (v5.0+ support)
- Rules engine with file-order execution
- Dot notation field access (info.*, trackers.*, files.*, peers.*, etc.)
- Multiple trigger types: manual, scheduled, on_added, on_completed
- Centralized logging with trace mode
- Centralized argument parsing with utility flags
- Environment variable expansion in config files
- Comprehensive operators and condition logic
- Dry-run mode for safe testing
- Idempotency detection for actions

### Fixed
- Updated API endpoints for qBittorrent v5.0 (pause→stop, resume→start)
- Proper boolean parsing in configuration files

### Changed
- Renamed action types to match qBittorrent v5.0 API terminology
- Action 'pause' → 'stop'
- Action 'resume' → 'start'

[Unreleased]: https://github.com/andronics/qbt-rules/compare/v0.5.9...HEAD
[0.5.9]: https://github.com/andronics/qbt-rules/compare/v0.5.8...v0.5.9
[0.5.8]: https://github.com/andronics/qbt-rules/compare/v0.5.7...v0.5.8
[0.5.7]: https://github.com/andronics/qbt-rules/compare/v0.5.6...v0.5.7
[0.5.6]: https://github.com/andronics/qbt-rules/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/andronics/qbt-rules/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/andronics/qbt-rules/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/andronics/qbt-rules/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/andronics/qbt-rules/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/andronics/qbt-rules/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/andronics/qbt-rules/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/andronics/qbt-rules/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/andronics/qbt-rules/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/andronics/qbt-rules/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/andronics/qbt-rules/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/andronics/qbt-rules/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/andronics/qbt-rules/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/andronics/qbt-rules/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/andronics/qbt-rules/releases/tag/v0.0.1
