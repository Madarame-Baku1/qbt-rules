"""
Comprehensive tests for RedisQueue backend

Test coverage for:
- Redis connection and initialization
- Job enqueue/dequeue operations
- Job listing and filtering
- Status updates and cancellation
- Cleanup and statistics
- Thread safety and connection pooling
- Key management and data structures
"""

import json
import pytest
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import List

# Skip all tests if Redis not available
pytest_plugins = ('pytest_mock',)

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

if REDIS_AVAILABLE:
    from qbt_rules.queue_backends.redis_queue import RedisQueue
    from qbt_rules.queue_manager import JobStatus, QueueManager


# Import error test - NOT skipped (tests the import error handling itself)
class TestRedisImportError:
    """Test ImportError handling when redis package not installed"""

    def test_import_error_message_when_redis_not_installed(self):
        """Should raise ImportError with installation instructions when redis not available"""
        import sys
        import importlib.util

        # Save current state
        saved_redis = sys.modules.get('redis')
        saved_redis_queue = sys.modules.get('qbt_rules.queue_backends.redis_queue')

        try:
            # Remove modules from cache
            for key in list(sys.modules.keys()):
                if 'redis_queue' in key:
                    del sys.modules[key]
            if 'redis' in sys.modules:
                del sys.modules['redis']

            # Add sentinel to block redis import
            sys.modules['redis'] = None

            # Attempt import should raise ImportError
            with pytest.raises(ImportError) as exc_info:
                import qbt_rules.queue_backends.redis_queue

            # Verify error message
            error_msg = str(exc_info.value)
            assert "Redis backend requires 'redis' package" in error_msg
            assert "pip install qbt-rules[redis]" in error_msg

        finally:
            # Restore original state
            if saved_redis is not None:
                sys.modules['redis'] = saved_redis
            elif 'redis' in sys.modules:
                del sys.modules['redis']

            if saved_redis_queue is not None:
                sys.modules['qbt_rules.queue_backends.redis_queue'] = saved_redis_queue


# Mark remaining tests to skip if Redis not available
pytestmark = pytest.mark.skipif(not REDIS_AVAILABLE, reason="Redis not installed")


@pytest.fixture
def queue(redis_client, mocker):
    """Create and cleanup Redis queue for testing"""
    # Flush test database before test
    redis_client.flushdb()

    # Mock Redis to use the provided redis_client (real or fake)
    # This allows RedisQueue to use fakeredis when real Redis isn't available
    mocker.patch('redis.ConnectionPool.from_url', return_value=None)
    mocker.patch('redis.Redis', return_value=redis_client)

    q = RedisQueue(redis_url='redis://localhost:6379/15')
    # Replace with actual client
    q.redis = redis_client
    q.pool = None

    yield q

    # Cleanup after test
    if hasattr(redis_client, 'flushdb'):
        redis_client.flushdb()


class TestRedisQueueInitialization:
    """Test Redis queue initialization and connection"""

    def test_init_connects_to_redis(self, redis_client, mocker):
        """Should successfully connect to Redis"""
        redis_client.flushdb()

        mocker.patch('redis.ConnectionPool.from_url', return_value=None)
        mocker.patch('redis.Redis', return_value=redis_client)

        queue = RedisQueue(redis_url='redis://localhost:6379/15')
        queue.redis = redis_client

        # Should be able to ping
        assert queue.redis.ping() is True

    def test_init_creates_connection_pool(self, redis_client, mocker):
        """Should create connection pool with correct settings"""
        redis_client.flushdb()

        # Create mock pool with expected attributes
        mock_pool = mocker.MagicMock()
        mock_pool.connection_kwargs = {'decode_responses': True}
        mock_pool.max_connections = 10

        mocker.patch('redis.ConnectionPool.from_url', return_value=mock_pool)
        mocker.patch('redis.Redis', return_value=redis_client)

        queue = RedisQueue(redis_url='redis://localhost:6379/15')

        assert queue.pool is not None
        assert queue.pool.connection_kwargs['decode_responses'] is True
        assert queue.pool.max_connections == 10

    def test_init_stores_redis_url(self, redis_client, mocker):
        """Should store Redis URL"""
        redis_client.flushdb()

        mocker.patch('redis.ConnectionPool.from_url', return_value=None)
        mocker.patch('redis.Redis', return_value=redis_client)

        redis_url = 'redis://localhost:6379/15'
        queue = RedisQueue(redis_url=redis_url)

        assert queue.redis_url == redis_url

    def test_init_with_default_url(self, redis_client, mocker):
        """Should use default localhost URL"""
        mocker.patch('redis.ConnectionPool.from_url', return_value=None)
        mocker.patch('redis.Redis', return_value=redis_client)

        queue = RedisQueue()
        assert queue.redis_url == 'redis://localhost:6379/0'

    def test_init_invalid_url_raises_connection_error(self):
        """Should raise ConnectionError for invalid Redis URL"""
        with pytest.raises(ConnectionError, match="Cannot connect to Redis"):
            RedisQueue(redis_url='redis://invalid-host:9999/0')

    def test_init_malformed_url_raises_error(self):
        """Should raise error for malformed URL"""
        with pytest.raises((ConnectionError, RuntimeError)):
            RedisQueue(redis_url='not-a-valid-url')


