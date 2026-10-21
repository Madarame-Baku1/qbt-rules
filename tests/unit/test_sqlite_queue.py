"""
Comprehensive tests for SQLite Queue Backend

Tests all SQLite queue operations including:
- Database initialization and schema
- Thread safety
- CRUD operations
- Queue FIFO ordering
- Transaction management
- Statistics and health checks
"""

import pytest
import sqlite3
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

from qbt_rules.queue_backends.sqlite_queue import SQLiteQueue
from qbt_rules.queue_manager import JobStatus, QueueManager


# ============================================================================
# Initialization and Database Setup Tests
# ============================================================================

class TestSQLiteQueueInitialization:
    """Test SQLite queue initialization"""

    def test_init_creates_database_file(self, tmp_path):
        """Initialization creates database file"""
        db_path = tmp_path / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        assert db_path.exists()
        assert db_path.is_file()
        queue.close()

    def test_init_creates_parent_directories(self, tmp_path):
        """Initialization creates parent directories if missing"""
        db_path = tmp_path / "nested" / "path" / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        assert db_path.parent.exists()
        assert db_path.exists()
        queue.close()

    def test_init_creates_schema_tables(self, tmp_path):
        """Initialization creates required database tables"""
        db_path = tmp_path / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table'
            ORDER BY name
        """)
        tables = [row[0] for row in cursor.fetchall()]

        assert 'jobs' in tables
        assert 'queue' in tables
        assert 'schema_version' in tables

        conn.close()
        queue.close()

    def test_init_creates_indexes(self, tmp_path):
        """Initialization creates database indexes"""
        db_path = tmp_path / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='index' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """)
        indexes = [row[0] for row in cursor.fetchall()]

        assert 'idx_jobs_status' in indexes
        assert 'idx_jobs_created_at' in indexes
        assert 'idx_jobs_context' in indexes
        assert 'idx_jobs_completed_at' in indexes
        assert 'idx_queue_priority' in indexes

        conn.close()
        queue.close()

    def test_init_sets_schema_version(self, tmp_path):
        """Initialization sets schema version"""
        db_path = tmp_path / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute('SELECT MAX(version) FROM schema_version')
        version = cursor.fetchone()[0]

        assert version == SQLiteQueue.SCHEMA_VERSION
        assert version == 2

        conn.close()
        queue.close()

    def test_init_with_existing_database(self, tmp_path):
        """Initialization works with existing database"""
        db_path = tmp_path / "test.db"

        # Create first queue
        queue1 = SQLiteQueue(db_path=str(db_path))
        job_id = queue1.enqueue(context="test")
        queue1.close()

        # Create second queue with same database
        queue2 = SQLiteQueue(db_path=str(db_path))
        job = queue2.get_job(job_id)

        assert job is not None
        assert job['job_id'] == job_id
        queue2.close()

    def test_init_enables_wal_mode(self, tmp_path):
        """Initialization enables WAL mode for concurrency"""
        db_path = tmp_path / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute('PRAGMA journal_mode')
        mode = cursor.fetchone()[0]

        assert mode.upper() == 'WAL'

        conn.close()
        queue.close()

    def test_init_enables_foreign_keys(self, tmp_path):
        """Initialization enables foreign key constraints"""
        db_path = tmp_path / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        conn = queue._get_connection()
        cursor = conn.execute('PRAGMA foreign_keys')
        enabled = cursor.fetchone()[0]

        assert enabled == 1
        queue.close()

    def test_db_path_stored_as_path_object(self, tmp_path):
        """Database path stored as Path object"""
        db_path = tmp_path / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        assert isinstance(queue.db_path, Path)
        assert queue.db_path == db_path
        queue.close()

    def test_inherits_from_queue_manager(self, tmp_path):
        """SQLiteQueue inherits from QueueManager"""
        db_path = tmp_path / "test.db"
        queue = SQLiteQueue(db_path=str(db_path))

        assert isinstance(queue, QueueManager)
        queue.close()

    def test_schema_migration_from_old_version(self, tmp_path, caplog):
        """Schema migration runs when database has old version"""
        import logging
        db_path = tmp_path / "test.db"

        # Create database with proper schema structure but version 0
        # This simulates an old database that needs migration
        conn = sqlite3.connect(str(db_path))

        # Create schema_version table with proper structure
        conn.execute('''
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Insert old version (0) to trigger migration path
        conn.execute('INSERT INTO schema_version (version) VALUES (0)')

        # Also need to create the main tables so it's a valid old schema
        # (otherwise it might be treated as corrupted)
        conn.execute('''
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                context TEXT,
                hash TEXT,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                result TEXT,
                error TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE queue (
                job_id TEXT PRIMARY KEY,
                priority INTEGER NOT NULL DEFAULT 0,
                enqueued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

        # Initialize queue - should trigger migration from v0 to the current version
        with caplog.at_level(logging.INFO):
            queue = SQLiteQueue(db_path=str(db_path))

        assert any(f"Schema migration from v0 to v{SQLiteQueue.SCHEMA_VERSION} completed"
                   in record.message for record in caplog.records)
        queue.close()

    def test_v1_database_migrates_to_v2_keeping_jobs(self, tmp_path):
        """A v1 database (before per-job dry-run) gains jobs.dry_run, existing jobs default to False"""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('CREATE TABLE schema_version (version INTEGER PRIMARY KEY, '
                     'applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        conn.execute('INSERT INTO schema_version (version) VALUES (1)')
        conn.execute('''CREATE TABLE jobs (id TEXT PRIMARY KEY, context TEXT, hash TEXT,
                        status TEXT NOT NULL, created_at TIMESTAMP NOT NULL, started_at TIMESTAMP,
                        completed_at TIMESTAMP, result TEXT, error TEXT)''')
        conn.execute('''CREATE TABLE queue (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL UNIQUE, priority INTEGER DEFAULT 0)''')
        conn.execute("INSERT INTO jobs (id, context, status, created_at) "
                     "VALUES ('old-job', 'nightly', 'completed', '2026-10-01T00:00:00+00:00')")
        conn.commit()
        conn.close()

        queue = SQLiteQueue(db_path=str(db_path))

        assert queue.get_job('old-job')['dry_run'] is False
        new_id = queue.enqueue(context='manual', dry_run=True)
        assert queue.get_job(new_id)['dry_run'] is True
        conn = sqlite3.connect(str(db_path))
        assert conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] == 2
        conn.close()
        queue.close()

    def test_reopening_current_database_does_not_migrate_again(self, tmp_path):
        """A second open of a v2 database must not try to add the column twice"""
        db_path = tmp_path / "test.db"
        SQLiteQueue(db_path=str(db_path)).close()
        queue = SQLiteQueue(db_path=str(db_path))
        assert queue.get_job(queue.enqueue(dry_run=True))['dry_run'] is True
        queue.close()

    def test_enqueue_defaults_to_not_dry_run(self, tmp_path):
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        assert queue.get_job(queue.enqueue(context='manual'))['dry_run'] is False
        queue.close()

        queue.close()


# ============================================================================
# Enqueue Operation Tests
# ============================================================================

class TestSQLiteQueueEnqueue:
    """Test job enqueueing"""

    def test_enqueue_creates_job(self, tmp_path):
        """enqueue() creates job in database"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue(context="weekly-cleanup")

        job = queue.get_job(job_id)
        assert job is not None
        assert job['job_id'] == job_id
        queue.close()

    def test_enqueue_returns_job_id(self, tmp_path):
        """enqueue() returns job ID"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        assert isinstance(job_id, str)
        assert len(job_id) > 0
        queue.close()

    def test_enqueue_with_context(self, tmp_path):
        """enqueue() with context parameter"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue(context="torrent-imported")

        job = queue.get_job(job_id)
        assert job['context'] == "torrent-imported"
        queue.close()

    def test_enqueue_with_hash_filter(self, tmp_path):
        """enqueue() with hash filter"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue(hash_filter="abc123def456")

        job = queue.get_job(job_id)
        assert job['hash'] == "abc123def456"
        queue.close()

    def test_enqueue_with_both_params(self, tmp_path):
        """enqueue() with both context and hash"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue(context="weekly-cleanup", hash_filter="abc123")

        job = queue.get_job(job_id)
        assert job['context'] == "weekly-cleanup"
        assert job['hash'] == "abc123"
        queue.close()

    def test_enqueue_sets_pending_status(self, tmp_path):
        """enqueue() sets status to pending"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        job = queue.get_job(job_id)
        assert job['status'] == JobStatus.PENDING
        queue.close()

    def test_enqueue_sets_created_at(self, tmp_path):
        """enqueue() sets created_at timestamp"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        before = datetime.now(timezone.utc)
        job_id = queue.enqueue()
        after = datetime.now(timezone.utc)

        job = queue.get_job(job_id)
        created_str = job['created_at']

        # Parse ISO timestamp (SQLite stores as string)
        if isinstance(created_str, str):
            created_dt = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
        else:
            created_dt = created_str

        # Should be between before and after
        assert before <= created_dt <= after
        queue.close()

    def test_enqueue_initializes_null_fields(self, tmp_path):
        """enqueue() initializes optional fields as None"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        job = queue.get_job(job_id)
        assert job['started_at'] is None
        assert job['completed_at'] is None
        assert job['result'] is None
        assert job['error'] is None
        queue.close()

    def test_enqueue_multiple_jobs(self, tmp_path):
        """enqueue() can create multiple jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_ids = []
        for i in range(10):
            job_id = queue.enqueue(context=f"test-{i}")
            job_ids.append(job_id)

        # All jobs should exist
        for job_id in job_ids:
            job = queue.get_job(job_id)
            assert job is not None

        queue.close()

    def test_enqueue_adds_to_queue_table(self, tmp_path):
        """enqueue() adds job to queue table"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        cursor = conn.execute('SELECT job_id FROM queue WHERE job_id = ?', (job_id,))
        row = cursor.fetchone()

        assert row is not None
        assert row[0] == job_id

        conn.close()
        queue.close()

    def test_enqueue_unique_job_ids(self, tmp_path):
        """enqueue() generates unique job IDs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_ids = [queue.enqueue() for _ in range(100)]
        assert len(job_ids) == len(set(job_ids))
        queue.close()


# ============================================================================
# Dequeue Operation Tests
# ============================================================================

class TestSQLiteQueueDequeue:
    """Test job dequeueing"""

    def test_dequeue_returns_next_job(self, tmp_path):
        """dequeue() returns next job from queue"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue(context="test")

        job = queue.dequeue()
        assert job is not None
        assert job['job_id'] == job_id
        queue.close()

    def test_dequeue_empty_queue_returns_none(self, tmp_path):
        """dequeue() returns None when queue is empty"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job = queue.dequeue()
        assert job is None
        queue.close()

    def test_dequeue_fifo_ordering(self, tmp_path):
        """dequeue() returns jobs in FIFO order"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        # Enqueue 5 jobs
        job_ids = []
        for i in range(5):
            job_id = queue.enqueue(context=f"job-{i}")
            job_ids.append(job_id)
            time.sleep(0.01)  # Ensure different timestamps

        # Dequeue should return in same order
        for expected_id in job_ids:
            job = queue.dequeue()
            assert job['job_id'] == expected_id

        queue.close()

    def test_dequeue_marks_as_processing(self, tmp_path):
        """dequeue() marks job as processing"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        job = queue.dequeue()
        assert job['status'] == JobStatus.PROCESSING
        queue.close()

    def test_dequeue_sets_started_at(self, tmp_path):
        """dequeue() sets started_at timestamp"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        before = datetime.now(timezone.utc)
        job = queue.dequeue()
        after = datetime.now(timezone.utc)

        started_str = job['started_at']
        if isinstance(started_str, str):
            started_dt = datetime.fromisoformat(started_str.replace('Z', '+00:00'))
        else:
            started_dt = started_str

        assert before <= started_dt <= after
        queue.close()

    def test_dequeue_removes_from_queue_table(self, tmp_path):
        """dequeue() removes job from queue table"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        job = queue.dequeue()

        # Should not be in queue table anymore
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        cursor = conn.execute('SELECT COUNT(*) FROM queue WHERE job_id = ?', (job_id,))
        count = cursor.fetchone()[0]

        assert count == 0
        conn.close()
        queue.close()

    def test_dequeue_keeps_job_in_jobs_table(self, tmp_path):
        """dequeue() keeps job in jobs table"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        job = queue.dequeue()

        # Should still be in jobs table
        stored_job = queue.get_job(job_id)
        assert stored_job is not None
        queue.close()

    def test_dequeue_atomic_operation(self, tmp_path):
        """dequeue() is atomic (transaction-safe) -- regression guard for
        the missing busy_timeout/BEGIN IMMEDIATE bug (see BUGS.md): 5
        threads racing to dequeue the same single job used to raise
        'sqlite3.OperationalError: database is locked' in worker threads,
        silently swallowed by pytest as a warning rather than a test
        failure. Explicitly capturing exceptions here turns any
        regression into a real assertion failure."""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        # Simulate concurrent dequeue attempts
        results = []
        errors = []
        def dequeue_worker():
            try:
                job = queue.dequeue()
                results.append(job)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=dequeue_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"dequeue() raised in {len(errors)} thread(s): {errors}"

        # Only one thread should get the job
        non_none_results = [r for r in results if r is not None]
        assert len(non_none_results) == 1
        queue.close()

    def test_dequeue_returns_all_job_fields(self, tmp_path):
        """dequeue() returns complete job dict"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue(context="test", hash_filter="abc123")

        job = queue.dequeue()

        assert 'job_id' in job
        assert 'context' in job
        assert 'hash' in job
        assert 'status' in job
        assert 'created_at' in job
        assert 'started_at' in job
        assert 'completed_at' in job
        assert 'result' in job
        assert 'error' in job
        queue.close()

    def test_dequeue_only_pending_jobs(self, tmp_path):
        """dequeue() only returns pending jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job1_id = queue.enqueue()
        job2_id = queue.enqueue()
        job3_id = queue.enqueue()

        # Dequeue and complete first job (removes from queue)
        job1 = queue.dequeue()
        queue.update_status(job1_id, JobStatus.COMPLETED, completed_at=datetime.now(timezone.utc))

        # Should get second job (first is no longer in queue)
        job = queue.dequeue()
        assert job['job_id'] == job2_id
        queue.close()

    def test_dequeue_race_condition_job_deleted(self, tmp_path):
        """dequeue() returns None if job was deleted between SELECT and fetch"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        # Create a job
        job_id = queue.enqueue()

        # Manually delete job from jobs table (simulating race condition)
        # This creates scenario where job_id is in queue but not in jobs table
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
        conn.commit()
        conn.close()

        # Dequeue should return None (job_row is None at line 224-225)
        job = queue.dequeue()
        assert job is None

        queue.close()


# ============================================================================
# Get Job Operation Tests
# ============================================================================

class TestSQLiteQueueGetJob:
    """Test get_job() method"""

    def test_get_job_returns_job_by_id(self, tmp_path):
        """get_job() returns job by ID"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue(context="test")

        job = queue.get_job(job_id)
        assert job is not None
        assert job['job_id'] == job_id
        assert job['context'] == "test"
        queue.close()

    def test_get_job_nonexistent_returns_none(self, tmp_path):
        """get_job() returns None for nonexistent job"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job = queue.get_job("nonexistent-id")
        assert job is None
        queue.close()

    def test_get_job_returns_dict(self, tmp_path):
        """get_job() returns dictionary"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        job = queue.get_job(job_id)
        assert isinstance(job, dict)
        queue.close()

    def test_get_job_includes_all_fields(self, tmp_path):
        """get_job() includes all job fields"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue(context="test", hash_filter="abc123")

        job = queue.get_job(job_id)

        required_fields = ['job_id', 'context', 'hash', 'status', 'created_at',
                          'started_at', 'completed_at', 'result', 'error']
        for field in required_fields:
            assert field in job
        queue.close()

    def test_get_job_with_result_data(self, tmp_path):
        """get_job() retrieves result data correctly"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        result_data = {'torrents': 10, 'matched': 5}
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc),
                          result=result_data)

        job = queue.get_job(job_id)
        assert job['result'] == result_data
        queue.close()

    def test_get_job_after_status_update(self, tmp_path):
        """get_job() returns updated job after status change"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        # Update status
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        job = queue.get_job(job_id)
        assert job['status'] == JobStatus.COMPLETED
        queue.close()


# ============================================================================
# List Jobs Operation Tests
# ============================================================================

class TestSQLiteQueueListJobs:
    """Test list_jobs() method"""

    def test_list_jobs_returns_all_jobs(self, tmp_path):
        """list_jobs() returns all jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        # Create 5 jobs
        for i in range(5):
            queue.enqueue(context=f"job-{i}")

        jobs = queue.list_jobs()
        assert len(jobs) == 5
        queue.close()

    def test_list_jobs_returns_list(self, tmp_path):
        """list_jobs() returns a list"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        jobs = queue.list_jobs()
        assert isinstance(jobs, list)
        queue.close()

    def test_list_jobs_empty_queue(self, tmp_path):
        """list_jobs() returns empty list when no jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        jobs = queue.list_jobs()
        assert jobs == []
        queue.close()

    def test_list_jobs_filter_by_status(self, tmp_path):
        """list_jobs() filters by status"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job1 = queue.enqueue()
        job2 = queue.enqueue()
        queue.update_status(job1, JobStatus.COMPLETED, completed_at=datetime.now(timezone.utc))

        pending_jobs = queue.list_jobs(status=JobStatus.PENDING)
        completed_jobs = queue.list_jobs(status=JobStatus.COMPLETED)

        assert len(pending_jobs) == 1
        assert len(completed_jobs) == 1
        assert pending_jobs[0]['job_id'] == job2
        assert completed_jobs[0]['job_id'] == job1
        queue.close()

    def test_list_jobs_filter_by_context(self, tmp_path):
        """list_jobs() filters by context"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        queue.enqueue(context="weekly-cleanup")
        queue.enqueue(context="torrent-imported")
        queue.enqueue(context="weekly-cleanup")

        scheduled_jobs = queue.list_jobs(context="weekly-cleanup")
        on_added_jobs = queue.list_jobs(context="torrent-imported")

        assert len(scheduled_jobs) == 2
        assert len(on_added_jobs) == 1
        queue.close()

    def test_list_jobs_pagination_limit(self, tmp_path):
        """list_jobs() respects limit parameter"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        for i in range(10):
            queue.enqueue()

        jobs = queue.list_jobs(limit=5)
        assert len(jobs) == 5
        queue.close()

    def test_list_jobs_pagination_offset(self, tmp_path):
        """list_jobs() respects offset parameter"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        # Create jobs with small delays to ensure ordering
        job_ids = []
        for i in range(10):
            job_id = queue.enqueue(context=f"job-{i}")
            job_ids.append(job_id)
            time.sleep(0.01)

        # Get first page
        page1 = queue.list_jobs(limit=3, offset=0)
        # Get second page
        page2 = queue.list_jobs(limit=3, offset=3)

        assert len(page1) == 3
        assert len(page2) == 3
        # Pages should not overlap
        page1_ids = {j['job_id'] for j in page1}
        page2_ids = {j['job_id'] for j in page2}
        assert not page1_ids.intersection(page2_ids)
        queue.close()

    def test_list_jobs_max_limit_100(self, tmp_path):
        """list_jobs() enforces max limit of 100"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        for i in range(150):
            queue.enqueue()

        jobs = queue.list_jobs(limit=200)
        assert len(jobs) == 100  # Should be capped at 100
        queue.close()

    def test_list_jobs_ordered_by_created_at_desc(self, tmp_path):
        """list_jobs() orders by created_at DESC (newest first)"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        # Create jobs with delays
        job_ids = []
        for i in range(5):
            job_id = queue.enqueue(context=f"job-{i}")
            job_ids.append(job_id)
            time.sleep(0.02)

        jobs = queue.list_jobs()

        # Newest should be first
        assert jobs[0]['job_id'] == job_ids[-1]  # Last created
        assert jobs[-1]['job_id'] == job_ids[0]  # First created
        queue.close()

    def test_list_jobs_combined_filters(self, tmp_path):
        """list_jobs() handles combined status and context filters"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job1 = queue.enqueue(context="weekly-cleanup")
        job2 = queue.enqueue(context="torrent-imported")
        job3 = queue.enqueue(context="weekly-cleanup")

        queue.update_status(job1, JobStatus.COMPLETED, completed_at=datetime.now(timezone.utc))

        # Filter: weekly-cleanup AND pending
        jobs = queue.list_jobs(status=JobStatus.PENDING, context="weekly-cleanup")

        assert len(jobs) == 1
        assert jobs[0]['job_id'] == job3
        queue.close()


# ============================================================================
# Count Jobs Operation Tests
# ============================================================================

class TestSQLiteQueueCountJobs:
    """Test count_jobs() method"""

    def test_count_jobs_total(self, tmp_path):
        """count_jobs() returns total job count"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        for i in range(10):
            queue.enqueue()

        count = queue.count_jobs()
        assert count == 10
        queue.close()

    def test_count_jobs_empty_queue(self, tmp_path):
        """count_jobs() returns 0 for empty queue"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        count = queue.count_jobs()
        assert count == 0
        queue.close()

    def test_count_jobs_by_status(self, tmp_path):
        """count_jobs() counts by status"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job1 = queue.enqueue()
        job2 = queue.enqueue()
        job3 = queue.enqueue()

        queue.update_status(job1, JobStatus.COMPLETED, completed_at=datetime.now(timezone.utc))
        queue.update_status(job2, JobStatus.FAILED, completed_at=datetime.now(timezone.utc), error="Test error")

        assert queue.count_jobs(JobStatus.PENDING) == 1
        assert queue.count_jobs(JobStatus.COMPLETED) == 1
        assert queue.count_jobs(JobStatus.FAILED) == 1
        assert queue.count_jobs(JobStatus.PROCESSING) == 0
        queue.close()

    def test_count_jobs_after_dequeue(self, tmp_path):
        """count_jobs() updates after dequeue"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        queue.enqueue()
        queue.enqueue()

        assert queue.count_jobs(JobStatus.PENDING) == 2

        queue.dequeue()

        assert queue.count_jobs(JobStatus.PENDING) == 1
        assert queue.count_jobs(JobStatus.PROCESSING) == 1
        queue.close()


# Due to character limit, I'll continue in next part...

# ============================================================================
# Update Status Operation Tests
# ============================================================================

class TestSQLiteQueueUpdateStatus:
    """Test update_status() method"""

    def test_update_status_changes_status(self, tmp_path):
        """update_status() changes job status"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        success = queue.update_status(job_id, JobStatus.COMPLETED,
                                      completed_at=datetime.now(timezone.utc))

        job = queue.get_job(job_id)
        assert success is True
        assert job['status'] == JobStatus.COMPLETED
        queue.close()

    def test_update_status_sets_started_at(self, tmp_path):
        """update_status() sets started_at"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        started = datetime.now(timezone.utc)
        queue.update_status(job_id, JobStatus.PROCESSING, started_at=started)

        job = queue.get_job(job_id)
        assert job['started_at'] is not None
        queue.close()

    def test_update_status_sets_completed_at(self, tmp_path):
        """update_status() sets completed_at"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        completed = datetime.now(timezone.utc)
        queue.update_status(job_id, JobStatus.COMPLETED, completed_at=completed)

        job = queue.get_job(job_id)
        assert job['completed_at'] is not None
        queue.close()

    def test_update_status_sets_result(self, tmp_path):
        """update_status() sets result data"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        result_data = {'torrents': 10, 'actions': 5}
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc),
                          result=result_data)

        job = queue.get_job(job_id)
        assert job['result'] == result_data
        queue.close()

    def test_update_status_sets_error(self, tmp_path):
        """update_status() sets error message"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        error_msg = "Connection failed"
        queue.update_status(job_id, JobStatus.FAILED,
                          completed_at=datetime.now(timezone.utc),
                          error=error_msg)

        job = queue.get_job(job_id)
        assert job['error'] == error_msg
        queue.close()

    def test_update_status_invalid_status_raises_error(self, tmp_path):
        """update_status() raises ValueError for invalid status"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        with pytest.raises(ValueError) as exc_info:
            queue.update_status(job_id, "invalid_status")

        assert "Invalid status" in str(exc_info.value)
        queue.close()

    def test_update_status_nonexistent_job_returns_false(self, tmp_path):
        """update_status() returns False for nonexistent job"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        success = queue.update_status("nonexistent", JobStatus.COMPLETED,
                                     completed_at=datetime.now(timezone.utc))

        assert success is False
        queue.close()

    def test_update_status_multiple_fields(self, tmp_path):
        """update_status() can update multiple fields at once"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        started = datetime.now(timezone.utc)
        completed = datetime.now(timezone.utc)
        result = {'count': 42}

        queue.update_status(
            job_id,
            JobStatus.COMPLETED,
            started_at=started,
            completed_at=completed,
            result=result
        )

        job = queue.get_job(job_id)
        assert job['status'] == JobStatus.COMPLETED
        assert job['started_at'] is not None
        assert job['completed_at'] is not None
        assert job['result'] == result
        queue.close()


# ============================================================================
# Cancel Job Operation Tests
# ============================================================================

class TestSQLiteQueueCancelJob:
    """Test cancel_job() method"""

    def test_cancel_job_pending(self, tmp_path):
        """cancel_job() cancels pending job"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        success = queue.cancel_job(job_id)

        job = queue.get_job(job_id)
        assert success is True
        assert job['status'] == JobStatus.CANCELLED
        queue.close()

    def test_cancel_job_removes_from_queue(self, tmp_path):
        """cancel_job() removes job from queue table"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        queue.cancel_job(job_id)

        # Should not be dequeued
        next_job = queue.dequeue()
        assert next_job is None
        queue.close()

    def test_cancel_job_nonexistent_returns_false(self, tmp_path):
        """cancel_job() returns False for nonexistent job"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        success = queue.cancel_job("nonexistent")
        assert success is False
        queue.close()

    def test_cancel_job_processing_returns_false(self, tmp_path):
        """cancel_job() returns False for processing job"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        # Mark as processing
        queue.dequeue()

        success = queue.cancel_job(job_id)
        assert success is False
        queue.close()

    def test_cancel_job_completed_returns_false(self, tmp_path):
        """cancel_job() returns False for completed job"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        success = queue.cancel_job(job_id)
        assert success is False
        queue.close()

    def test_cancel_job_transaction_safe(self, tmp_path):
        """cancel_job() is transaction-safe -- regression guard for the
        missing busy_timeout/BEGIN IMMEDIATE bug (see BUGS.md): concurrent
        cancel_job() calls used to raise 'sqlite3.OperationalError:
        database is locked' in worker threads."""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))
        job_id = queue.enqueue()

        # Simulate concurrent cancellation
        results = []
        errors = []
        def cancel_worker():
            try:
                success = queue.cancel_job(job_id)
                results.append(success)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=cancel_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"cancel_job() raised in {len(errors)} thread(s): {errors}"

        # Only one thread should succeed
        assert sum(results) == 1
        queue.close()


