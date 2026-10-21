"""
Tests for QBittorrentAPI class.
"""

import pytest


from qbt_rules.api import QBittorrentAPI as QBittorrentAPIv4
from qbt_rules.errors import AuthenticationError, ConnectionError, APIError
from unittest.mock import Mock, patch, MagicMock


class TestQBittorrentAPIv4Init:
    """Test QBittorrentAPI v0.4.0 initialization."""

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_init_creates_client(self, mock_client_class):
        """Should create qbittorrent-api Client"""
        mock_client = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password', connect_now=False)

        assert api.host == 'http://localhost:8080'
        assert api.username == 'admin'
        assert api.password == 'password'
        assert api._connected is False
        mock_client_class.assert_called_once_with(
            host='http://localhost:8080',
            username='admin',
            password='password'
        )

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_init_strips_trailing_slash(self, mock_client_class):
        """Should strip trailing slash from host"""
        api = QBittorrentAPIv4('http://localhost:8080/', 'admin', 'password', connect_now=False)
        assert api.host == 'http://localhost:8080'

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_init_with_connect_now_true(self, mock_client_class):
        """Should connect immediately when connect_now=True"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password', connect_now=True)

        assert api._connected is True
        mock_client.auth_log_in.assert_called_once()

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_init_login_failed_raises_auth_error(self, mock_client_class):
        """Should raise AuthenticationError when login fails"""
        import qbittorrentapi
        mock_client = Mock()
        original_error = qbittorrentapi.LoginFailed("Unauthorized")
        mock_client.auth_log_in = Mock(side_effect=original_error)
        mock_client_class.return_value = mock_client

        with pytest.raises(AuthenticationError) as error:
            QBittorrentAPIv4('http://localhost:8080', 'admin', 'wrong', connect_now=True)

        assert error.value.__cause__ is original_error

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_init_connection_error_raises(self, mock_client_class):
        """Should raise ConnectionError when cannot reach server"""
        import qbittorrentapi
        mock_client = Mock()
        original_error = qbittorrentapi.APIConnectionError("Connection refused")
        mock_client.auth_log_in = Mock(side_effect=original_error)
        mock_client_class.return_value = mock_client

        with pytest.raises(ConnectionError) as error:
            QBittorrentAPIv4('http://localhost:9999', 'admin', 'password', connect_now=True)

        assert error.value.__cause__ is original_error

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_init_generic_exception_raises_connection_error(self, mock_client_class):
        """Should raise ConnectionError for generic exceptions"""
        mock_client = Mock()
        original_error = Exception("Unexpected error")
        mock_client.auth_log_in = Mock(side_effect=original_error)
        mock_client_class.return_value = mock_client

        with pytest.raises(ConnectionError) as error:
            QBittorrentAPIv4('http://localhost:8080', 'admin', 'password', connect_now=True)

        assert error.value.__cause__ is original_error


class TestQBittorrentAPIv4TorrentInfo:
    """Test torrent information methods."""

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_get_torrents(self, mock_client_class):
        """Should get list of torrents"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')

        # Mock TorrentInfoList response
        mock_torrent = {'hash': 'abc123', 'name': 'test.torrent'}
        mock_client.torrents_info = Mock(return_value=[mock_torrent])
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        torrents = api.get_torrents()

        assert len(torrents) == 1
        assert torrents[0]['hash'] == 'abc123'
        mock_client.torrents_info.assert_called_once_with(
            status_filter=None,
            category=None,
            tag=None
        )

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_get_properties(self, mock_client_class):
        """Should get torrent properties"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_properties = Mock(return_value={'size': 1024})
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        props = api.get_properties('abc123')

        assert props['size'] == 1024

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_get_trackers(self, mock_client_class):
        """Should get torrent trackers"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_trackers = Mock(return_value=[{'url': 'http://tracker.example.com'}])
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        trackers = api.get_trackers('abc123')

        assert len(trackers) == 1
        assert trackers[0]['url'] == 'http://tracker.example.com'

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_get_files(self, mock_client_class):
        """Should get torrent files"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_files = Mock(return_value=[{'name': 'file.txt'}])
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        files = api.get_files('abc123')

        assert len(files) == 1
        assert files[0]['name'] == 'file.txt'

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_get_webseeds(self, mock_client_class):
        """Should get torrent webseeds"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_webseeds = Mock(return_value=[{'url': 'http://webseed.example.com'}])
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        webseeds = api.get_webseeds('abc123')

        assert len(webseeds) == 1

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_get_peers(self, mock_client_class):
        """Should get torrent peers"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.sync_torrent_peers = Mock(return_value={'peers': {}})
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        peers = api.get_peers('abc123')

        assert isinstance(peers, list)


class TestQBittorrentAPIv4GlobalInfo:
    """Test global information methods."""

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_get_transfer_info(self, mock_client_class):
        """Should get transfer info"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.transfer_info = Mock(return_value={'dl_speed': 1000})
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        info = api.get_transfer_info()

        assert info['dl_speed'] == 1000

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_get_app_preferences(self, mock_client_class):
        """Should get app preferences"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.app_preferences = Mock(return_value={'locale': 'en'})
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        prefs = api.get_app_preferences()

        assert prefs['locale'] == 'en'


class TestQBittorrentAPIv4TorrentControl:
    """Test torrent control methods."""

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_stop_torrents(self, mock_client_class):
        """Should stop torrents"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_pause = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.stop_torrents(['abc123'])

        assert result is True
        mock_client.torrents_pause.assert_called_once_with(torrent_hashes=['abc123'])

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_start_torrents(self, mock_client_class):
        """Should start torrents"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_resume = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.start_torrents(['abc123'])

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_force_start_torrents(self, mock_client_class):
        """Should force start torrents"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_set_force_start = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.force_start_torrents(['abc123'])

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_recheck_torrents(self, mock_client_class):
        """Should recheck torrents"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_recheck = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.recheck_torrents(['abc123'])

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_reannounce_torrents(self, mock_client_class):
        """Should reannounce torrents"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_reannounce = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.reannounce_torrents(['abc123'])

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_delete_torrents(self, mock_client_class):
        """Should delete torrents"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_delete = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.delete_torrents(['abc123'], delete_files=True)

        assert result is True
        mock_client.torrents_delete.assert_called_once_with(
            torrent_hashes=['abc123'],
            delete_files=True
        )


class TestQBittorrentAPIv4CategoryTags:
    """Test category and tag methods."""

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_set_category(self, mock_client_class):
        """Should set category"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_set_category = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.set_category(['abc123'], 'movies')

        assert result is True
        mock_client.torrents_set_category.assert_called_once_with(
            torrent_hashes=['abc123'],
            category='movies'
        )

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_add_tags(self, mock_client_class):
        """Should add tags"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_add_tags = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.add_tags(['abc123'], ['tag1', 'tag2'])

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_remove_tags(self, mock_client_class):
        """Should remove tags"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_remove_tags = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.remove_tags(['abc123'], ['tag1'])

        assert result is True