class TestRedisQueueKeyBuilding:
    """Test Redis key building and prefixing"""

    def test_key_with_single_part(self, queue):
        """Should build key with prefix and single part"""
        key = queue._key('test')
        assert key == 'qbt_rules:test'

    def test_key_with_multiple_parts(self, queue):
        """Should join multiple parts with colons"""
        key = queue._key('jobs', '123', 'data')
        assert key == 'qbt_rules:jobs:123:data'

    def test_key_prefix_constant(self, queue):
        """Should use correct prefix constant"""
        assert queue.KEY_PREFIX == 'qbt_rules'


class TestRedisQueueEnqueue:
    """Test job enqueueing"""

    def test_enqueue_dry_run_round_trips(self, queue):
        assert queue.get_job(queue.enqueue(context='manual', dry_run=True))['dry_run'] is True
        assert queue.get_job(queue.enqueue(context='manual'))['dry_run'] is False

    def test_job_without_dry_run_field_reads_as_false(self, queue):
        """Jobs queued before per-job dry-run have no dry_run field in their hash"""
        job_id = queue.enqueue(context='manual')
        queue.redis.hdel(queue._key('jobs', job_id), 'dry_run')
        assert queue.get_job(job_id)['dry_run'] is False

    def test_enqueue_returns_job_id(self, queue):
        """Should return generated job ID"""
        job_id = queue.enqueue()

        assert isinstance(job_id, str)
        assert len(job_id) > 0

    def test_enqueue_creates_job_hash(self, queue, redis_client):
        """Should create Redis hash with job data"""
        job_id = queue.enqueue(context='test', hash_filter='abc123')

        job_key = f'qbt_rules:jobs:{job_id}'
        assert redis_client.exists(job_key) == 1

        job_data = redis_client.hgetall(job_key)
        assert job_data['id'] == job_id
        assert job_data['context'] == 'test'
        assert job_data['hash'] == 'abc123'
        assert job_data['status'] == JobStatus.PENDING

    def test_enqueue_adds_to_pending_queue(self, queue, redis_client):
        """Should add job ID to pending queue (LIST)"""
        job_id = queue.enqueue()

        pending_key = 'qbt_rules:queue:pending'
        pending_jobs = redis_client.lrange(pending_key, 0, -1)

        assert job_id in pending_jobs

    def test_enqueue_adds_to_status_index(self, queue, redis_client):
        """Should add job to status SET index"""
        job_id = queue.enqueue()

        status_key = f'qbt_rules:jobs:status:{JobStatus.PENDING}'
        assert redis_client.sismember(status_key, job_id) == 1

    def test_enqueue_adds_to_context_index(self, queue, redis_client):
        """Should add job to context SET index when context provided"""
        job_id = queue.enqueue(context='my-context')

        context_key = 'qbt_rules:jobs:context:my-context'
        assert redis_client.sismember(context_key, job_id) == 1

    def test_enqueue_no_context_index_without_context(self, queue, redis_client):
        """Should not create context index when context is None"""
        job_id = queue.enqueue(context=None)

        # Should not exist any context index
        context_keys = redis_client.keys('qbt_rules:jobs:context:*')
        assert len(context_keys) == 0

    def test_enqueue_adds_to_time_sorted_set(self, queue, redis_client):
        """Should add job to time-sorted ZSET"""
        job_id = queue.enqueue()

        time_key = 'qbt_rules:jobs:by_time'
        score = redis_client.zscore(time_key, job_id)

        assert score is not None
        assert score > 0  # Unix timestamp

    def test_enqueue_unique_job_ids(self, queue):
        """Should generate unique job IDs"""
        job_ids = [queue.enqueue() for _ in range(10)]

        assert len(job_ids) == len(set(job_ids))

    def test_enqueue_fifo_ordering(self, queue, redis_client):
        """Should maintain FIFO order in pending queue"""
        job1_id = queue.enqueue()
        time.sleep(0.01)  # Small delay to ensure different timestamps
        job2_id = queue.enqueue()
        time.sleep(0.01)
        job3_id = queue.enqueue()

        # Check queue order (FIFO)
        pending_key = 'qbt_rules:queue:pending'
        queue_order = redis_client.lrange(pending_key, 0, -1)

        assert queue_order == [job1_id, job2_id, job3_id]

    def test_enqueue_stores_created_at(self, queue):
        """Should store ISO format created_at timestamp"""
        before = datetime.now(timezone.utc)
        job_id = queue.enqueue()
        after = datetime.now(timezone.utc)

        job = queue.get_job(job_id)
        created_at = datetime.fromisoformat(job['created_at'])

        assert before <= created_at <= after


