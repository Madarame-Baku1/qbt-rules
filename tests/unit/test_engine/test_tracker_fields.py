"""tracker.* fields from the trackie integration (qbt_rules/trackie.py)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from qbt_rules.engine import ConditionEvaluator, RulesEngine
from qbt_rules.errors import DataNotReadyError, FieldError
from qbt_rules.trackie import (
    DEFAULT_MAX_AGE_S,
    TrackerDataNotReadyError,
    TrackieClient,
    TrackieSnapshot,
    parse_max_age,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(minutes=20)).isoformat().replace('+00:00', 'Z')
STALE = (NOW - timedelta(hours=5)).isoformat().replace('+00:00', 'Z')

HASH_REQUIRED = 'a' * 40
HASH_SATISFIED = 'b' * 40
HASH_HNR = 'c' * 40
HASH_UNKNOWN = 'f' * 40


def record(tracker, tid, status, **kw):
    base = {
        'tracker': tracker, 'tracker_id': tid, 'name': f'Torrent {tid}', 'name_truncated': False,
        'status': status, 'remaining_s': None, 'seed_time_s': None, 'uploaded_b': 100,
        'downloaded_b': 1000, 'ratio': 0.1, 'gap_b': None, 'hnr_seed_time_s': None,
        'on_peers': True, 'on_hnr': False, 'seen_at': FRESH,
        'match': 'exact', 'match_source': 'sonarr',
    }
    base.update(kw)
    return base


def snapshot_doc(td_fetched=FRESH):
    return {
        'generated_at': FRESH,
        'fetched_at': {'ipt': FRESH, 'td': td_fetched},
        'accounts': {
            'ipt': {'ratio': 0.8, 'min_ratio': 1.0, 'uploaded_b': 8, 'downloaded_b': 10, 'bonus': 50.0,
                    'seeding': 3, 'leeching': 0, 'hnr_count': 1, 'hnr_limit': 40, 'hnr_grace_s': 432000,
                    'announce_hosts': ['routing.bgp.technology'], 'seen_at': FRESH},
            'td': {'ratio': 1.9, 'min_ratio': 0.3, 'uploaded_b': 19, 'downloaded_b': 10, 'bonus': 9.5,
                   'seeding': 1, 'leeching': 0, 'hnr_count': 0, 'hnr_limit': 40, 'hnr_grace_s': 259200,
                   'announce_hosts': ['sync.td-peers.com'], 'seen_at': td_fetched},
        },
        'torrents': {
            HASH_REQUIRED.upper(): record('ipt', 1, 'required', remaining_s=3600),
            HASH_SATISFIED: record('ipt', 2, 'satisfied', seed_time_s=1209600, match='fuzzy',
                                   match_source='name'),
            HASH_HNR: record('td', 3, 'hnr', gap_b=900, on_peers=False, on_hnr=True, hnr_seed_time_s=0),
        },
        'unmatched': [],
    }


class FakeTrackie:
    """Stands in for TrackieClient: counts fetches, returns a fixed result."""

    def __init__(self, doc=None, error=None, max_age_s=DEFAULT_MAX_AGE_S):
        self.doc, self.error, self.max_age_s, self.calls = doc, error, max_age_s, 0

    def fetch(self):
        self.calls += 1
        if self.error:
            return None, self.error
        return TrackieSnapshot(self.doc, self.max_age_s, now=NOW), None


@pytest.fixture
def evaluator(mock_api):
    ev = ConditionEvaluator(mock_api)
    ev.trackie = FakeTrackie(snapshot_doc())
    return ev


def torrent(hash_):
    return {'hash': hash_, 'name': 'x'}


# ============================================================================
# Torrent fields
# ============================================================================

class TestTorrentFields:

    @pytest.mark.parametrize(('field', 'expected'), [
        ('tracker.status', 'required'),
        ('tracker.remaining', 3600),
        ('tracker.seed_time', None),
        ('tracker.tracker', 'ipt'),
        ('tracker.id', 1),
        ('tracker.match', 'exact'),
        ('tracker.match_source', 'sonarr'),
        ('tracker.uploaded', 100),
        ('tracker.ratio', 0.1),
        ('tracker.age', 1200),
    ])
    def test_required_torrent(self, evaluator, field, expected):
        # The snapshot key is uppercase: lookups are case-insensitive
        assert evaluator.get_field_value(torrent(HASH_REQUIRED), field) == expected

    def test_satisfied_and_hnr(self, evaluator):
        assert evaluator.get_field_value(torrent(HASH_SATISFIED), 'tracker.status') == 'satisfied'
        assert evaluator.get_field_value(torrent(HASH_SATISFIED), 'tracker.seed_time') == 1209600
        assert evaluator.get_field_value(torrent(HASH_SATISFIED), 'tracker.match') == 'fuzzy'
        assert evaluator.get_field_value(torrent(HASH_HNR), 'tracker.gap') == 900
        assert evaluator.get_field_value(torrent(HASH_HNR), 'tracker.on_hnr') is True

    def test_snapshot_fetched_once_per_run(self, evaluator):
        for _ in range(5):
            evaluator.get_field_value(torrent(HASH_REQUIRED), 'tracker.status')
            evaluator.get_field_value(torrent(HASH_HNR), 'tracker.account.ratio')
        assert evaluator.trackie.calls == 1
        evaluator.reset_trackie()
        evaluator.get_field_value(torrent(HASH_REQUIRED), 'tracker.status')
        assert evaluator.trackie.calls == 2


# ============================================================================
# Account fields
# ============================================================================

class TestAccountFields:

    def test_account_from_matched_record(self, evaluator):
        t = torrent(HASH_REQUIRED)
        assert evaluator.get_field_value(t, 'tracker.account.ratio') == 0.8
        assert evaluator.get_field_value(t, 'tracker.account.min_ratio') == 1.0
        assert evaluator.get_field_value(t, 'tracker.account.ratio_ok') is False
        assert evaluator.get_field_value(t, 'tracker.account.hnr_count') == 1
        assert evaluator.get_field_value(t, 'tracker.account.hnr_grace') == 432000
        assert evaluator.get_field_value(t, 'tracker.account.age') == 1200

    def test_account_from_announce_host_for_unmatched_torrent(self, evaluator):
        evaluator.trackers_cache[HASH_UNKNOWN] = [
            {'url': 'https://sync.td-peers.com/announce.php?passkey=x'},
            {'url': '** [DHT] **'},
        ]
        t = torrent(HASH_UNKNOWN)
        assert evaluator.get_field_value(t, 'tracker.account.ratio_ok') is True
        # ...while its own record is still unknown
        with pytest.raises(TrackerDataNotReadyError):
            evaluator.get_field_value(t, 'tracker.status')

    def test_torrent_on_an_uncovered_tracker(self, evaluator):
        evaluator.trackers_cache[HASH_UNKNOWN] = [{'url': 'https://tracker.example.org/announce'}]
        with pytest.raises(TrackerDataNotReadyError):
            evaluator.get_field_value(torrent(HASH_UNKNOWN), 'tracker.account.ratio')


# ============================================================================
# Fail-safe behaviour
# ============================================================================

class TestFailSafe:

    def test_no_record_is_not_ready(self, evaluator):
        with pytest.raises(DataNotReadyError):
            evaluator.get_field_value(torrent(HASH_UNKNOWN), 'tracker.status')

    def test_stale_tracker_is_not_ready(self, mock_api):
        ev = ConditionEvaluator(mock_api)
        ev.trackie = FakeTrackie(snapshot_doc(td_fetched=STALE))
        with pytest.raises(TrackerDataNotReadyError, match="Tracker data"):
            ev.get_field_value(torrent(HASH_HNR), 'tracker.status')
        # The other tracker is still fresh
        assert ev.get_field_value(torrent(HASH_REQUIRED), 'tracker.status') == 'required'

    def test_unreachable_is_not_ready_and_tried_once(self, mock_api):
        ev = ConditionEvaluator(mock_api)
        ev.trackie = FakeTrackie(error="trackie unreachable at http://trackie:8080 (ConnectionError)")
        for _ in range(3):
            with pytest.raises(TrackerDataNotReadyError):
                ev.get_field_value(torrent(HASH_REQUIRED), 'tracker.status')
        assert ev.trackie.calls == 1

    def test_condition_skips_instead_of_matching(self, mock_api):
        """A none: group must not vacuously match when trackie is down."""
        ev = ConditionEvaluator(mock_api)
        ev.trackie = FakeTrackie(error="down")
        conditions = {'none': [{'field': 'tracker.status', 'operator': '==', 'value': 'hnr'}]}
        assert ev.evaluate(torrent(HASH_REQUIRED), conditions) is False

    def test_satisfied_condition_end_to_end(self, evaluator):
        conditions = {'all': [
            {'field': 'tracker.status', 'operator': '==', 'value': 'satisfied'},
            {'field': 'tracker.match', 'operator': '==', 'value': 'exact'},
        ]}
        assert evaluator.evaluate(torrent(HASH_SATISFIED), conditions) is False  # fuzzy
        conditions['all'][1]['value'] = 'fuzzy'
        assert evaluator.evaluate(torrent(HASH_SATISFIED), conditions) is True

    def test_null_remaining_does_not_match_numeric(self, evaluator):
        conditions = {'all': [{'field': 'tracker.remaining', 'operator': '<=', 'value': 3600}]}
        assert evaluator.evaluate(torrent(HASH_SATISFIED), conditions) is False
        assert evaluator.evaluate(torrent(HASH_REQUIRED), conditions) is True


# ============================================================================
# Configuration errors
# ============================================================================

class TestConfiguration:

    def test_not_configured_is_a_field_error(self, mock_api):
        with pytest.raises(FieldError, match="integrations.trackie.url"):
            ConditionEvaluator(mock_api).get_field_value(torrent(HASH_REQUIRED), 'tracker.status')

    @pytest.mark.parametrize('field', ['tracker.bogus', 'tracker.account.bogus', 'tracker.account.status'])
    def test_unknown_field_is_a_field_error(self, evaluator, field):
        with pytest.raises(FieldError, match="Unknown tracker field"):
            evaluator.get_field_value(torrent(HASH_REQUIRED), field)

    @pytest.mark.parametrize(('value', 'expected'), [
        (None, DEFAULT_MAX_AGE_S), ('', DEFAULT_MAX_AGE_S), (7200, 7200), ('5400', 5400),
        ('2 hours', 7200), ('90 minutes', 5400), ('soon', DEFAULT_MAX_AGE_S),
    ])
    def test_parse_max_age(self, value, expected):
        assert parse_max_age(value) == expected

    def test_engine_builds_client_only_when_url_set(self, mock_api):
        config = Mock()
        assert RulesEngine(mock_api, config).evaluator.trackie is None
        engine = RulesEngine(mock_api, config, integrations_config={
            'trackie': {'url': 'http://trackie:8080/', 'max_age': '2 hours', 'timeout': '5'},
        })
        client = engine.evaluator.trackie
        assert isinstance(client, TrackieClient)
        assert (client.url, client.max_age_s, client.timeout_s) == ('http://trackie:8080', 7200, 5.0)


# ============================================================================
# HTTP client
# ============================================================================

class TestTrackieClient:

    def _client(self, response=None, exc=None):
        session = Mock()
        if exc:
            session.get.side_effect = exc
        else:
            session.get.return_value = response
        return TrackieClient('http://trackie:8080', 3600, session=session), session

    def test_success(self):
        resp = Mock()
        resp.json.return_value = snapshot_doc()
        client, session = self._client(resp)
        snap, err = client.fetch()
        assert err is None and snap.record(HASH_REQUIRED)['tracker_id'] == 1
        session.get.assert_called_once_with('http://trackie:8080/snapshot', timeout=10)

    @pytest.mark.parametrize(('kwargs', 'reason'), [
        ({'exc': requests.ConnectionError('boom')}, 'unreachable'),
        ({'exc': requests.Timeout('slow')}, 'unreachable'),
    ])
    def test_network_errors(self, kwargs, reason):
        client, _ = self._client(**kwargs)
        snap, err = client.fetch()
        assert snap is None and reason in err

    def test_http_error(self):
        resp = Mock()
        resp.raise_for_status.side_effect = requests.HTTPError('503')
        client, _ = self._client(resp)
        assert client.fetch()[0] is None

    def test_not_json_or_wrong_shape(self):
        resp = Mock()
        resp.json.side_effect = ValueError()
        client, _ = self._client(resp)
        assert 'JSON' in client.fetch()[1]
        resp2 = Mock()
        resp2.json.return_value = {'status': 'ok'}
        client2, _ = self._client(resp2)
        assert 'unexpected' in client2.fetch()[1]


# ============================================================================
# Notify templates
# ============================================================================

class TestNotifyTemplates:

    def test_tracker_fields_render_and_missing_ones_are_empty(self, mock_api, evaluator):
        from qbt_rules.engine import ActionExecutor
        executor = ActionExecutor(mock_api, dry_run=True, field_resolver=evaluator)
        msg = "${tracker.status} on ${tracker.tracker}, gap ${tracker.gap}"
        assert executor._render_notify_message(torrent(HASH_HNR), msg) == "hnr on td, gap 900"
        # No record for this torrent: rendered empty, not a failed notification
        assert executor._render_notify_message(torrent(HASH_UNKNOWN), msg) == " on , gap "
