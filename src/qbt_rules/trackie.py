"""
trackie integration -- tracker-side seeding stats for the tracker.* namespace

trackie (https://github.com/andronics/trackie) is a separate service that
reads each private tracker's own Peers and Hit & Run pages and serves them
as one JSON snapshot, with each tracker torrent already matched to its
qBittorrent info hash. qbt-rules knows nothing about any individual
tracker: it fetches that snapshot once per run and looks torrents up by
hash.

Everything here fails safe. trackie unreachable, a snapshot older than
max_age, or a torrent with no record all raise TrackerDataNotReadyError,
which ConditionEvaluator.evaluate() treats like any other DataNotReadyError:
the rule is skipped for that torrent on this run, never evaluated against
guessed data. A delete rule can therefore never fire because trackie was
down.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from qbt_rules.errors import DataNotReadyError, QBittorrentError
from qbt_rules.utils import parse_duration

logger = logging.getLogger(__name__)

# tracker.<field> -> key in a trackie torrent record. tracker.age is computed
# (seconds since the record's seen_at), not looked up.
TORRENT_FIELDS = {
    'status': 'status',              # required | satisfied | hnr
    'remaining': 'remaining_s',      # seconds still owed, None once met
    'seed_time': 'seed_time_s',      # seconds seeded, shown once met
    'uploaded': 'uploaded_b',
    'downloaded': 'downloaded_b',
    'ratio': 'ratio',
    'gap': 'gap_b',                  # H&R only: bytes still needed for 1:1
    'hnr_seed_time': 'hnr_seed_time_s',
    'name': 'name',
    'id': 'tracker_id',
    'tracker': 'tracker',            # trackie's key for the site, e.g. ipt, td
    'match': 'match',                # exact | fuzzy
    'match_source': 'match_source',
    'on_peers': 'on_peers',
    'on_hnr': 'on_hnr',
}

# tracker.account.<field> -> key in a trackie account record. ratio_ok and
# age are computed.
ACCOUNT_FIELDS = {
    'ratio': 'ratio',
    'min_ratio': 'min_ratio',
    'uploaded': 'uploaded_b',
    'downloaded': 'downloaded_b',
    'bonus': 'bonus',
    'seeding': 'seeding',
    'leeching': 'leeching',
    'hnr_count': 'hnr_count',
    'hnr_limit': 'hnr_limit',
    'hnr_grace': 'hnr_grace_s',
}

COMPUTED_TORRENT_FIELDS = ('age',)
COMPUTED_ACCOUNT_FIELDS = ('ratio_ok', 'age')

DEFAULT_MAX_AGE_S = 3 * 3600  # about three of trackie's 45-55 min poll cycles


def parse_max_age(value: Any) -> int:
    """integrations.trackie.max_age: seconds, or a duration like '3 hours'"""
    if value is None or value == '':
        return DEFAULT_MAX_AGE_S
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        return int(value)
    seconds = parse_duration(str(value))
    if seconds <= 0:
        logger.warning(f"Invalid integrations.trackie.max_age {value!r}, using {DEFAULT_MAX_AGE_S}s")
        return DEFAULT_MAX_AGE_S
    return seconds


class TrackerDataNotReadyError(DataNotReadyError):
    """tracker.* data is missing, stale or unreachable -- skip, don't guess"""

    def __init__(self, field: str, torrent_hash: str, problem: str):
        # Deliberately bypasses DataNotReadyError.__init__, whose wording is
        # about qBittorrent file metadata; the class is still a
        # DataNotReadyError so evaluate() handles it the same way.
        QBittorrentError.__init__(
            self,
            code="DATA-002",
            message="Tracker data not available",
            details={"Field": field, "Hash": torrent_hash, "Problem": problem},
            fix="This rule is skipped for this torrent on this run and re-evaluated "
                "next run; check trackie's /health if it persists",
        )


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


class TrackieSnapshot:
    """One /snapshot response, with the lookups the tracker.* fields need"""

    def __init__(self, data: Dict[str, Any], max_age_s: int, now: Optional[datetime] = None):
        self.data = data
        self.max_age_s = max_age_s
        self.now = now or datetime.now(timezone.utc)
        self.torrents: Dict[str, Dict[str, Any]] = {
            h.lower(): r for h, r in (data.get('torrents') or {}).items()
        }
        self.accounts: Dict[str, Dict[str, Any]] = data.get('accounts') or {}
        self.fetched_at: Dict[str, Optional[datetime]] = {
            k: _parse_ts(v) for k, v in (data.get('fetched_at') or {}).items()
        }

    def age_s(self, timestamp: Optional[str]) -> Optional[int]:
        ts = _parse_ts(timestamp)
        return None if ts is None else int((self.now - ts).total_seconds())

    def stale_reason(self, tracker: str) -> Optional[str]:
        """Why this tracker's data can't be used, or None if it can"""
        fetched = self.fetched_at.get(tracker)
        if fetched is None:
            return f"trackie has no data for tracker '{tracker}'"
        age = int((self.now - fetched).total_seconds())
        if age > self.max_age_s:
            return (f"trackie's data for '{tracker}' is {age // 60} min old "
                    f"(max_age {self.max_age_s // 60} min)")
        return None

    def record(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        return self.torrents.get(torrent_hash.lower())

    def tracker_for_urls(self, urls: List[str]) -> Optional[str]:
        """Which tracker a torrent announces to, from its tracker URLs"""
        hosts = {urlparse(u).hostname for u in urls if u}
        for key, account in self.accounts.items():
            if hosts & {h.lower() for h in account.get('announce_hosts') or []}:
                return key
        return None


class TrackieClient:
    """Fetches trackie's /snapshot -- one GET per rules run"""

    def __init__(self, url: str, max_age_s: int, timeout_s: float = 10,
                 session: Optional[requests.Session] = None):
        self.url = url.rstrip('/')
        self.max_age_s = max_age_s
        self.timeout_s = timeout_s
        self.session = session or requests.Session()

    def fetch(self) -> Tuple[Optional[TrackieSnapshot], Optional[str]]:
        """(snapshot, None) on success, (None, reason) on any failure"""
        try:
            resp = self.session.get(f"{self.url}/snapshot", timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            return None, f"trackie unreachable at {self.url} ({type(e).__name__})"
        except ValueError:
            return None, f"trackie at {self.url} did not return JSON"
        if not isinstance(data, dict) or 'torrents' not in data:
            return None, f"trackie at {self.url} returned an unexpected document"
        return TrackieSnapshot(data, self.max_age_s), None