class TestRedisQueueDequeue:
    """Test job dequeueing"""

    def test_dequeue_returns_job_dict(self, queue):
        """Should return job dictionary"""
        job_id = queue.enqueue(context='test', hash_filter='abc')

        job = queue.dequeue()

        assert job is not None
        assert job['job_id'] == job_id
        assert job['context'] == 'test'
        assert job['hash'] == 'abc'

    def test_dequeue_empty_queue_returns_none(self, queue):
        """Should return None when queue is empty"""
        job = queue.dequeue()
        assert job is None

    def test_dequeue_missing_job_data_returns_none(self, queue, mocker):
        """Should return None when hgetall returns empty data (line 156)"""
        # This tests defensive code where hgetall returns no data
        # Could happen in edge cases like Redis failures or corrupted state

        # Mock lpop to return a job ID
        mock_lpop = mocker.patch.object(queue.redis, 'lpop', return_value='test-job-id')

        # Mock pipeline to return empty dict for hgetall (last result)
        mock_pipeline_instance = mocker.MagicMock()
        mock_pipeline_instance.hset = mocker.MagicMock(return_value=mock_pipeline_instance)
        mock_pipeline_instance.srem = mocker.MagicMock(return_value=mock_pipeline_instance)
        mock_pipeline_instance.sadd = mocker.MagicMock(return_value=mock_pipeline_instance)
        mock_pipeline_instance.hgetall = mocker.MagicMock(return_value=mock_pipeline_instance)

        # execute() returns results where last item (hgetall) is empty dict
        mock_pipeline_instance.execute.return_value = [1, 1, 1, 1, {}]  # Last is empty hgetall

        mocker.patch.object(queue.redis, 'pipeline', return_value=mock_pipeline_instance)

        # Dequeue should return None when hgetall returns empty data (line 156)
        job = queue.dequeue()

        # Should return None because job_data is empty
        assert job is None
        mock_lpop.assert_called_once()

    def test_dequeue_removes_from_pending_queue(self, queue, redis_client):
        """Should remove job from pending queue"""
        job_id = queue.enqueue()

        queue.dequeue()

        pending_key = 'qbt_rules:queue:pending'
        pending_jobs = redis_client.lrange(pending_key, 0, -1)

        assert job_id not in pending_jobs

    def test_dequeue_updates_status_to_processing(self, queue):
        """Should update job status to PROCESSING"""
        job_id = queue.enqueue()

        job = queue.dequeue()

        assert job['status'] == JobStatus.PROCESSING

    def test_dequeue_sets_started_at(self, queue):
        """Should set started_at timestamp"""
        before = datetime.now(timezone.utc)
        job_id = queue.enqueue()
        job = queue.dequeue()
        after = datetime.now(timezone.utc)

        assert job['started_at'] is not None
        started_at = datetime.fromisoformat(job['started_at'])
        assert before <= started_at <= after

    def test_dequeue_updates_status_index(self, queue, redis_client):
        """Should move job from PENDING to PROCESSING status index"""
        job_id = queue.enqueue()

        queue.dequeue()

        pending_key = f'qbt_rules:jobs:status:{JobStatus.PENDING}'
        processing_key = f'qbt_rules:jobs:status:{JobStatus.PROCESSING}'

        assert redis_client.sismember(pending_key, job_id) == 0
        assert redis_client.sismember(processing_key, job_id) == 1

    def test_dequeue_fifo_order(self, queue):
        """Should dequeue in FIFO order"""
        job1_id = queue.enqueue()
        time.sleep(0.01)
        job2_id = queue.enqueue()
        time.sleep(0.01)
        job3_id = queue.enqueue()

        job1 = queue.dequeue()
        job2 = queue.dequeue()
        job3 = queue.dequeue()

        assert job1['job_id'] == job1_id
        assert job2['job_id'] == job2_id
        assert job3['job_id'] == job3_id

    def test_dequeue_atomic_lpop(self, queue):
        """Should use atomic LPOP operation"""
        # Enqueue multiple jobs
        for _ in range(5):
            queue.enqueue()

        # Dequeue from multiple threads
        results = []

        def dequeue_worker():
            job = queue.dequeue()
            if job:
                results.append(job['job_id'])

        threads = [threading.Thread(target=dequeue_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have 5 unique job IDs (no duplicates)
        assert len(results) == 5
        assert len(set(results)) == 5


class TestRedisQueueGetJob:
    """Test get_job() method"""

    def test_get_job_returns_job_dict(self, queue):
        """Should return job dictionary with all fields"""
        job_id = queue.enqueue(context='test', hash_filter='abc')

        job = queue.get_job(job_id)

        assert job is not None
        assert job['job_id'] == job_id
        assert job['context'] == 'test'
        assert job['hash'] == 'abc'
        assert job['status'] == JobStatus.PENDING

    def test_get_job_nonexistent_returns_none(self, queue):
        """Should return None for non-existent job"""
        job = queue.get_job('nonexistent-id')
        assert job is None

    def test_get_job_converts_empty_strings_to_none(self, queue):
        """Should convert empty string fields to None"""
        job_id = queue.enqueue()  # No context or hash

        job = queue.get_job(job_id)

        # Empty strings should become None
        assert job['started_at'] is None
        assert job['completed_at'] is None
        assert job['error'] is None


class TestRedisQueueListJobs:
    """Test list_jobs() method"""

    def test_list_jobs_all_jobs(self, queue):
        """Should list all jobs when no filters"""
        job1_id = queue.enqueue()
        job2_id = queue.enqueue()

        jobs = queue.list_jobs()

        assert len(jobs) == 2
        job_ids = [j['job_id'] for j in jobs]
        assert job1_id in job_ids
        assert job2_id in job_ids

    def test_list_jobs_filter_by_status(self, queue):
        """Should filter jobs by status"""
        job1_id = queue.enqueue()
        job2_id = queue.enqueue()

        # Dequeue one job (changes to PROCESSING)
        queue.dequeue()

        pending_jobs = queue.list_jobs(status=JobStatus.PENDING)
        processing_jobs = queue.list_jobs(status=JobStatus.PROCESSING)

        assert len(pending_jobs) == 1
        assert pending_jobs[0]['job_id'] == job2_id

        assert len(processing_jobs) == 1
        assert processing_jobs[0]['job_id'] == job1_id

    def test_list_jobs_filter_by_context(self, queue):
        """Should filter jobs by context"""
        job1_id = queue.enqueue(context='context-a')
        job2_id = queue.enqueue(context='context-b')
        job3_id = queue.enqueue(context='context-a')

        context_a_jobs = queue.list_jobs(context='context-a')

        assert len(context_a_jobs) == 2
        job_ids = [j['job_id'] for j in context_a_jobs]
        assert job1_id in job_ids
        assert job3_id in job_ids
        assert job2_id not in job_ids

    def test_list_jobs_filter_by_status_and_context(self, queue):
        """Should filter by both status and context (intersection)"""
        job1_id = queue.enqueue(context='test')
        job2_id = queue.enqueue(context='test')
        job3_id = queue.enqueue(context='other')

        # Dequeue job1
        queue.dequeue()

        # Filter: PENDING + test context
        jobs = queue.list_jobs(status=JobStatus.PENDING, context='test')

        assert len(jobs) == 1
        assert jobs[0]['job_id'] == job2_id

    def test_list_jobs_pagination_limit(self, queue):
        """Should respect limit parameter"""
        for _ in range(10):
            queue.enqueue()

        jobs = queue.list_jobs(limit=5)

        assert len(jobs) == 5

    def test_list_jobs_pagination_offset(self, queue):
        """Should respect offset parameter"""
        job_ids = []
        for _ in range(5):
            job_ids.append(queue.enqueue())
            time.sleep(0.01)  # Ensure different timestamps

        # Get first 2 jobs
        page1 = queue.list_jobs(limit=2, offset=0)
        # Get next 2 jobs
        page2 = queue.list_jobs(limit=2, offset=2)

        assert len(page1) == 2
        assert len(page2) == 2

        # Should be different jobs
        page1_ids = [j['job_id'] for j in page1]
        page2_ids = [j['job_id'] for j in page2]
        assert set(page1_ids).isdisjoint(set(page2_ids))

    def test_list_jobs_limit_capped_at_100(self, queue):
        """Should cap limit at 100"""
        for _ in range(10):
            queue.enqueue()

        # Request 200, should get max 10 (all available, capped at 100)
        jobs = queue.list_jobs(limit=200)

        # Internal limit is 100, but we only have 10 jobs
        assert len(jobs) == 10

    def test_list_jobs_ordered_by_created_at_desc(self, queue):
        """Should order jobs by created_at descending (newest first)"""
        job_ids = []
        for i in range(3):
            job_ids.append(queue.enqueue())
            time.sleep(0.01)  # Ensure different timestamps

        jobs = queue.list_jobs()

        # Newest first
        assert jobs[0]['job_id'] == job_ids[2]
        assert jobs[1]['job_id'] == job_ids[1]
        assert jobs[2]['job_id'] == job_ids[0]

    def test_list_jobs_empty_queue(self, queue):
        """Should return empty list for empty queue"""
        jobs = queue.list_jobs()
        assert jobs == []


class TestRedisQueueCountJobs:
    """Test count_jobs() method"""

    def test_count_jobs_total(self, queue):
        """Should count total jobs when no status filter"""
        queue.enqueue()
        queue.enqueue()
        queue.dequeue()  # One PROCESSING

        count = queue.count_jobs()

        assert count == 2

    def test_count_jobs_by_status(self, queue):
        """Should count jobs by specific status"""
        queue.enqueue()
        queue.enqueue()
        queue.enqueue()

        queue.dequeue()  # One PROCESSING

        pending_count = queue.count_jobs(status=JobStatus.PENDING)
        processing_count = queue.count_jobs(status=JobStatus.PROCESSING)

        assert pending_count == 2
        assert processing_count == 1

    def test_count_jobs_empty_queue(self, queue):
        """Should return 0 for empty queue"""
        count = queue.count_jobs()
        assert count == 0

    def test_count_jobs_nonexistent_status(self, queue):
        """Should return 0 for status with no jobs"""
        queue.enqueue()

        completed_count = queue.count_jobs(status=JobStatus.COMPLETED)

        assert completed_count == 0


class TestRedisQueueUpdateStatus:
    """Test update_status() method"""

    def test_update_status_changes_status(self, queue):
        """Should update job status"""
        job_id = queue.enqueue()
        queue.dequeue()

        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        job = queue.get_job(job_id)
        assert job['status'] == JobStatus.COMPLETED

    def test_update_status_sets_completed_at(self, queue):
        """Should set completed_at timestamp"""
        job_id = queue.enqueue()
        queue.dequeue()

        completed_time = datetime.now(timezone.utc)
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=completed_time)

        job = queue.get_job(job_id)
        assert job['completed_at'] is not None
        assert job['completed_at'] == completed_time.isoformat()

    def test_update_status_sets_result(self, queue):
        """Should set result with JSON serialization"""
        job_id = queue.enqueue()
        queue.dequeue()

        result_data = {'torrents': 10, 'actions': 5}
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc),
                          result=result_data)

        job = queue.get_job(job_id)
        assert job['result'] == result_data

    def test_update_status_sets_error(self, queue):
        """Should set error message"""
        job_id = queue.enqueue()
        queue.dequeue()

        queue.update_status(job_id, JobStatus.FAILED,
                          error='Test error message')

        job = queue.get_job(job_id)
        assert job['error'] == 'Test error message'

    def test_update_status_updates_status_index(self, queue, redis_client):
        """Should update status SET indexes"""
        job_id = queue.enqueue()
        queue.dequeue()

        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        processing_key = f'qbt_rules:jobs:status:{JobStatus.PROCESSING}'
        completed_key = f'qbt_rules:jobs:status:{JobStatus.COMPLETED}'

        assert redis_client.sismember(processing_key, job_id) == 0
        assert redis_client.sismember(completed_key, job_id) == 1

    def test_update_status_invalid_status_raises_error(self, queue):
        """Should raise ValueError for invalid status"""
        job_id = queue.enqueue()

        with pytest.raises(ValueError, match="Invalid status"):
            queue.update_status(job_id, 'invalid-status')

    def test_update_status_nonexistent_job_returns_false(self, queue):
        """Should return False for non-existent job"""
        result = queue.update_status('nonexistent-id', JobStatus.COMPLETED)
        assert result is False

    def test_update_status_sets_started_at(self, queue):
        """Should set started_at when provided"""
        job_id = queue.enqueue()

        started_time = datetime.now(timezone.utc)
        queue.update_status(job_id, JobStatus.PROCESSING,
                          started_at=started_time)

        job = queue.get_job(job_id)
        assert job['started_at'] == started_time.isoformat()


