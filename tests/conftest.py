"""Pytest configuration and comprehensive fixtures for qbt-rules test suite."""

import atexit
import shutil
import pytest
import tempfile
import os
from typing import Dict, Any, List, Optional
from unittest.mock import Mock, MagicMock
from pathlib import Path

# prometheus_client resolves its multiprocess-vs-single-process value
# storage exactly once, at its own first import anywhere in the process --
# changing PROMETHEUS_MULTIPROC_DIR afterward has no effect, since the
# resolved ValueClass is cached module-level state for the rest of the
# process's life. This must therefore be set here, at the very top of the
# first file pytest loads (before any test module's own `from qbt_rules
# import metrics`/engine.py/worker.py/etc. imports can trigger prometheus_client's
# actual first import), not inside a per-test fixture in test_metrics.py.
_metrics_test_dir = tempfile.mkdtemp(prefix='qbtr-test-metrics-')
os.environ.setdefault('PROMETHEUS_MULTIPROC_DIR', _metrics_test_dir)
atexit.register(shutil.rmtree, _metrics_test_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_metrics_module_state():
    """
    Reset qbt_rules.metrics's module-level enabled flag and Counter/Histogram
    references before and after every test in the whole suite

    Applies across every test file (not just test_metrics.py) since
    test_server.py's /metrics tests also call metrics.init(enabled=True),
    and without this reset that would otherwise leak "enabled" state into
    every test that runs afterward in the same pytest session.
    """
    from qbt_rules import metrics

    def _reset():
        metrics._enabled = False
        metrics._actions_executed = None
        metrics._job_duration = None
        metrics._scheduler_fires = None
        metrics._http_requests = None
        metrics._http_duration = None

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def clean_environment_variables():
    """Clean up environment variables before and after each test."""
    # Store original value
    original_dry_run = os.environ.get('QBT_RULES_DRY_RUN')

    # Clean before test
    os.environ.pop('QBT_RULES_DRY_RUN', None)

    yield

    # Clean after test
    os.environ.pop('QBT_RULES_DRY_RUN', None)

    # Restore original value if it existed
    if original_dry_run is not None:
        os.environ['QBT_RULES_DRY_RUN'] = original_dry_run


# ============================================================================
# Mock QBittorrent API
# ============================================================================

class MockQBittorrentAPI:
    """Mock QBittorrentAPI client for testing without real qBittorrent instance."""

    def __init__(self):
        self.torrents_data = {}
        self.trackers_data = {}
        self.files_data = {}
        self.peers_data = {}
        self.properties_data = {}
        self.webseeds_data = {}
        self.transfer_info_data = {
            'dl_info_speed': 1048576,  # 1 MB/s
            'up_info_speed': 524288,   # 512 KB/s
            'dl_info_data': 1073741824,  # 1 GB
            'up_info_data': 2147483648,  # 2 GB
        }
        self.app_preferences = {
            'locale': 'en',
            'save_path': '/downloads',
        }

        # Track API calls for verification
        self.calls = {
            'stop': [],
            'start': [],
            'force_start': [],
            'release_force_start': [],
            'recheck': [],
            'reannounce': [],
            'delete': [],
            'set_category': [],
            'add_tags': [],
            'remove_tags': [],
            'set_upload_limit': [],
            'set_download_limit': [],
            'increase_priority': [],
            'decrease_priority': [],
            'set_top_priority': [],
            'set_bottom_priority': [],
            'set_file_priority': [],
        }

    def get_torrent(self, torrent_hash):
        """Get a single torrent by hash, or None if it doesn't exist (e.g. deleted)."""
        return self.torrents_data.get(torrent_hash)

    def get_torrents(self, category=None, tag=None, hashes=None):
        """Get torrents list with optional filters."""
        torrents = list(self.torrents_data.values())

        if category:
            torrents = [t for t in torrents if t.get('category') == category]
        if tag:
            torrents = [t for t in torrents if tag in t.get('tags', '').split(',')]
        if hashes:
            torrents = [t for t in torrents if t['hash'] in hashes]

        return torrents

    def get_trackers(self, torrent_hash):
        """Get trackers for a torrent."""
        return self.trackers_data.get(torrent_hash, [])

    def get_files(self, torrent_hash):
        """Get files for a torrent."""
        return self.files_data.get(torrent_hash, [])

    def get_peers(self, torrent_hash):
        """Get peers for a torrent."""
        return self.peers_data.get(torrent_hash, {})

    def get_properties(self, torrent_hash):
        """Get properties for a torrent."""
        return self.properties_data.get(torrent_hash, {})

    def get_webseeds(self, torrent_hash):
        """Get webseeds for a torrent."""
        return self.webseeds_data.get(torrent_hash, [])

    def get_transfer_info(self):
        """Get global transfer info."""
        return self.transfer_info_data

    def get_app_preferences(self):
        """Get application preferences."""
        return self.app_preferences

    def get_app_version(self):
        """Get application version."""
        return "v5.0.0"

    def get_api_version(self):
        """Get API version."""
        return "v2.9.3"

    # Action methods that track calls
    def stop_torrents(self, hashes):
        """Stop torrents."""
        self.calls['stop'].append(hashes)
        for hash in hashes:
            if hash in self.torrents_data:
                self.torrents_data[hash]['state'] = 'pausedDL'
        return True

    def start_torrents(self, hashes):
        """Start torrents."""
        self.calls['start'].append(hashes)
        for hash in hashes:
            if hash in self.torrents_data:
                self.torrents_data[hash]['state'] = 'downloading'
        return True

    def force_start_torrents(self, hashes, enable=True):
        """Force start torrents, or release them back to normal queue management if enable=False."""
        if enable:
            self.calls['force_start'].append(hashes)
        else:
            self.calls['release_force_start'].append(hashes)
        for hash in hashes:
            if hash in self.torrents_data:
                self.torrents_data[hash]['force_start'] = enable
                if enable:
                    self.torrents_data[hash]['state'] = 'forceDL'
        return True

    def recheck_torrents(self, hashes):
        """Recheck torrents."""
        self.calls['recheck'].append(hashes)
        return True

    def reannounce_torrents(self, hashes):
        """Reannounce torrents."""
        self.calls['reannounce'].append(hashes)
        return True

    def delete_torrents(self, hashes, delete_files=False):
        """Delete torrents."""
        self.calls['delete'].append({'hashes': hashes, 'delete_files': delete_files})
        for hash in hashes:
            if hash in self.torrents_data:
                del self.torrents_data[hash]
        return True

    def set_category(self, hashes, category):
        """Set torrent category."""
        self.calls['set_category'].append({'hashes': hashes, 'category': category})
        for hash in hashes:
            if hash in self.torrents_data:
                self.torrents_data[hash]['category'] = category
        return True

    def add_tags(self, hashes, tags):
        """Add tags to torrents."""
        self.calls['add_tags'].append({'hashes': hashes, 'tags': tags})
        for hash in hashes:
            if hash in self.torrents_data:
                current = self.torrents_data[hash].get('tags', '')
                current_list = [t.strip() for t in current.split(',') if t.strip()]
                # tags is already a list
                new_tags = [t.strip() for t in tags if t.strip()] if isinstance(tags, list) else [tags.strip()]
                combined = list(set(current_list + new_tags))
                self.torrents_data[hash]['tags'] = ','.join(combined)
        return True

    def remove_tags(self, hashes, tags):
        """Remove tags from torrents."""
        self.calls['remove_tags'].append({'hashes': hashes, 'tags': tags})
        for hash in hashes:
            if hash in self.torrents_data:
                current = self.torrents_data[hash].get('tags', '')
                current_list = [t.strip() for t in current.split(',') if t.strip()]
                # tags is already a list
                remove_tags = [t.strip() for t in tags if t.strip()] if isinstance(tags, list) else [tags.strip()]
                remaining = [t for t in current_list if t not in remove_tags]
                self.torrents_data[hash]['tags'] = ','.join(remaining)
        return True

    def set_upload_limit(self, hashes, limit):
        """Set upload limit."""
        self.calls['set_upload_limit'].append({'hashes': hashes, 'limit': limit})
        for hash in hashes:
            if hash in self.torrents_data:
                self.torrents_data[hash]['up_limit'] = limit
        return True

    def set_download_limit(self, hashes, limit):
        """Set download limit."""
        self.calls['set_download_limit'].append({'hashes': hashes, 'limit': limit})
        for hash in hashes:
            if hash in self.torrents_data:
                self.torrents_data[hash]['dl_limit'] = limit
        return True

    def increase_priority(self, hashes):
        """Increase torrent priority."""
        self.calls['increase_priority'].append(hashes)
        return True

    def decrease_priority(self, hashes):
        """Decrease torrent priority."""
        self.calls['decrease_priority'].append(hashes)
        return True

    def set_top_priority(self, hashes):
        """Set maximum torrent priority."""
        self.calls['set_top_priority'].append(hashes)
        return True

    def set_bottom_priority(self, hashes):
        """Set minimum torrent priority."""
        self.calls['set_bottom_priority'].append(hashes)
        return True

    def set_file_priority(self, torrent_hash, file_ids, priority):
        """Set the priority of individual files in one torrent."""
        self.calls['set_file_priority'].append((torrent_hash, list(file_ids), priority))
        return True


@pytest.fixture
def mock_api():
    """Create a mock QBittorrentAPI instance."""
    return MockQBittorrentAPI()


# ============================================================================
# Torrent Fixtures - Various States
# ============================================================================

@pytest.fixture
def sample_torrent() -> Dict[str, Any]:
    """Basic sample torrent - seeding, good ratio."""
    return {
        "hash": "abc123def456",
        "name": "Example.Torrent.1080p",
        "size": 1073741824,  # 1 GB
        "progress": 1.0,
        "dlspeed": 0,
        "upspeed": 524288,  # 512 KB/s
        "downloaded": 1073741824,
        "uploaded": 2147483648,  # 2 GB
        "ratio": 2.0,
        "num_complete": 5,
        "num_incomplete": 2,
        "num_leechs": 2,
        "num_seeds": 5,
        "state": "uploading",
        "category": "",
        "tags": "",
        "added_on": 1700000000,
        "completion_on": 1700010000,
        "last_activity": 1700020000,
        "availability": 1.0,
        "up_limit": -1,
        "dl_limit": -1,
    }


@pytest.fixture
def downloading_torrent() -> Dict[str, Any]:
    """Torrent currently downloading."""
    return {
        "hash": "download123",
        "name": "Downloading.Movie.2160p",
        "size": 5368709120,  # 5 GB
        "progress": 0.45,
        "dlspeed": 2097152,  # 2 MB/s
        "upspeed": 0,
        "downloaded": 2415919104,  # ~2.25 GB
        "uploaded": 0,
        "ratio": 0.0,
        "num_complete": 10,
        "num_incomplete": 5,
        "num_leechs": 3,
        "num_seeds": 10,
        "state": "downloading",
        "category": "movies",
        "tags": "hd,new",
        "added_on": 1702000000,
        "completion_on": -1,
        "last_activity": 1702001000,
        "availability": 2.5,
        "up_limit": -1,
        "dl_limit": -1,
    }


@pytest.fixture
def seeding_torrent() -> Dict[str, Any]:
    """Torrent seeding with active upload."""
    return {
        "hash": "seed789",
        "name": "Popular.Show.S01E01.720p",
        "size": 2147483648,  # 2 GB
        "progress": 1.0,
        "dlspeed": 0,
        "upspeed": 1048576,  # 1 MB/s
        "downloaded": 2147483648,
        "uploaded": 10737418240,  # 10 GB
        "ratio": 5.0,
        "num_complete": 50,
        "num_incomplete": 10,
        "num_leechs": 8,
        "num_seeds": 50,
        "state": "uploading",
        "category": "tv",
        "tags": "tv,popular",
        "added_on": 1701000000,
        "completion_on": 1701005000,
        "last_activity": 1702005000,
        "availability": 1.0,
        "up_limit": -1,
        "dl_limit": -1,
    }


@pytest.fixture
def paused_torrent() -> Dict[str, Any]:
    """Paused torrent."""
    return {
        "hash": "paused456",
        "name": "Paused.Torrent",
        "size": 536870912,  # 512 MB
        "progress": 1.0,
        "dlspeed": 0,
        "upspeed": 0,
        "downloaded": 536870912,
        "uploaded": 268435456,  # 256 MB
        "ratio": 0.5,
        "num_complete": 3,
        "num_incomplete": 1,
        "num_leechs": 1,
        "num_seeds": 3,
        "state": "pausedUP",
        "category": "misc",
        "tags": "paused",
        "added_on": 1700500000,
        "completion_on": 1700510000,
        "last_activity": 1700520000,
        "availability": 1.0,
        "up_limit": -1,
        "dl_limit": -1,
    }


@pytest.fixture
def completed_torrent() -> Dict[str, Any]:
    """Recently completed torrent."""
    import time
    now = int(time.time())
    return {
        "hash": "completed999",
        "name": "Just.Completed.1080p",
        "size": 3221225472,  # 3 GB
        "progress": 1.0,
        "dlspeed": 0,
        "upspeed": 262144,  # 256 KB/s
        "downloaded": 3221225472,
        "uploaded": 322122547,  # ~307 MB
        "ratio": 0.1,
        "num_complete": 8,
        "num_incomplete": 4,
        "num_leechs": 4,
        "num_seeds": 8,
        "state": "uploading",
        "category": "movies",
        "tags": "new,completed",
        "added_on": now - 7200,  # 2 hours ago
        "completion_on": now - 300,  # 5 minutes ago
        "last_activity": now - 60,  # 1 minute ago
        "availability": 1.0,
        "up_limit": -1,
        "dl_limit": -1,
    }


@pytest.fixture
def old_seeded_torrent() -> Dict[str, Any]:
    """Old torrent that has been seeding for a long time."""
    import time
    now = int(time.time())
    return {
        "hash": "oldseeder",
        "name": "Old.Torrent.2020",
        "size": 1073741824,  # 1 GB
        "progress": 1.0,
        "dlspeed": 0,
        "upspeed": 0,
        "downloaded": 1073741824,
        "uploaded": 5368709120,  # 5 GB
        "ratio": 5.0,
        "num_complete": 2,
        "num_incomplete": 0,
        "num_leechs": 0,
        "num_seeds": 2,
        "state": "stalledUP",
        "category": "old",
        "tags": "archived,old",
        "added_on": now - 7776000,  # 90 days ago
        "completion_on": now - 7776000 + 3600,  # Completed 90 days ago
        "last_activity": now - 86400,  # Last active 1 day ago
        "availability": 1.0,
        "up_limit": -1,
        "dl_limit": -1,
    }


@pytest.fixture
def large_torrent() -> Dict[str, Any]:
    """Very large torrent (100 GB)."""
    return {
        "hash": "largetorrent",
        "name": "Huge.Collection.4K.UHD",
        "size": 107374182400,  # 100 GB
        "progress": 0.15,
        "dlspeed": 5242880,  # 5 MB/s
        "upspeed": 0,
        "downloaded": 16106127360,  # ~15 GB
        "uploaded": 0,
        "ratio": 0.0,
        "num_complete": 5,
        "num_incomplete": 20,
        "num_leechs": 15,
        "num_seeds": 5,
        "state": "downloading",
        "category": "large",
        "tags": "4k,uhd,large",
        "added_on": 1702000000,
        "completion_on": -1,
        "last_activity": 1702001000,
        "availability": 0.8,
        "up_limit": -1,
        "dl_limit": -1,
    }


@pytest.fixture
def small_torrent() -> Dict[str, Any]:
    """Very small torrent (10 MB)."""
    return {
        "hash": "smalltorrent",
        "name": "Small.File.pdf",
        "size": 10485760,  # 10 MB
        "progress": 1.0,
        "dlspeed": 0,
        "upspeed": 10240,  # 10 KB/s
        "downloaded": 10485760,
        "uploaded": 52428800,  # 50 MB
        "ratio": 5.0,
        "num_complete": 100,
        "num_incomplete": 5,
        "num_leechs": 5,
        "num_seeds": 100,
        "state": "uploading",
        "category": "docs",
        "tags": "small,document",
        "added_on": 1701000000,
        "completion_on": 1701000100,
        "last_activity": 1702000000,
        "availability": 1.0,
        "up_limit": -1,
        "dl_limit": -1,
    }


# ============================================================================
# Collection Fixtures - Trackers, Files, Peers, etc.
# ============================================================================

@pytest.fixture
def mock_trackers() -> List[Dict[str, Any]]:
    """Mock tracker data."""
    return [
        {
            "url": "http://tracker1.example.com:8080/announce",
            "status": 2,  # Working
            "tier": 0,
            "num_peers": 50,
            "num_seeds": 30,
            "num_leeches": 20,
            "num_downloaded": 500,
            "msg": "",
        },
        {
            "url": "udp://tracker2.example.com:6969/announce",
            "status": 2,  # Working
            "tier": 1,
            "num_peers": 25,
            "num_seeds": 15,
            "num_leeches": 10,
            "num_downloaded": 250,
            "msg": "",
        },
        {
            "url": "http://tracker3.example.com/announce",
            "status": 0,  # Not contacted
            "tier": 2,
            "num_peers": 0,
            "num_seeds": 0,
            "num_leeches": 0,
            "num_downloaded": 0,
            "msg": "Not contacted yet",
        },
    ]


@pytest.fixture
def mock_files() -> List[Dict[str, Any]]:
    """Mock file data."""
    return [
        {
            "index": 0,
            "name": "Movie.1080p.mkv",
            "size": 2147483648,  # 2 GB
            "progress": 1.0,
            "priority": 1,
            "is_seed": True,
            "piece_range": [0, 2047],
            "availability": 1.0,
        },
        {
            "index": 1,
            "name": "subtitles.srt",
            "size": 52428,  # ~51 KB
            "progress": 1.0,
            "priority": 1,
            "is_seed": True,
            "piece_range": [2048, 2048],
            "availability": 1.0,
        },
        {
            "index": 2,
            "name": "sample.mkv",
            "size": 10485760,  # 10 MB
            "progress": 0.0,
            "priority": 0,  # Don't download
            "is_seed": False,
            "piece_range": [2049, 2058],
            "availability": 0.0,
        },
    ]


@pytest.fixture
def mock_peers() -> Dict[str, Dict[str, Any]]:
    """Mock peer data."""
    return {
        "192.168.1.100:51413": {
            "client": "qBittorrent/4.5.0",
            "connection": "bt",
            "country": "US",
            "country_code": "us",
            "dl_speed": 524288,  # 512 KB/s
            "downloaded": 1073741824,  # 1 GB
            "files": "",
            "flags": "d E ",
            "flags_desc": "d = interested, E = encrypted",
            "ip": "192.168.1.100",
            "port": 51413,
            "progress": 0.5,
            "relevance": 1.0,
            "up_speed": 0,
            "uploaded": 0,
        },
        "10.0.0.50:6881": {
            "client": "Transmission/3.0",
            "connection": "bt",
            "country": "CA",
            "country_code": "ca",
            "dl_speed": 0,
            "downloaded": 0,
            "files": "",
            "flags": "u P ",
            "flags_desc": "u = not interested, P = encrypted",
            "ip": "10.0.0.50",
            "port": 6881,
            "progress": 1.0,
            "relevance": 1.0,
            "up_speed": 262144,  # 256 KB/s
            "uploaded": 536870912,  # 512 MB
        },
    }


@pytest.fixture
def mock_properties() -> Dict[str, Any]:
    """Mock torrent properties."""
    return {
        "save_path": "/downloads/torrents/",
        "creation_date": 1700000000,
        "piece_size": 1048576,  # 1 MB
        "comment": "Test torrent comment",
        "total_wasted": 0,
        "total_uploaded": 2147483648,
        "total_uploaded_session": 1073741824,
        "total_downloaded": 1073741824,
        "total_downloaded_session": 1073741824,
        "up_limit": -1,
        "dl_limit": -1,
        "time_elapsed": 10000,
        "seeding_time": 5000,
        "nb_connections": 50,
        "nb_connections_limit": 100,
        "share_ratio": 2.0,
        "addition_date": 1700000000,
        "completion_date": 1700010000,
        "created_by": "mktorrent 1.1",
        "dl_speed_avg": 524288,
        "dl_speed": 0,
        "eta": 8640000,
        "last_seen": 1700020000,
        "peers": 5,
        "peers_total": 50,
        "pieces_have": 1024,
        "pieces_num": 1024,
        "reannounce": 1800,
        "seeds": 3,
        "seeds_total": 30,
        "total_size": 1073741824,
        "up_speed_avg": 262144,
        "up_speed": 524288,
    }


@pytest.fixture
def mock_webseeds() -> List[Dict[str, Any]]:
    """Mock webseed data."""
    return [
        {
            "url": "http://webseed1.example.com/torrent/file.mkv",
        },
        {
            "url": "https://webseed2.example.com/downloads/file.mkv",
        },
    ]


@pytest.fixture
def mock_transfer_info() -> Dict[str, Any]:
    """Mock global transfer info."""
    return {
        "dl_info_speed": 2097152,  # 2 MB/s
        "dl_info_data": 107374182400,  # 100 GB total downloaded
        "up_info_speed": 1048576,  # 1 MB/s
        "up_info_data": 214748364800,  # 200 GB total uploaded
        "dl_rate_limit": 10485760,  # 10 MB/s limit
        "up_rate_limit": 5242880,  # 5 MB/s limit
        "dht_nodes": 500,
        "connection_status": "connected",
    }


@pytest.fixture
def mock_app_preferences() -> Dict[str, Any]:
    """Mock application preferences."""
    return {
        "locale": "en",
        "save_path": "/downloads/",
        "temp_path_enabled": False,
        "temp_path": "/downloads/temp/",
        "scan_dirs": {},
        "download_path": "/downloads/",
        "export_dir_enabled": False,
        "export_dir": "",
        "mail_notification_enabled": False,
        "mail_notification_sender": "",
        "mail_notification_email": "",
        "mail_notification_smtp": "",
        "mail_notification_ssl_enabled": False,
        "mail_notification_auth_enabled": False,
        "mail_notification_username": "",
        "mail_notification_password": "",
        "autorun_enabled": False,
        "autorun_program": "",
        "queueing_enabled": True,
        "max_active_downloads": 3,
        "max_active_torrents": 5,
        "max_active_uploads": 3,
        "dont_count_slow_torrents": False,
        "slow_torrent_dl_rate_threshold": 2,
        "slow_torrent_ul_rate_threshold": 2,
        "slow_torrent_inactive_timer": 60,
        "max_ratio_enabled": False,
        "max_ratio": 1.0,
        "max_ratio_act": 0,
        "listen_port": 6881,
        "upnp": True,
        "random_port": False,
        "dl_limit": -1,
        "up_limit": -1,
        "max_connec": 500,
        "max_connec_per_torrent": 100,
        "max_uploads": -1,
        "max_uploads_per_torrent": -1,
        "stop_tracker_timeout": 5,
        "enable_piece_extent_affinity": False,
        "bittorrent_protocol": 0,
        "limit_utp_rate": True,
        "limit_tcp_overhead": False,
        "limit_lan_peers": True,
        "alt_dl_limit": -1,
        "alt_up_limit": -1,
        "scheduler_enabled": False,
        "schedule_from_hour": 8,
        "schedule_from_min": 0,
        "schedule_to_hour": 20,
        "schedule_to_min": 0,
        "scheduler_days": 0,
        "dht": True,
        "pex": True,
        "lsd": True,
        "encryption": 0,
        "anonymous_mode": False,
        "proxy_type": -1,
        "proxy_ip": "",
        "proxy_port": 8080,
        "proxy_peer_connections": False,
        "proxy_auth_enabled": False,
        "proxy_username": "",
        "proxy_password": "",
        "proxy_torrents_only": False,
        "ip_filter_enabled": False,
        "ip_filter_path": "",
        "ip_filter_trackers": False,
        "web_ui_domain_list": "*",
        "web_ui_address": "*",
        "web_ui_port": 8080,
        "web_ui_upnp": False,
        "web_ui_username": "admin",
        "web_ui_password": "",
        "web_ui_csrf_protection_enabled": True,
        "web_ui_clickjacking_protection_enabled": True,
        "web_ui_secure_cookie_enabled": True,
        "web_ui_max_auth_fail_count": 5,
        "web_ui_ban_duration": 3600,
        "web_ui_session_timeout": 3600,
        "web_ui_host_header_validation_enabled": True,
        "bypass_local_auth": False,
        "bypass_auth_subnet_whitelist_enabled": False,
        "bypass_auth_subnet_whitelist": "",
        "alternative_webui_enabled": False,
        "alternative_webui_path": "",
        "use_https": False,
        "ssl_key": "",
        "ssl_cert": "",
        "web_ui_https_key_path": "",
        "web_ui_https_cert_path": "",
        "dyndns_enabled": False,
        "dyndns_service": 0,
        "dyndns_domain": "",
        "dyndns_username": "",
        "dyndns_password": "",
        "rss_refresh_interval": 30,
        "rss_max_articles_per_feed": 50,
        "rss_processing_enabled": False,
        "rss_auto_downloading_enabled": False,
        "rss_download_repack_proper_episodes": True,
        "rss_smart_episode_filters": "",
    }


# ============================================================================
# Rule Fixtures
# ============================================================================

@pytest.fixture
def simple_rule() -> Dict[str, Any]:
    """Simple rule with basic conditions."""
    return {
        "id": "simple-test-rule",
        "meta": {"description": "Simple test rule"},
        "enabled": True,
        "stop_on_match": False,
        "conditions": {
            "context": "adhoc-run",
            "all": [
                {"field": "info.ratio", "operator": ">=", "value": 1.0}
            ]
        },
        "actions": [
            {"type": "add_tag", "params": {"tag": "test"}}
        ]
    }


@pytest.fixture
def complex_rule() -> Dict[str, Any]:
    """Complex rule with nested conditions and multiple actions."""
    return {
        "name": "Complex test rule",
        "enabled": True,
        "stop_on_match": True,
        "conditions": {
            "context": "weekly-cleanup",
            "all": [
                {"field": "info.state", "operator": "in", "value": ["uploading", "stalledUP"]},
                {
                    "any": [
                        {"field": "info.ratio", "operator": ">=", "value": 2.0},
                        {"field": "info.completion_on", "operator": "older_than", "value": "30 days"},
                    ]
                }
            ]
        },
        "actions": [
            {"type": "add_tag", "params": {"tag": "ready-to-delete"}},
            {"type": "set_category", "params": {"category": "old"}},
            {"type": "stop"},
        ]
    }


# ============================================================================
# Config Fixtures
# ============================================================================

@pytest.fixture
def mock_config():
    """Mock qBittorrent configuration."""
    config = Mock()
    config.get_rules = Mock(return_value=[])
    config.get = Mock(return_value=None)
    config.config_dir = Path("/config")
    return config


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create temporary config directory with example files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Create config.yml
    config_yml = config_dir / "config.yml"
    config_yml.write_text("""
qbittorrent:
  host: http://localhost:8080
  username: admin
  password: adminpass

logging:
  level: INFO
  trace: false

dry_run: false
""")

    # Create rules.yml
    rules_yml = config_dir / "rules.yml"
    rules_yml.write_text("""
rules:
  - id: test-rule
    meta:
      description: "Test rule"
    enabled: true
    stop_on_match: false
    conditions:
      context: adhoc-run
      all:
        - field: info.ratio
          operator: ">="
          value: 1.0
    actions:
      - type: add_tag
        params:
          tag: "test"
""")

    return config_dir


# ============================================================================
# Redis Test Infrastructure
# ============================================================================

@pytest.fixture(scope="session")
def redis_available():
    """Check if Redis is available for testing."""
    try:
        import redis
        client = redis.Redis(host='localhost', port=6379, socket_connect_timeout=1)
        client.ping()
        client.close()
        return True
    except (ImportError, redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        return False


@pytest.fixture
def redis_client(redis_available):
    """
    Provide Redis client for testing.

    Uses real Redis if available, otherwise uses fakeredis.
    Skip tests that require real Redis if neither is available.
    """
    if redis_available:
        # Use real Redis
        import redis
        client = redis.Redis(
            host='localhost',
            port=6379,
            db=15,  # Use dedicated test database
            decode_responses=True  # Decode to strings (matches RedisQueue expectation)
        )
        # Clean up test database
        client.flushdb()
        yield client
        # Clean up after test
        client.flushdb()
        client.close()
    else:
        # Try to use fakeredis as fallback
        try:
            from fakeredis import FakeRedis
            client = FakeRedis(decode_responses=True)  # Decode to strings
            yield client
        except ImportError:
            pytest.skip("Redis not available and fakeredis not installed")


@pytest.fixture
def redis_url(redis_available):
    """Provide Redis URL for testing."""
    if redis_available:
        return "redis://localhost:6379/15"
    else:
        # Return fake URL - tests using this should mock the connection
        return "redis://localhost:6379/15"


def pytest_configure(config):
    """Configure pytest with custom markers and settings."""
    config.addinivalue_line(
        "markers", "requires_redis: mark test as requiring real Redis server"
    )