# ============================================================================
# Cleanup Old Jobs Tests
# ============================================================================

class TestSQLiteQueueCleanup:
    """Test cleanup_old_jobs() method"""

    def test_cleanup_old_completed_jobs(self, tmp_path):
        """cleanup_old_jobs() removes old completed jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.enqueue()
        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        queue.update_status(job_id, JobStatus.COMPLETED, completed_at=old_time)

        # Cleanup jobs older than 7 days
        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 1
        assert queue.get_job(job_id) is None
        queue.close()

    def test_cleanup_old_failed_jobs(self, tmp_path):
        """cleanup_old_jobs() removes old failed jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.enqueue()
        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        queue.update_status(job_id, JobStatus.FAILED,
                          completed_at=old_time, error="Test")

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 1
        queue.close()

    def test_cleanup_old_cancelled_jobs(self, tmp_path):
        """cleanup_old_jobs() removes old cancelled jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.enqueue()
        # Cancel and manually set old completed_at
        queue.cancel_job(job_id)
        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        conn = queue._get_connection()
        conn.execute('UPDATE jobs SET completed_at = ? WHERE id = ?',
                    (old_time.isoformat(), job_id))

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 1
        queue.close()

    def test_cleanup_preserves_recent_jobs(self, tmp_path):
        """cleanup_old_jobs() preserves recent jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.enqueue()
        recent_time = datetime.now(timezone.utc)
        queue.update_status(job_id, JobStatus.COMPLETED, completed_at=recent_time)

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 0
        assert queue.get_job(job_id) is not None
        queue.close()

    def test_cleanup_preserves_pending_jobs(self, tmp_path):
        """cleanup_old_jobs() preserves pending jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.enqueue()
        # Don't update status - stays pending

        deleted = queue.cleanup_old_jobs(retention_period=0)

        assert deleted == 0
        assert queue.get_job(job_id) is not None
        queue.close()

    def test_cleanup_preserves_processing_jobs(self, tmp_path):
        """cleanup_old_jobs() preserves processing jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.enqueue()
        queue.dequeue()  # Marks as processing

        deleted = queue.cleanup_old_jobs(retention_period=0)

        assert deleted == 0
        queue.close()

    def test_cleanup_returns_count(self, tmp_path):
        """cleanup_old_jobs() returns count of deleted jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        for i in range(5):
            job_id = queue.enqueue()
            queue.update_status(job_id, JobStatus.COMPLETED, completed_at=old_time)

        deleted = queue.cleanup_old_jobs(retention_period=7 * 86400)

        assert deleted == 5
        queue.close()


# ============================================================================
# Queue Depth Tests
# ============================================================================

class TestSQLiteQueueDepth:
    """Test get_queue_depth() method"""

    def test_get_queue_depth_empty(self, tmp_path):
        """get_queue_depth() returns 0 for empty queue"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        depth = queue.get_queue_depth()
        assert depth == 0
        queue.close()

    def test_get_queue_depth_with_jobs(self, tmp_path):
        """get_queue_depth() returns count of pending jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        for i in range(5):
            queue.enqueue()

        depth = queue.get_queue_depth()
        assert depth == 5
        queue.close()

    def test_get_queue_depth_excludes_processing(self, tmp_path):
        """get_queue_depth() excludes processing jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        queue.enqueue()
        queue.enqueue()
        queue.dequeue()  # One becomes processing

        depth = queue.get_queue_depth()
        assert depth == 1
        queue.close()

    def test_get_queue_depth_excludes_completed(self, tmp_path):
        """get_queue_depth() excludes completed jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.enqueue()
        queue.enqueue()

        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc))

        depth = queue.get_queue_depth()
        assert depth == 1
        queue.close()


# ============================================================================
# Statistics Tests
# ============================================================================

class TestSQLiteQueueStats:
    """Test get_stats() method"""

    def test_get_stats_returns_dict(self, tmp_path):
        """get_stats() returns dictionary"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        stats = queue.get_stats()
        assert isinstance(stats, dict)
        queue.close()

    def test_get_stats_counts_total_jobs(self, tmp_path):
        """get_stats() includes total job count"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        for i in range(10):
            queue.enqueue()

        stats = queue.get_stats()
        assert stats['total_jobs'] == 10
        queue.close()

    def test_get_stats_counts_by_status(self, tmp_path):
        """get_stats() counts jobs by status"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job1 = queue.enqueue()
        job2 = queue.enqueue()
        job3 = queue.enqueue()
        job4 = queue.enqueue()

        queue.update_status(job1, JobStatus.COMPLETED, completed_at=datetime.now(timezone.utc))
        queue.update_status(job2, JobStatus.FAILED, completed_at=datetime.now(timezone.utc), error="Test")
        queue.cancel_job(job3)
        # job4 stays pending

        stats = queue.get_stats()
        assert stats['pending'] == 1
        assert stats['processing'] == 0
        assert stats['completed'] == 1
        assert stats['failed'] == 1
        assert stats['cancelled'] == 1
        queue.close()

    def test_get_stats_average_execution_time(self, tmp_path):
        """get_stats() calculates average execution time"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.enqueue()
        started = datetime.now(timezone.utc)
        completed = started + timedelta(seconds=10)

        queue.update_status(job_id, JobStatus.COMPLETED,
                          started_at=started,
                          completed_at=completed)

        stats = queue.get_stats()
        assert stats['average_execution_time'] is not None
        assert 9 <= stats['average_execution_time'] <= 11  # ~10 seconds
        queue.close()

    def test_get_stats_average_execution_time_none_when_no_completed(self, tmp_path):
        """get_stats() returns None for average when no completed jobs"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        queue.enqueue()

        stats = queue.get_stats()
        assert stats['average_execution_time'] is None
        queue.close()