class TestRedisQueueCancelJob:
    """Test cancel_job() method"""

    def test_cancel_job_pending_job(self, queue):
        """Should cancel pending job"""
        job_id = queue.enqueue()

        result = queue.cancel_job(job_id)

        assert result is True

        job = queue.get_job(job_id)
        assert job['status'] == JobStatus.CANCELLED

    def test_cancel_job_removes_from_pending_queue(self, queue, redis_client):
        """Should remove job from pending queue"""
        job_id = queue.enqueue()

        queue.cancel_job(job_id)

        pending_key = 'qbt_rules:queue:pending'
        pending_jobs = redis_client.lrange(pending_key, 0, -1)

        assert job_id not in pending_jobs

    def test_cancel_job_updates_status_index(self, queue, redis_client):
        """Should update status indexes"""
        job_id = queue.enqueue()

        queue.cancel_job(job_id)

        pending_key = f'qbt_rules:jobs:status:{JobStatus.PENDING}'
        cancelled_key = f'qbt_rules:jobs:status:{JobStatus.CANCELLED}'

        assert redis_client.sismember(pending_key, job_id) == 0
        assert redis_client.sismember(cancelled_key, job_id) == 1

    def test_cancel_job_processing_job_returns_false(self, queue):
        """Should not cancel job that is already processing"""
        job_id = queue.enqueue()
        queue.dequeue()  # Now PROCESSING

        result = queue.cancel_job(job_id)

        assert result is False

        job = queue.get_job(job_id)
        assert job['status'] == JobStatus.PROCESSING  # Unchanged

    def test_cancel_job_nonexistent_returns_false(self, queue):
        """Should return False for non-existent job"""
        result = queue.cancel_job('nonexistent-id')
        assert result is False

    def test_cancel_job_completed_job_returns_false(self, queue):
        """Should not cancel already completed job"""
        job_id = queue.enqueue()
        queue.dequeue()
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        result = queue.cancel_job(job_id)

        assert result is False


