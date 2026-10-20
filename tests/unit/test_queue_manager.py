"""
Comprehensive tests for queue_manager.py

Tests JobStatus class, QueueManager abstract interface, factory function,
and utility methods.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock
import uuid

from qbt_rules.queue_manager import (
    JobStatus,
    QueueManager,
    create_queue
)


# ============================================================================
# JobStatus Class Tests
# ============================================================================

class TestJobStatus:
    """Test JobStatus constants and methods"""

    def test_pending_constant(self):
        """PENDING constant has correct value"""
        assert JobStatus.PENDING == "pending"

    def test_processing_constant(self):
        """PROCESSING constant has correct value"""
        assert JobStatus.PROCESSING == "processing"

    def test_completed_constant(self):
        """COMPLETED constant has correct value"""
        assert JobStatus.COMPLETED == "completed"

    def test_failed_constant(self):
        """FAILED constant has correct value"""
        assert JobStatus.FAILED == "failed"

    def test_cancelled_constant(self):
        """CANCELLED constant has correct value"""
        assert JobStatus.CANCELLED == "cancelled"

    def test_all_method_returns_all_statuses(self):
        """all() returns list of all valid statuses"""
        statuses = JobStatus.all()
        assert isinstance(statuses, list)
        assert len(statuses) == 5
        assert "pending" in statuses
        assert "processing" in statuses
        assert "completed" in statuses
        assert "failed" in statuses
        assert "cancelled" in statuses

    def test_all_method_returns_unique_values(self):
        """all() returns unique values"""
        statuses = JobStatus.all()
        assert len(statuses) == len(set(statuses))

    def test_all_method_no_none_values(self):
        """all() does not contain None values"""
        statuses = JobStatus.all()
        assert None not in statuses

    def test_all_method_all_lowercase(self):
        """all() returns lowercase string values"""
        statuses = JobStatus.all()
        for status in statuses:
            assert isinstance(status, str)
            assert status == status.lower()


# ============================================================================
# QueueManager Static Methods Tests
# ============================================================================

class TestQueueManagerStaticMethods:
    """Test QueueManager static methods"""

    def test_generate_job_id_returns_string(self):
        """generate_job_id() returns string"""
        job_id = QueueManager.generate_job_id()
        assert isinstance(job_id, str)

    def test_generate_job_id_is_valid_uuid(self):
        """generate_job_id() returns valid UUID"""
        job_id = QueueManager.generate_job_id()
        # Should be parseable as UUID
        uuid_obj = uuid.UUID(job_id)
        assert str(uuid_obj) == job_id

    def test_generate_job_id_is_unique(self):
        """generate_job_id() returns unique IDs"""
        ids = [QueueManager.generate_job_id() for _ in range(100)]
        assert len(ids) == len(set(ids)), "Generated IDs should be unique"

    def test_generate_job_id_format(self):
        """generate_job_id() returns UUID v4 format"""
        job_id = QueueManager.generate_job_id()
        # UUID v4 format: xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx
        parts = job_id.split('-')
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12

    def test_validate_status_pending(self):
        """validate_status() returns True for 'pending'"""
        assert QueueManager.validate_status("pending") is True

    def test_validate_status_processing(self):
        """validate_status() returns True for 'processing'"""
        assert QueueManager.validate_status("processing") is True

    def test_validate_status_completed(self):
        """validate_status() returns True for 'completed'"""
        assert QueueManager.validate_status("completed") is True

    def test_validate_status_failed(self):
        """validate_status() returns True for 'failed'"""
        assert QueueManager.validate_status("failed") is True

    def test_validate_status_cancelled(self):
        """validate_status() returns True for 'cancelled'"""
        assert QueueManager.validate_status("cancelled") is True

    def test_validate_status_invalid(self):
        """validate_status() returns False for invalid status"""
        assert QueueManager.validate_status("invalid") is False

    def test_validate_status_empty_string(self):
        """validate_status() returns False for empty string"""
        assert QueueManager.validate_status("") is False

    def test_validate_status_none(self):
        """validate_status() returns False for None"""
        assert QueueManager.validate_status(None) is False

    def test_validate_status_uppercase(self):
        """validate_status() returns False for uppercase"""
        assert QueueManager.validate_status("PENDING") is False

    def test_validate_status_mixed_case(self):
        """validate_status() returns False for mixed case"""
        assert QueueManager.validate_status("Pending") is False


# ============================================================================
# QueueManager create_job_dict Method Tests
# ============================================================================

class TestQueueManagerCreateJobDict:
    """Test QueueManager.create_job_dict() helper method"""

    def test_create_job_dict_minimal_params(self):
        """create_job_dict() with minimal parameters"""
        # Create a concrete subclass for testing
        class ConcreteQueue(QueueManager):
            def enqueue(self, context=None, hash_filter=None): pass
            def dequeue(self): pass
            def get_job(self, job_id): pass
            def list_jobs(self, status=None, context=None, limit=50, offset=0): pass
            def count_jobs(self, status=None): pass
            def update_status(self, job_id, status, started_at=None, completed_at=None, result=None, error=None): pass
            def cancel_job(self, job_id): pass
            def cleanup_old_jobs(self, retention_period): pass
            def get_queue_depth(self): pass
            def get_stats(self): pass
            def health_check(self): pass

        queue = ConcreteQueue()
        job_dict = queue.create_job_dict(
            job_id="test-id",
            context="weekly-cleanup",
            hash_filter=None
        )

        assert job_dict['job_id'] == "test-id"
        assert job_dict['context'] == "weekly-cleanup"
        assert job_dict['hash'] is None
        assert job_dict['status'] == JobStatus.PENDING
        assert job_dict['created_at'] is not None
        assert job_dict['started_at'] is None
        assert job_dict['completed_at'] is None
        assert job_dict['result'] is None
        assert job_dict['error'] is None

    def test_create_job_dict_with_hash_filter(self):
        """create_job_dict() with hash filter"""
        class ConcreteQueue(QueueManager):
            def enqueue(self, context=None, hash_filter=None): pass
            def dequeue(self): pass
            def get_job(self, job_id): pass
            def list_jobs(self, status=None, context=None, limit=50, offset=0): pass
            def count_jobs(self, status=None): pass
            def update_status(self, job_id, status, started_at=None, completed_at=None, result=None, error=None): pass
            def cancel_job(self, job_id): pass
            def cleanup_old_jobs(self, retention_period): pass
            def get_queue_depth(self): pass
            def get_stats(self): pass
            def health_check(self): pass

        queue = ConcreteQueue()
        job_dict = queue.create_job_dict(
            job_id="test-id",
            context=None,
            hash_filter="abc123def456"
        )

        assert job_dict['hash'] == "abc123def456"

    def test_create_job_dict_with_custom_status(self):
        """create_job_dict() with custom status"""
        class ConcreteQueue(QueueManager):
            def enqueue(self, context=None, hash_filter=None): pass
            def dequeue(self): pass
            def get_job(self, job_id): pass
            def list_jobs(self, status=None, context=None, limit=50, offset=0): pass
            def count_jobs(self, status=None): pass
            def update_status(self, job_id, status, started_at=None, completed_at=None, result=None, error=None): pass
            def cancel_job(self, job_id): pass
            def cleanup_old_jobs(self, retention_period): pass
            def get_queue_depth(self): pass
            def get_stats(self): pass
            def health_check(self): pass

        queue = ConcreteQueue()
        job_dict = queue.create_job_dict(
            job_id="test-id",
            context="weekly-cleanup",
            hash_filter=None,
            status=JobStatus.COMPLETED
        )

        assert job_dict['status'] == JobStatus.COMPLETED

    def test_create_job_dict_with_datetime_timestamps(self):
        """create_job_dict() with datetime timestamps"""
        class ConcreteQueue(QueueManager):
            def enqueue(self, context=None, hash_filter=None): pass
            def dequeue(self): pass
            def get_job(self, job_id): pass
            def list_jobs(self, status=None, context=None, limit=50, offset=0): pass
            def count_jobs(self, status=None): pass
            def update_status(self, job_id, status, started_at=None, completed_at=None, result=None, error=None): pass
            def cancel_job(self, job_id): pass
            def cleanup_old_jobs(self, retention_period): pass
            def get_queue_depth(self): pass
            def get_stats(self): pass
            def health_check(self): pass

        queue = ConcreteQueue()
        now = datetime.now(timezone.utc)
        started = now + timedelta(seconds=1)
        completed = now + timedelta(seconds=5)

        job_dict = queue.create_job_dict(
            job_id="test-id",
            context="weekly-cleanup",
            hash_filter=None,
            created_at=now,
            started_at=started,
            completed_at=completed
        )

        # Should convert datetime to ISO format strings
        assert isinstance(job_dict['created_at'], str)
        assert isinstance(job_dict['started_at'], str)
        assert isinstance(job_dict['completed_at'], str)

        # Should be valid ISO format
        datetime.fromisoformat(job_dict['created_at'])
        datetime.fromisoformat(job_dict['started_at'])
        datetime.fromisoformat(job_dict['completed_at'])

    def test_create_job_dict_with_string_timestamps(self):
        """create_job_dict() with string timestamps (passthrough)"""
        class ConcreteQueue(QueueManager):
            def enqueue(self, context=None, hash_filter=None): pass
            def dequeue(self): pass
            def get_job(self, job_id): pass
            def list_jobs(self, status=None, context=None, limit=50, offset=0): pass
            def count_jobs(self, status=None): pass
            def update_status(self, job_id, status, started_at=None, completed_at=None, result=None, error=None): pass
            def cancel_job(self, job_id): pass
            def cleanup_old_jobs(self, retention_period): pass
            def get_queue_depth(self): pass
            def get_stats(self): pass
            def health_check(self): pass

        queue = ConcreteQueue()
        created_str = "2025-01-01T12:00:00"

        job_dict = queue.create_job_dict(
            job_id="test-id",
            context="weekly-cleanup",
            hash_filter=None,
            created_at=created_str
        )

        # Should pass through string timestamps unchanged
        assert job_dict['created_at'] == created_str

    def test_create_job_dict_with_result(self):
        """create_job_dict() with result data"""
        class ConcreteQueue(QueueManager):
            def enqueue(self, context=None, hash_filter=None): pass
            def dequeue(self): pass
            def get_job(self, job_id): pass
            def list_jobs(self, status=None, context=None, limit=50, offset=0): pass
            def count_jobs(self, status=None): pass
            def update_status(self, job_id, status, started_at=None, completed_at=None, result=None, error=None): pass
            def cancel_job(self, job_id): pass
            def cleanup_old_jobs(self, retention_period): pass
            def get_queue_depth(self): pass
            def get_stats(self): pass
            def health_check(self): pass

        queue = ConcreteQueue()
        result_data = {
            'torrents_processed': 10,
            'rules_matched': 5,
            'actions_executed': 3
        }

        job_dict = queue.create_job_dict(
            job_id="test-id",
            context="weekly-cleanup",
            hash_filter=None,
            result=result_data
        )

        assert job_dict['result'] == result_data
        assert job_dict['result']['torrents_processed'] == 10

    def test_create_job_dict_with_error(self):
        """create_job_dict() with error message"""
        class ConcreteQueue(QueueManager):
            def enqueue(self, context=None, hash_filter=None): pass
            def dequeue(self): pass
            def get_job(self, job_id): pass
            def list_jobs(self, status=None, context=None, limit=50, offset=0): pass
            def count_jobs(self, status=None): pass
            def update_status(self, job_id, status, started_at=None, completed_at=None, result=None, error=None): pass
            def cancel_job(self, job_id): pass
            def cleanup_old_jobs(self, retention_period): pass
            def get_queue_depth(self): pass
            def get_stats(self): pass
            def health_check(self): pass

        queue = ConcreteQueue()
        error_msg = "Connection to qBittorrent failed"

        job_dict = queue.create_job_dict(
            job_id="test-id",
            context="weekly-cleanup",
            hash_filter=None,
            status=JobStatus.FAILED,
            error=error_msg
        )

        assert job_dict['error'] == error_msg

    def test_create_job_dict_auto_creates_timestamp(self):
        """create_job_dict() auto-creates created_at if not provided"""
        class ConcreteQueue(QueueManager):
            def enqueue(self, context=None, hash_filter=None): pass
            def dequeue(self): pass
            def get_job(self, job_id): pass
            def list_jobs(self, status=None, context=None, limit=50, offset=0): pass
            def count_jobs(self, status=None): pass
            def update_status(self, job_id, status, started_at=None, completed_at=None, result=None, error=None): pass
            def cancel_job(self, job_id): pass
            def cleanup_old_jobs(self, retention_period): pass
            def get_queue_depth(self): pass
            def get_stats(self): pass
            def health_check(self): pass

        queue = ConcreteQueue()
        before = datetime.now(timezone.utc)
        job_dict = queue.create_job_dict(
            job_id="test-id",
            context="weekly-cleanup",
            hash_filter=None
        )
        after = datetime.now(timezone.utc)

        # created_at should be set and be a recent timestamp
        assert job_dict['created_at'] is not None
        created_dt = datetime.fromisoformat(job_dict['created_at'])
        assert before <= created_dt <= after


# ============================================================================
# create_queue Factory Function Tests
# ============================================================================

class TestCreateQueueFactory:
    """Test create_queue() factory function"""

    def test_create_queue_sqlite_default_path(self, tmp_path):
        """create_queue() creates SQLite queue with custom path"""
        db_path = str(tmp_path / "test.db")
        queue = create_queue('sqlite', db_path=db_path)

        # Should return SQLiteQueue instance
        from qbt_rules.queue_backends.sqlite_queue import SQLiteQueue
        assert isinstance(queue, SQLiteQueue)
        assert isinstance(queue, QueueManager)
        # db_path might be a Path object or string
        assert str(queue.db_path) == db_path

    def test_create_queue_sqlite_custom_path(self, tmp_path):
        """create_queue() creates SQLite queue with custom path"""
        db_path = str(tmp_path / "custom.db")
        queue = create_queue('sqlite', db_path=db_path)

        from qbt_rules.queue_backends.sqlite_queue import SQLiteQueue
        assert isinstance(queue, SQLiteQueue)
        assert str(queue.db_path) == db_path

    @pytest.mark.redis
    def test_create_queue_redis_default_url(self, redis_available):
        """create_queue() creates Redis queue with default URL"""
        if not redis_available:
            pytest.skip("Redis not available")

        queue = create_queue('redis')

        from qbt_rules.queue_backends.redis_queue import RedisQueue
        assert isinstance(queue, RedisQueue)
        assert isinstance(queue, QueueManager)

    @pytest.mark.redis
    def test_create_queue_redis_custom_url(self, redis_available):
        """create_queue() creates Redis queue with custom URL"""
        if not redis_available:
            pytest.skip("Redis not available")

        queue = create_queue('redis', redis_url='redis://localhost:6379/15')

        from qbt_rules.queue_backends.redis_queue import RedisQueue
        assert isinstance(queue, RedisQueue)

    def test_create_queue_invalid_backend(self):
        """create_queue() raises ValueError for invalid backend"""
        with pytest.raises(ValueError) as exc_info:
            create_queue('invalid')

        assert "Unknown queue backend" in str(exc_info.value)
        assert "invalid" in str(exc_info.value)

    def test_create_queue_redis_import_error_when_package_missing(self):
        """create_queue() raises ValueError when redis package not installed"""
        import sys

        # Save every module this test removes, not just top-level 'redis':
        # dropping redis.* submodules (and fakeredis) without restoring them
        # makes later imports create duplicate classes, e.g. a second
        # redis.driver_info.DriverInfo that redis>=8.1's isinstance check
        # rejects, breaking every Redis test that runs after this one.
        saved_modules = {
            key: mod for key, mod in sys.modules.items()
            if ('redis' in key and 'qbt_rules' not in key and 'tests' not in key)
            or ('redis_queue' in key and 'tests' not in key)
        }

        try:
            # Remove modules from cache to force reimport
            for key in saved_modules:
                del sys.modules[key]

            # Block redis import
            sys.modules['redis'] = None

            # Attempt to create Redis queue should raise ValueError
            with pytest.raises(ValueError) as exc_info:
                create_queue('redis')

            # Verify error message
            error_msg = str(exc_info.value)
            assert "Redis backend requires 'redis' package" in error_msg
            assert "pip install qbt-rules[redis]" in error_msg

        finally:
            # Restore original state: drop anything the failed import left
            # behind (including the None sentinel), then put back the saved
            # module objects
            for key in list(sys.modules.keys()):
                if ('redis' in key and 'qbt_rules' not in key and 'tests' not in key) \
                        or ('redis_queue' in key and 'tests' not in key):
                    del sys.modules[key]
            sys.modules.update(saved_modules)

    def test_create_queue_redis_success_with_mock(self):
        """create_queue() successfully creates RedisQueue with correct parameters"""
        try:
            # Try to import RedisQueue to see if we can test this
            from qbt_rules.queue_backends.redis_queue import RedisQueue
        except ImportError:
            pytest.skip("Redis package not available")

        # Mock RedisQueue at its source location (it's imported inside create_queue)
        with patch('qbt_rules.queue_backends.redis_queue.RedisQueue') as MockRedisQueue:
            mock_queue = Mock(spec=RedisQueue)
            MockRedisQueue.return_value = mock_queue

            # Create Redis queue with custom URL (lines 285-286)
            queue = create_queue('redis', redis_url='redis://localhost:6379/5')

            # Should have called RedisQueue constructor with correct URL
            MockRedisQueue.assert_called_once_with(redis_url='redis://localhost:6379/5')

            # Should return the mock queue instance
            assert queue is mock_queue

    def test_create_queue_case_sensitive(self):
        """create_queue() is case-sensitive for backend names"""
        with pytest.raises(ValueError):
            create_queue('SQLite')  # Should be lowercase 'sqlite'

        with pytest.raises(ValueError):
            create_queue('REDIS')  # Should be lowercase 'redis'

    def test_create_queue_empty_string(self):
        """create_queue() raises ValueError for empty string"""
        with pytest.raises(ValueError):
            create_queue('')

    def test_create_queue_none_backend(self):
        """create_queue() raises ValueError for None"""
        with pytest.raises(ValueError):
            create_queue(None)