# ============================================================================
# Health Check Tests
# ============================================================================

class TestSQLiteQueueHealthCheck:
    """Test health_check() method"""

    def test_health_check_returns_true_when_healthy(self, tmp_path):
        """health_check() returns True when database accessible"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        is_healthy = queue.health_check()
        assert is_healthy is True
        queue.close()

    def test_health_check_returns_bool(self, tmp_path):
        """health_check() returns boolean"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        result = queue.health_check()
        assert isinstance(result, bool)
        queue.close()

    def test_health_check_returns_false_on_database_error(self, tmp_path, mocker, caplog):
        """health_check() returns False and logs error when database fails"""
        import logging
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        # Mock _get_connection to raise an exception
        mocker.patch.object(queue, '_get_connection', side_effect=Exception("Database connection failed"))

        # Run health check
        with caplog.at_level(logging.ERROR):
            is_healthy = queue.health_check()

        # Should return False
        assert is_healthy is False

        # Should log error
        assert any("Queue health check failed" in record.message and
                   "Database connection failed" in record.message
                   for record in caplog.records)

        queue.close()


# ============================================================================
# Thread Safety Tests
# ============================================================================

class TestSQLiteQueueThreadSafety:
    """Test thread-safety of SQLite queue"""

    def test_concurrent_enqueue(self, tmp_path):
        """Multiple threads can enqueue concurrently"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_ids = []
        def enqueue_worker():
            for i in range(10):
                job_id = queue.enqueue(context="concurrent")
                job_ids.append(job_id)

        threads = [threading.Thread(target=enqueue_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All jobs should be created
        assert len(job_ids) == 50
        assert len(set(job_ids)) == 50  # All unique
        queue.close()

    def test_concurrent_dequeue(self, tmp_path):
        """Multiple threads can dequeue concurrently without duplicates --
        regression guard for the missing busy_timeout/BEGIN IMMEDIATE bug
        (see BUGS.md): concurrent dequeue() calls used to raise
        'sqlite3.OperationalError: database is locked' in worker threads."""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        # Enqueue jobs
        for i in range(10):
            queue.enqueue(context=f"job-{i}")

        dequeued_jobs = []
        errors = []
        def dequeue_worker():
            while True:
                try:
                    job = queue.dequeue()
                except Exception as e:
                    errors.append(e)
                    break
                if job:
                    dequeued_jobs.append(job)
                else:
                    break
                time.sleep(0.001)

        threads = [threading.Thread(target=dequeue_worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"dequeue() raised in {len(errors)} thread(s): {errors}"

        # All jobs should be dequeued exactly once
        assert len(dequeued_jobs) == 10
        job_ids = [j['job_id'] for j in dequeued_jobs]
        assert len(job_ids) == len(set(job_ids))  # No duplicates
        queue.close()

    def test_thread_local_connections(self, tmp_path):
        """Each thread gets its own database connection"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        conn_ids = []
        def get_connection_id():
            conn = queue._get_connection()
            conn_ids.append(id(conn))

        threads = [threading.Thread(target=get_connection_id) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread should get different connection object
        assert len(set(conn_ids)) == 5
        queue.close()


# ============================================================================
# Transaction Tests
# ============================================================================

class TestSQLiteQueueTransactions:
    """Test transaction management"""

    def test_transaction_commits_on_success(self, tmp_path):
        """Transaction commits on successful completion"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        with queue._transaction() as conn:
            job_id = queue.generate_job_id()
            conn.execute(
                'INSERT INTO jobs (id, context, hash, status, created_at) VALUES (?, ?, ?, ?, ?)',
                (job_id, "test", None, JobStatus.PENDING, datetime.now(timezone.utc).isoformat())
            )

        # Job should exist
        job = queue.get_job(job_id)
        assert job is not None
        queue.close()

    def test_transaction_rolls_back_on_exception(self, tmp_path):
        """Transaction rolls back on exception"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        job_id = queue.generate_job_id()

        with pytest.raises(Exception):
            with queue._transaction() as conn:
                conn.execute(
                    'INSERT INTO jobs (id, context, hash, status, created_at) VALUES (?, ?, ?, ?, ?)',
                    (job_id, "test", None, JobStatus.PENDING, datetime.now(timezone.utc).isoformat())
                )
                raise Exception("Simulated error")

        # Job should not exist
        job = queue.get_job(job_id)
        assert job is None
        queue.close()


# ============================================================================
# Helper Method Tests
# ============================================================================

class TestSQLiteQueueHelpers:
    """Test helper methods"""

    def test_row_to_dict_conversion(self, tmp_path):
        """_row_to_dict() converts SQLite row to dict correctly"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        result_data = {'key': 'value', 'count': 42}
        job_id = queue.enqueue(context="test", hash_filter="abc123")
        queue.update_status(job_id, JobStatus.COMPLETED,
                          completed_at=datetime.now(timezone.utc),
                          result=result_data)

        job = queue.get_job(job_id)

        # Should be a plain dict
        assert isinstance(job, dict)
        assert job['job_id'] == job_id
        assert job['context'] == "test"
        assert job['hash'] == "abc123"
        assert job['result'] == result_data
        queue.close()

    def test_close_method(self, tmp_path):
        """close() closes database connection"""
        queue = SQLiteQueue(db_path=str(tmp_path / "test.db"))

        # Get connection to create it
        conn = queue._get_connection()

        queue.close()

        # Connection should be closed
        assert not hasattr(queue.local, 'conn')