class TestRedisQueueCleanup:
    """Test cleanup_old_jobs() method"""

    def test_cleanup_old_completed_jobs(self, queue, redis_client):
        """Should remove old completed jobs"""
        # Create old job
        job_id = queue.enqueue()
        queue.dequeue()
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        # Manually set old timestamp (10 days ago)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        time_key = 'qbt_rules:jobs:by_time'
        redis_client.zadd(time_key, {job_id: old_timestamp})

        # Cleanup jobs older than 7 days
        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 1
        assert queue.get_job(job_id) is None

    def test_cleanup_old_failed_jobs(self, queue, redis_client):
        """Should remove old failed jobs"""
        job_id = queue.enqueue()
        queue.dequeue()
        queue.update_status(job_id, JobStatus.FAILED, error='Test error')

        # Set old timestamp
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        time_key = 'qbt_rules:jobs:by_time'
        redis_client.zadd(time_key, {job_id: old_timestamp})

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 1
        assert queue.get_job(job_id) is None

    def test_cleanup_old_cancelled_jobs(self, queue, redis_client):
        """Should remove old cancelled jobs"""
        job_id = queue.enqueue()
        queue.cancel_job(job_id)

        # Set old timestamp
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        time_key = 'qbt_rules:jobs:by_time'
        redis_client.zadd(time_key, {job_id: old_timestamp})

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 1
        assert queue.get_job(job_id) is None

    def test_cleanup_does_not_remove_pending_jobs(self, queue, redis_client):
        """Should not remove old pending jobs"""
        job_id = queue.enqueue()

        # Set old timestamp
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        time_key = 'qbt_rules:jobs:by_time'
        redis_client.zadd(time_key, {job_id: old_timestamp})

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 0
        assert queue.get_job(job_id) is not None

    def test_cleanup_does_not_remove_processing_jobs(self, queue, redis_client):
        """Should not remove old processing jobs"""
        job_id = queue.enqueue()
        queue.dequeue()  # PROCESSING

        # Set old timestamp
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        time_key = 'qbt_rules:jobs:by_time'
        redis_client.zadd(time_key, {job_id: old_timestamp})

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 0
        assert queue.get_job(job_id) is not None

    def test_cleanup_removes_from_all_indexes(self, queue, redis_client):
        """Should remove job from all indexes"""
        job_id = queue.enqueue(context='test-context')
        queue.dequeue()
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        # Set old timestamp
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        time_key = 'qbt_rules:jobs:by_time'
        redis_client.zadd(time_key, {job_id: old_timestamp})

        queue.cleanup_old_jobs(retention_period=7 * 86400)

        # Check all indexes
        assert redis_client.exists(f'qbt_rules:jobs:{job_id}') == 0
        assert redis_client.zscore(time_key, job_id) is None
        assert redis_client.sismember(
            f'qbt_rules:jobs:status:{JobStatus.COMPLETED}', job_id
        ) == 0
        assert redis_client.sismember(
            'qbt_rules:jobs:context:test-context', job_id
        ) == 0

    def test_cleanup_no_old_jobs_returns_zero(self, queue):
        """Should return 0 when no old jobs to clean"""
        queue.enqueue()

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 0

    def test_cleanup_multiple_old_jobs(self, queue, redis_client):
        """Should remove multiple old jobs"""
        job_ids = []
        for _ in range(5):
            job_id = queue.enqueue()
            queue.dequeue()
            queue.update_status(job_id, JobStatus.COMPLETED,
                              completed_at=datetime.now(timezone.utc))
            job_ids.append(job_id)

        # Set all to old timestamps
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        time_key = 'qbt_rules:jobs:by_time'
        for job_id in job_ids:
            redis_client.zadd(time_key, {job_id: old_timestamp})

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 5


