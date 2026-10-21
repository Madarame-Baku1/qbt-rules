"""Tests for the set_file_priority action (ActionExecutor._execute_set_file_priority)."""

import logging
from unittest.mock import Mock, patch

import pytest

from qbt_rules.api import QBittorrentAPI
from qbt_rules.engine import ActionExecutor, ConditionEvaluator


def _files():
    return [
        {'index': 0, 'name': 'Movie/movie.mkv', 'priority': 1},
        {'index': 1, 'name': 'Movie/Sample/sample.mkv', 'priority': 1},
        {'index': 2, 'name': 'Movie/movie.nfo', 'priority': 1},
        {'index': 3, 'name': 'Movie/Extras/behind-the-scenes.mkv', 'priority': 0},
    ]


@pytest.fixture
def api_with_files(mock_api, sample_torrent):
    mock_api.files_data[sample_torrent['hash']] = _files()
    return mock_api


def _run(api, torrent, params, dry_run=False, field_resolver=None):
    executor = ActionExecutor(api, dry_run=dry_run, field_resolver=field_resolver)
    return executor.execute(torrent, {'type': 'set_file_priority', 'params': params})


class TestSetFilePriority:

    def test_pattern_selects_matching_files(self, api_with_files, sample_torrent):
        success, skipped = _run(api_with_files, sample_torrent,
                                {'priority': 0, 'pattern': '(?i)sample'})

        assert (success, skipped) == (True, False)
        assert api_with_files.calls['set_file_priority'] == [(sample_torrent['hash'], [1], 0)]

    def test_no_pattern_means_every_file(self, api_with_files, sample_torrent):
        _run(api_with_files, sample_torrent, {'priority': 7})

        assert api_with_files.calls['set_file_priority'] == [(sample_torrent['hash'], [0, 1, 2, 3], 7)]

    def test_files_already_at_priority_are_left_out(self, api_with_files, sample_torrent):
        """Extras is already 0 -- only the sample needs changing"""
        _run(api_with_files, sample_torrent, {'priority': 0, 'pattern': '(?i)(sample|extras)/'})

        assert api_with_files.calls['set_file_priority'] == [(sample_torrent['hash'], [1], 0)]

    @pytest.mark.parametrize('name, value', [('skip', 0), ('normal', 1), ('high', 6),
                                             ('MAXIMUM', 7), ('6', 6)])
    def test_priority_names_and_strings(self, api_with_files, sample_torrent, name, value):
        # 4 isn't one of the accepted values, so every case is a real change
        api_with_files.files_data[sample_torrent['hash']][2]['priority'] = 4
        _run(api_with_files, sample_torrent, {'priority': name, 'pattern': r'\.nfo$'})

        assert api_with_files.calls['set_file_priority'] == [(sample_torrent['hash'], [2], value)]

    def test_index_falls_back_to_position(self, mock_api, sample_torrent):
        """Older qBittorrent versions don't report an index per file"""
        mock_api.files_data[sample_torrent['hash']] = [
            {'name': 'a.mkv', 'priority': 1}, {'name': 'b.txt', 'priority': 1},
        ]
        _run(mock_api, sample_torrent, {'priority': 'skip', 'pattern': r'\.txt$'})

        assert mock_api.calls['set_file_priority'] == [(sample_torrent['hash'], [1], 0)]


class TestSetFilePriorityIdempotency:

    def test_skipped_when_nothing_to_change(self, api_with_files, sample_torrent):
        success, skipped = _run(api_with_files, sample_torrent,
                                {'priority': 0, 'pattern': '(?i)extras'})

        assert (success, skipped) == (True, True)
        assert api_with_files.calls['set_file_priority'] == []

    def test_skipped_when_no_file_matches(self, api_with_files, sample_torrent):
        success, skipped = _run(api_with_files, sample_torrent, {'priority': 0, 'pattern': r'\.exe$'})

        assert (success, skipped) == (True, True)
        assert api_with_files.calls['set_file_priority'] == []

    def test_second_run_in_same_job_is_skipped(self, api_with_files, sample_torrent):
        """The shared files cache is updated after a change, so a repeat is a no-op"""
        evaluator = ConditionEvaluator(api_with_files)
        params = {'priority': 0, 'pattern': '(?i)sample'}

        _run(api_with_files, sample_torrent, params, field_resolver=evaluator)
        success, skipped = _run(api_with_files, sample_torrent, params, field_resolver=evaluator)

        assert (success, skipped) == (True, True)
        assert len(api_with_files.calls['set_file_priority']) == 1

    def test_uses_evaluator_files_cache(self, api_with_files, sample_torrent):
        """A rule that matched on files.* already fetched the list -- don't fetch it again"""
        evaluator = ConditionEvaluator(api_with_files)
        evaluator.get_field_value(sample_torrent, 'files.name')
        api_with_files.get_files = Mock(side_effect=AssertionError("fetched twice"))

        success, _ = _run(api_with_files, sample_torrent, {'priority': 0, 'pattern': 'sample'},
                          field_resolver=evaluator)

        assert success is True


class TestSetFilePriorityFailures:

    @pytest.mark.parametrize('priority', [None, 3, 'low', -1, True])
    def test_invalid_priority(self, api_with_files, sample_torrent, priority, caplog):
        with caplog.at_level(logging.ERROR):
            success, skipped = _run(api_with_files, sample_torrent, {'priority': priority})

        assert (success, skipped) == (False, False)
        assert api_with_files.calls['set_file_priority'] == []
        assert caplog.text.count('invalid priority') == 1  # not logged twice

    def test_invalid_pattern(self, api_with_files, sample_torrent, caplog):
        with caplog.at_level(logging.ERROR):
            success, _ = _run(api_with_files, sample_torrent, {'priority': 0, 'pattern': '(['})

        assert success is False
        assert 'invalid pattern' in caplog.text
        assert api_with_files.calls['set_file_priority'] == []

    def test_metadata_not_loaded(self, mock_api, sample_torrent, caplog):
        """An empty file list means metadata isn't loaded, not "no files": don't skip as done"""
        with caplog.at_level(logging.WARNING):
            success, skipped = _run(mock_api, sample_torrent, {'priority': 0})

        assert (success, skipped) == (False, False)
        assert 'not loaded yet' in caplog.text
        assert mock_api.calls['set_file_priority'] == []


class TestSetFilePriorityDryRun:

    def test_dry_run_logs_count_without_calling_api(self, api_with_files, sample_torrent, caplog):
        with caplog.at_level(logging.INFO):
            success, skipped = _run(api_with_files, sample_torrent,
                                    {'priority': 'skip', 'pattern': '(?i)sample'}, dry_run=True)

        assert (success, skipped) == (True, True)
        assert 'Would set priority 0 on 1 file(s)' in caplog.text
        assert api_with_files.calls['set_file_priority'] == []


class TestSetFilePriorityApi:

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_calls_torrents_file_priority(self, mock_client_class):
        mock_client = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client_class.return_value = mock_client

        api = QBittorrentAPI('http://localhost:8080', 'admin', 'password')
        result = api.set_file_priority('abc123', [1, 3], 0)

        assert result is True
        mock_client.torrents_file_priority.assert_called_once_with(
            torrent_hash='abc123', file_ids=[1, 3], priority=0
        )