class TestQBittorrentAPIv4Limits:
    """Test limit setting methods."""

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_set_upload_limit(self, mock_client_class):
        """Should set upload limit"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_set_upload_limit = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.set_upload_limit(['abc123'], 1000000)

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_set_download_limit(self, mock_client_class):
        """Should set download limit"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_set_download_limit = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.set_download_limit(['abc123'], 1000000)

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_set_share_limits(self, mock_client_class):
        """Should set share limits"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_set_share_limits = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.set_share_limits(['abc123'], ratio_limit=2.0, seeding_time_limit=1440)

        assert result is True
        mock_client.torrents_set_share_limits.assert_called_once_with(
            torrent_hashes=['abc123'],
            ratio_limit=2.0,
            seeding_time_limit=1440
        )


class TestQBittorrentAPIv4Priority:
    """Test priority methods."""

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_increase_priority(self, mock_client_class):
        """Should increase priority"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_increase_priority = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.increase_priority(['abc123'])

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_decrease_priority(self, mock_client_class):
        """Should decrease priority"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_decrease_priority = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.decrease_priority(['abc123'])

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_set_top_priority(self, mock_client_class):
        """Should set top priority"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_top_priority = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.set_top_priority(['abc123'])

        assert result is True

    @patch('qbt_rules.api.qbittorrentapi.Client')
    def test_set_bottom_priority(self, mock_client_class):
        """Should set bottom priority"""
        mock_client = Mock()
        mock_client.auth_log_in = Mock()
        mock_client.app_version = Mock(return_value='v4.5.0')
        mock_client.app_web_api_version = Mock(return_value='2.8.0')
        mock_client.torrents_bottom_priority = Mock()
        mock_client_class.return_value = mock_client

        api = QBittorrentAPIv4('http://localhost:8080', 'admin', 'password')
        result = api.set_bottom_priority(['abc123'])

        assert result is True