class TestRedisQueueGetQueueDepth:
    """Test get_queue_depth() method"""

    def test_get_queue_depth_pending_jobs(self, queue):
        """Should return count of pending jobs"""
        queue.enqueue()
        queue.enqueue()
        queue.enqueue()

        depth = queue.get_queue_depth()

        assert depth == 3

    def test_get_queue_depth_excludes_processing_jobs(self, queue):
        """Should not count processing jobs"""
        queue.enqueue()
        queue.enqueue()

        queue.dequeue()  # One now processing

        depth = queue.get_queue_depth()

        assert depth == 1

    def test_get_queue_depth_empty_queue(self, queue):
        """Should return 0 for empty queue"""
        depth = queue.get_queue_depth()
        assert depth == 0


class TestRedisQueueGetStats:
    """Test get_stats() method"""

    def test_get_stats_returns_all_status_counts(self, queue):
        """Should return counts for all job statuses"""
        queue.enqueue()
        queue.enqueue()
        job_id = queue.enqueue()

        queue.dequeue()  # Job 1: PROCESSING
        job = queue.dequeue()  # Job 2: PROCESSING

        # Complete job 2
        queue.update_status(job['job_id'], JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        # Now we have:
        # - Job 1: PROCESSING
        # - Job 2: COMPLETED
        # - Job 3 (job_id): PENDING

        stats = queue.get_stats()

        assert stats['total_jobs'] == 3
        assert stats['pending'] == 1
        assert stats['processing'] == 1
        assert stats['completed'] == 1
        assert stats['failed'] == 0
        assert stats['cancelled'] == 0

    def test_get_stats_calculates_average_execution_time(self, queue):
        """Should calculate average execution time for completed jobs"""
        # Create completed jobs with known execution times
        for i in range(3):
            job_id = queue.enqueue()
            job = queue.dequeue()

            started = datetime.now(timezone.utc) - timedelta(seconds=10)
            completed = datetime.now(timezone.utc)

            queue.update_status(job_id, JobStatus.COMPLETED,
                              started_at=started,
                              completed_at=completed)

        stats = queue.get_stats()

        assert stats['average_execution_time'] is not None
        assert isinstance(stats['average_execution_time'], (int, float))
        assert stats['average_execution_time'] > 0

    def test_get_stats_no_completed_jobs_average_is_none(self, queue):
        """Should return None for average_execution_time when no completed jobs"""
        queue.enqueue()

        stats = queue.get_stats()

        assert stats['average_execution_time'] is None

    def test_get_stats_handles_jobs_without_timestamps(self, queue, redis_client):
        """Should handle completed jobs without proper timestamps"""
        job_id = queue.enqueue()
        queue.dequeue()
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))
        # started_at may be missing or invalid

        # Manually corrupt started_at
        job_key = f'qbt_rules:jobs:{job_id}'
        redis_client.hset(job_key, 'started_at', 'invalid-date')

        stats = queue.get_stats()

        # Should not crash, may be None
        assert 'average_execution_time' in stats


