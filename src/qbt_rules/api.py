"""
qBittorrent Web API client - qbittorrent-api wrapper

Wraps qbittorrent-api package for multi-version support (v4.1+ through v5.1.4+)
Maintains backward compatibility with existing qbt-rules interface
"""

from typing import Dict, List, Optional

import qbittorrentapi

from qbt_rules.errors import AuthenticationError, ConnectionError
from qbt_rules.logging import get_logger

logger = get_logger(__name__)


class QBittorrentAPI:
    """
    qBittorrent Web API client wrapper

    Uses qbittorrent-api package for:
    - Multi-version support (v4.1+ through v5.1.4+)
    - Automatic version detection
    - Auto-managed authentication
    - Structured response types
    """

    def __init__(self, host: str, username: str, password: str, connect_now: bool = True):
        """
        Initialize API client and optionally authenticate

        Args:
            host: qBittorrent host URL (e.g., 'http://localhost:8080')
            username: qBittorrent username
            password: qBittorrent password
            connect_now: If True, authenticate immediately; if False, defer until first API call

        Raises:
            AuthenticationError: If login fails (only when connect_now=True)
            ConnectionError: If cannot reach server (only when connect_now=True)
        """
        self.host = host.rstrip('/')
        self.username = username
        self.password = password
        self._connected = False

        # Initialize qbittorrent-api client (does not connect yet)
        self.client = qbittorrentapi.Client(
            host=self.host,
            username=self.username,
            password=self.password
        )

        if connect_now:
            self._ensure_connected()

    def _ensure_connected(self):
        """
        Ensure we're connected to qBittorrent (lazy initialization support)

        Raises:
            AuthenticationError: If login fails
            ConnectionError: If cannot reach server
        """
        if self._connected:
            return

        try:
            # Verify connection
            self.client.auth_log_in()
            self._connected = True

            logger.info(f"Successfully authenticated with qBittorrent at {self.host}")
            logger.debug(f"qBittorrent version: {self.client.app_version()}")
            logger.debug(f"Web API version: {self.client.app_web_api_version()}")

        except qbittorrentapi.LoginFailed as e:
            raise AuthenticationError(self.host, str(e)) from e
        except qbittorrentapi.APIConnectionError as e:
            raise ConnectionError(self.host, str(e)) from e
        except Exception as e:
            raise ConnectionError(self.host, str(e)) from e

    # Torrent Information Methods

    def get_torrents(self, filter_type: Optional[str] = None, category: Optional[str] = None,
                     tag: Optional[str] = None) -> List[Dict]:
        """
        Get list of torrents

        Args:
            filter_type: Filter by state (e.g., 'downloading', 'seeding', 'completed')
            category: Filter by category
            tag: Filter by tag

        Returns:
            List of torrent dictionaries
        """
        self._ensure_connected()

        # Use qbittorrent-api Client
        torrents = self.client.torrents_info(
            status_filter=filter_type,
            category=category,
            tag=tag
        )

        # Convert to list of dicts (qbittorrent-api returns TorrentInfoList)
        return [dict(t) for t in torrents]

    def get_torrent(self, torrent_hash: str) -> Optional[Dict]:
        """
        Get single torrent by hash

        Args:
            torrent_hash: Torrent hash

        Returns:
            Torrent dictionary or None if not found
        """
        self._ensure_connected()

        # Use qbittorrent-api Client with specific hash
        torrents = self.client.torrents_info(torrent_hashes=torrent_hash)

        # Return first torrent or None if not found
        return dict(torrents[0]) if torrents else None

    def get_properties(self, torrent_hash: str) -> Dict:
        """
        Get detailed torrent properties

        Args:
            torrent_hash: Torrent hash

        Returns:
            Properties dictionary
        """
        props = self.client.torrents_properties(torrent_hash=torrent_hash)
        return dict(props)

    def get_trackers(self, torrent_hash: str) -> List[Dict]:
        """
        Get tracker information for a torrent

        Args:
            torrent_hash: Torrent hash

        Returns:
            List of tracker dictionaries
        """
        trackers = self.client.torrents_trackers(torrent_hash=torrent_hash)
        return [dict(t) for t in trackers]

    def get_files(self, torrent_hash: str) -> List[Dict]:
        """
        Get file list for a torrent

        Args:
            torrent_hash: Torrent hash

        Returns:
            List of file dictionaries
        """
        files = self.client.torrents_files(torrent_hash=torrent_hash)
        return [dict(f) for f in files]

    def get_webseeds(self, torrent_hash: str) -> List[Dict]:
        """
        Get web seeds for a torrent

        Args:
            torrent_hash: Torrent hash

        Returns:
            List of web seed dictionaries
        """
        webseeds = self.client.torrents_webseeds(torrent_hash=torrent_hash)
        return [dict(w) for w in webseeds]

    def get_peers(self, torrent_hash: str) -> List[Dict]:
        """
        Get peer information for a torrent

        Args:
            torrent_hash: Torrent hash

        Returns:
            List of peer dictionaries
        """
        peers_data = self.client.sync_torrent_peers(torrent_hash=torrent_hash)

        # Convert peers dict to list of dicts for consistency
        peers_dict = peers_data.get('peers', {})
        return [{'id': peer_id, **dict(peer_data)} for peer_id, peer_data in peers_dict.items()]

    # Global Information Methods

    def get_transfer_info(self) -> Dict:
        """
        Get global transfer information

        Returns:
            Transfer info dictionary with speeds, data transferred, etc.
        """
        info = self.client.transfer_info()
        return dict(info)

    def get_app_preferences(self) -> Dict:
        """
        Get application preferences

        Returns:
            Preferences dictionary
        """
        prefs = self.client.app_preferences()
        return dict(prefs)

    # Torrent Control Methods

    def stop_torrents(self, hashes: List[str]) -> bool:
        """Stop torrents (pause in qBittorrent v5.0+)"""
        self.client.torrents_pause(torrent_hashes=hashes)
        return True

    def start_torrents(self, hashes: List[str]) -> bool:
        """Start torrents (resume in qBittorrent v5.0+)"""
        self.client.torrents_resume(torrent_hashes=hashes)
        return True

    def force_start_torrents(self, hashes: List[str], enable: bool = True) -> bool:
        """Force start torrents, or release them back to normal queue management if enable=False"""
        self.client.torrents_set_force_start(enable=enable, torrent_hashes=hashes)
        return True

    def recheck_torrents(self, hashes: List[str]) -> bool:
        """Recheck torrents"""
        self.client.torrents_recheck(torrent_hashes=hashes)
        return True

    def reannounce_torrents(self, hashes: List[str]) -> bool:
        """Reannounce torrents to trackers"""
        self.client.torrents_reannounce(torrent_hashes=hashes)
        return True

    def delete_torrents(self, hashes: List[str], delete_files: bool) -> bool:
        """Delete torrents"""
        self.client.torrents_delete(delete_files=delete_files, torrent_hashes=hashes)
        return True

    # Category and Tag Methods

    def set_category(self, hashes: List[str], category: str) -> bool:
        """Set category"""
        self.client.torrents_set_category(category=category, torrent_hashes=hashes)
        return True

    def add_tags(self, hashes: List[str], tags: List[str]) -> bool:
        """Add tags"""
        self.client.torrents_add_tags(tags=tags, torrent_hashes=hashes)
        return True

    def remove_tags(self, hashes: List[str], tags: List[str]) -> bool:
        """Remove tags"""
        self.client.torrents_remove_tags(tags=tags, torrent_hashes=hashes)
        return True

    # Limit Methods

    def set_upload_limit(self, hashes: List[str], limit: int) -> bool:
        """Set upload limit (bytes/s, -1 for unlimited)"""
        self.client.torrents_set_upload_limit(limit=limit, torrent_hashes=hashes)
        return True

    def set_download_limit(self, hashes: List[str], limit: int) -> bool:
        """Set download limit (bytes/s, -1 for unlimited)"""
        self.client.torrents_set_download_limit(limit=limit, torrent_hashes=hashes)
        return True

    def set_share_limits(self, hashes: List[str], ratio_limit: float = -2,
                         seeding_time_limit: int = -2) -> bool:
        """
        Set share limits

        Args:
            hashes: List of torrent hashes
            ratio_limit: Max ratio (-2=global, -1=unlimited, >=0=limit)
            seeding_time_limit: Max seeding time in minutes (-2=global, -1=unlimited, >=0=limit)
        """
        self.client.torrents_set_share_limits(
            ratio_limit=ratio_limit,
            seeding_time_limit=seeding_time_limit,
            torrent_hashes=hashes
        )
        return True

    def set_file_priority(self, torrent_hash: str, file_ids: List[int], priority: int) -> bool:
        """
        Set the download priority of individual files in one torrent

        Args:
            torrent_hash: Torrent hash
            file_ids: File indices, as /torrents/files reports them
            priority: 0 (do not download), 1 (normal), 6 (high), 7 (maximum)
        """
        self.client.torrents_file_priority(
            torrent_hash=torrent_hash, file_ids=file_ids, priority=priority
        )
        return True

    # Priority Methods

    def increase_priority(self, hashes: List[str]) -> bool:
        """Increase torrent priority"""
        self.client.torrents_increase_priority(torrent_hashes=hashes)
        return True

    def decrease_priority(self, hashes: List[str]) -> bool:
        """Decrease torrent priority"""
        self.client.torrents_decrease_priority(torrent_hashes=hashes)
        return True

    def set_top_priority(self, hashes: List[str]) -> bool:
        """Set maximum priority"""
        self.client.torrents_top_priority(torrent_hashes=hashes)
        return True

    def set_bottom_priority(self, hashes: List[str]) -> bool:
        """Set minimum priority"""
        self.client.torrents_bottom_priority(torrent_hashes=hashes)
        return True