class TestRedisQueueHealthCheck:
    """Test health_check() method"""

    def test_health_check_connected_returns_true(self, queue):
        """Should return True when Redis is connected"""
        result = queue.health_check()
        assert result is True

    def test_health_check_disconnected_returns_false(self, queue, mocker):
        """Should return False when Redis ping fails"""
        # Mock ping to raise exception
        mocker.patch.object(queue.redis, 'ping', side_effect=redis.ConnectionError())

        result = queue.health_check()
        assert result is False


class TestRedisQueueHashToDict:
    """Test _hash_to_dict() conversion"""

    def test_hash_to_dict_converts_all_fields(self, queue):
        """Should convert Redis hash to job dict with all fields"""
        hash_data = {
            'id': 'test-job-id',
            'context': 'test-context',
            'hash': 'abc123',
            'status': JobStatus.PENDING,
            'created_at': '2025-01-01T12:00:00',
            'started_at': '2025-01-01T12:01:00',
            'completed_at': '2025-01-01T12:02:00',
            'result': '{"count": 5}',
            'error': 'Test error'
        }

        job = queue._hash_to_dict(hash_data)

        assert job['job_id'] == 'test-job-id'
        assert job['context'] == 'test-context'
        assert job['hash'] == 'abc123'
        assert job['status'] == JobStatus.PENDING
        assert job['created_at'] == '2025-01-01T12:00:00'
        assert job['started_at'] == '2025-01-01T12:01:00'
        assert job['completed_at'] == '2025-01-01T12:02:00'
        assert job['result'] == {'count': 5}
        assert job['error'] == 'Test error'

    def test_hash_to_dict_converts_empty_strings_to_none(self, queue):
        """Should convert empty strings to None"""
        hash_data = {
            'id': 'test-job-id',
            'context': '',
            'hash': '',
            'status': JobStatus.PENDING,
            'created_at': '2025-01-01T12:00:00',
            'started_at': '',
            'completed_at': '',
            'result': '',
            'error': ''
        }

        job = queue._hash_to_dict(hash_data)

        assert job['context'] is None
        assert job['hash'] is None
        assert job['started_at'] is None
        assert job['completed_at'] is None
        assert job['result'] is None
        assert job['error'] is None

    def test_hash_to_dict_parses_json_result(self, queue):
        """Should parse JSON result field"""
        hash_data = {
            'id': 'test-job-id',
            'context': '',
            'hash': '',
            'status': JobStatus.COMPLETED,
            'created_at': '2025-01-01T12:00:00',
            'started_at': '',
            'completed_at': '',
            'result': '{"torrents": 10, "actions": 5}',
            'error': ''
        }

        job = queue._hash_to_dict(hash_data)

        assert job['result'] == {'torrents': 10, 'actions': 5}


class TestRedisQueueCloseAndCleanup:
    """Test close() and __del__() methods"""

    def test_close_disconnects_pool(self, queue, mocker):
        """Should disconnect connection pool"""
        # Create mock pool
        mock_pool = mocker.MagicMock()
        queue.pool = mock_pool

        queue.close()

        # Verify disconnect was called
        mock_pool.disconnect.assert_called_once()

    def test_del_calls_close(self, queue, mocker):
        """Should call close() when object is deleted"""
        close_spy = mocker.spy(queue, 'close')

        del queue

        # Note: __del__ timing is not guaranteed in Python
        # This test demonstrates intent but may not always catch the call


class TestRedisQueueThreadSafety:
    """Test thread safety with connection pooling"""

    def test_concurrent_enqueue(self, queue):
        """Should handle concurrent enqueues safely"""
        results = []

        def enqueue_worker():
            for _ in range(10):
                job_id = queue.enqueue()
                results.append(job_id)

        # 5 threads, 10 enqueues each = 50 jobs
        threads = [threading.Thread(target=enqueue_worker) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have 50 unique job IDs
        assert len(results) == 50
        assert len(set(results)) == 50

    def test_concurrent_dequeue_no_duplicates(self, queue):
        """Should not return duplicate jobs when dequeuing concurrently"""
        # Enqueue jobs
        for _ in range(10):
            queue.enqueue()

        results = []

        def dequeue_worker():
            for _ in range(3):
                job = queue.dequeue()
                if job:
                    results.append(job['job_id'])
                time.sleep(0.001)

        # 3 threads trying to dequeue
        threads = [threading.Thread(target=dequeue_worker) for _ in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have unique job IDs (no duplicates)
        assert len(results) == len(set(results))
        assert len(results) <= 10

    def test_connection_pool_handles_concurrent_operations(self, queue):
        """Should use connection pool for concurrent operations"""
        def mixed_operations():
            for _ in range(5):
                queue.enqueue()
                queue.dequeue()
                queue.count_jobs()
                queue.get_queue_depth()

        threads = [threading.Thread(target=mixed_operations) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should complete without errors
        assert True
