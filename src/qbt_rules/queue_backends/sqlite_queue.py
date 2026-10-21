"""
SQLite Queue Backend

File-based queue implementation using SQLite with:
- Thread-safe operations
- Persistent storage
- ACID transactions
- Automatic schema migration
"""

import sqlite3
import json
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from qbt_rules.queue_manager import QueueManager, JobStatus

logger = logging.getLogger(__name__)


class SQLiteQueue(QueueManager):
    """
    SQLite-based queue implementation

    Uses two tables:
    - jobs: Complete job data with all fields
    - queue: Pending job IDs for FIFO ordering

    Thread safety via connection-per-thread pattern.
    """

    SCHEMA_VERSION = 2
    BUSY_TIMEOUT_MS = 5000

    def __init__(self, db_path: str = '/config/qbt-rules.db'):
        """
        Initialize SQLite queue

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.local = threading.local()
        self._connections = []  # Track all connections
        self._conn_lock = threading.Lock()  # Lock for connection tracking

        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_database()

        logger.info(f"SQLite queue initialized: {self.db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """
        Get thread-local database connection

        Returns:
            SQLite connection for current thread
        """
        if not hasattr(self.local, 'conn'):
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None  # Autocommit mode
            )
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrency
            conn.execute('PRAGMA journal_mode=WAL')
            # Enable foreign keys
            conn.execute('PRAGMA foreign_keys=ON')
            # Wait for contended locks instead of failing immediately.
            # Needed alongside BEGIN IMMEDIATE in _transaction() below --
            # without it, a connection blocked at BEGIN IMMEDIATE raises
            # 'database is locked' right away instead of waiting its turn.
            conn.execute(f'PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}')

            # Track this connection
            with self._conn_lock:
                self._connections.append(conn)

            self.local.conn = conn

        return self.local.conn

    @contextmanager
    def _transaction(self):
        """
        Context manager for database transactions

        Usage:
            with self._transaction() as conn:
                conn.execute(...)
        """
        conn = self._get_connection()
        try:
            # IMMEDIATE acquires the write lock up front, rather than
            # deferring it to the first write statement. With a plain
            # BEGIN, concurrent threads can each acquire a read lock via
            # their own SELECT and then all try to upgrade to a write lock
            # at once -- a real deadlock among mutually-blocking readers
            # that busy_timeout alone cannot resolve, since none of them
            # will release their read lock until they win or their
            # transaction ends. BEGIN IMMEDIATE avoids that entirely: only
            # one transaction ever holds the write lock at a time, and
            # everyone else cleanly waits their turn (via busy_timeout)
            # instead of racing.
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise

    def _init_database(self):
        """Initialize database schema and run migrations"""
        conn = self._get_connection()

        # Create schema_version table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Check current schema version
        cursor = conn.execute('SELECT MAX(version) FROM schema_version')
        current_version = cursor.fetchone()[0]

        if current_version is None:
            # Initial schema creation: v1, then the same migrations an
            # existing database gets, so both paths end up identical
            self._create_schema_v1(conn)
            conn.execute('INSERT INTO schema_version (version) VALUES (1)')
            logger.info("Created database schema v1")
            current_version = 1

        if current_version < self.SCHEMA_VERSION:
            self._migrate_schema(conn, current_version)

    def _create_schema_v1(self, conn: sqlite3.Connection):
        """Create initial database schema (version 1)"""

        # Jobs table: Complete job data
        conn.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
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

        # Indexes for efficient queries
        conn.execute('CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_jobs_context ON jobs(context)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_jobs_completed_at ON jobs(completed_at)')

        # Queue table: Pending jobs in FIFO order
        conn.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                priority INTEGER DEFAULT 0,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
        ''')

        conn.execute('CREATE INDEX IF NOT EXISTS idx_queue_priority ON queue(priority, id)')

    def _migrate_schema(self, conn: sqlite3.Connection, from_version: int):
        """
        Run schema migrations

        Args:
            conn: Database connection
            from_version: Current schema version
        """
        if from_version < 2:
            # v2: per-job dry-run, so `qbt-rules --dry-run --context ...` (and
            # /api/execute?dry_run=true) dry-run that job on a live server
            conn.execute('ALTER TABLE jobs ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0')
            conn.execute('INSERT INTO schema_version (version) VALUES (2)')

        logger.info(f"Schema migration from v{from_version} to v{self.SCHEMA_VERSION} completed")

    def enqueue(self, context: Optional[str] = None, hash_filter: Optional[str] = None,
                dry_run: bool = False) -> str:
        """Add job to queue"""
        job_id = self.generate_job_id()
        created_at = datetime.now(timezone.utc)

        with self._transaction() as conn:
            # Insert job
            conn.execute('''
                INSERT INTO jobs (id, context, hash, status, created_at, dry_run)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (job_id, context, hash_filter, JobStatus.PENDING, created_at.isoformat(),
                  1 if dry_run else 0))

            # Add to queue
            conn.execute('''
                INSERT INTO queue (job_id, priority)
                VALUES (?, 0)
            ''', (job_id,))

        logger.debug(f"Enqueued job {job_id} (context={context}, hash={hash_filter})")
        return job_id

    def dequeue(self) -> Optional[Dict[str, Any]]:
        """Get next pending job and mark as processing"""
        with self._transaction() as conn:
            # Get next job from queue (FIFO)
            cursor = conn.execute('''
                SELECT job_id FROM queue
                ORDER BY priority DESC, id ASC
                LIMIT 1
            ''')
            row = cursor.fetchone()

            if not row:
                return None

            job_id = row['job_id']

            # Mark job as processing
            started_at = datetime.now(timezone.utc)
            conn.execute('''
                UPDATE jobs
                SET status = ?, started_at = ?
                WHERE id = ?
            ''', (JobStatus.PROCESSING, started_at.isoformat(), job_id))

            # Remove from queue
            conn.execute('DELETE FROM queue WHERE job_id = ?', (job_id,))

            # Get full job data
            cursor = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,))
            job_row = cursor.fetchone()

        if not job_row:
            return None

        return self._row_to_dict(job_row)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job by ID"""
        conn = self._get_connection()
        cursor = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,))
        row = cursor.fetchone()

        if not row:
            return None

        return self._row_to_dict(row)

    def list_jobs(
        self,
        status: Optional[str] = None,
        context: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """List jobs with filtering"""
        # Limit max to 100
        limit = min(limit, 100)

        conn = self._get_connection()

        # Build query
        query = 'SELECT * FROM jobs WHERE 1=1'
        params = []

        if status:
            query += ' AND status = ?'
            params.append(status)

        if context:
            query += ' AND context = ?'
            params.append(context)

        query += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

        return [self._row_to_dict(row) for row in rows]

    def count_jobs(self, status: Optional[str] = None) -> int:
        """Count jobs by status"""
        conn = self._get_connection()

        if status:
            cursor = conn.execute('SELECT COUNT(*) FROM jobs WHERE status = ?', (status,))
        else:
            cursor = conn.execute('SELECT COUNT(*) FROM jobs')

        return cursor.fetchone()[0]

    def update_status(
        self,
        job_id: str,
        status: str,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> bool:
        """Update job status and fields"""
        if not self.validate_status(status):
            raise ValueError(f"Invalid status: {status}")

        conn = self._get_connection()

        # Build update query
        updates = ['status = ?']
        params = [status]

        if started_at is not None:
            updates.append('started_at = ?')
            params.append(started_at.isoformat())

        if completed_at is not None:
            updates.append('completed_at = ?')
            params.append(completed_at.isoformat())

        if result is not None:
            updates.append('result = ?')
            params.append(json.dumps(result))

        if error is not None:
            updates.append('error = ?')
            params.append(error)

        params.append(job_id)

        query = f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?"
        cursor = conn.execute(query, params)

        return cursor.rowcount > 0

    def cancel_job(self, job_id: str) -> bool:
        """Cancel pending job"""
        with self._transaction() as conn:
            # Check if job is pending
            cursor = conn.execute('SELECT status FROM jobs WHERE id = ?', (job_id,))
            row = cursor.fetchone()

            if not row or row['status'] != JobStatus.PENDING:
                return False

            # Update status to cancelled
            conn.execute('UPDATE jobs SET status = ? WHERE id = ?', (JobStatus.CANCELLED, job_id))

            # Remove from queue
            conn.execute('DELETE FROM queue WHERE job_id = ?', (job_id,))

        logger.debug(f"Cancelled job {job_id}")
        return True

    def cleanup_old_jobs(self, retention_period: int) -> int:
        """Remove old completed/failed/cancelled jobs"""
        cutoff_date = datetime.now(timezone.utc) - timedelta(seconds=retention_period)

        conn = self._get_connection()
        cursor = conn.execute('''
            DELETE FROM jobs
            WHERE status IN (?, ?, ?)
            AND completed_at < ?
        ''', (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, cutoff_date.isoformat()))

        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old jobs older than {cutoff_date}")

        return deleted

    def get_queue_depth(self) -> int:
        """Get number of pending jobs"""
        return self.count_jobs(JobStatus.PENDING)

    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics"""
        conn = self._get_connection()

        # Count by status
        stats = {
            'total_jobs': self.count_jobs(),
            'pending': self.count_jobs(JobStatus.PENDING),
            'processing': self.count_jobs(JobStatus.PROCESSING),
            'completed': self.count_jobs(JobStatus.COMPLETED),
            'failed': self.count_jobs(JobStatus.FAILED),
            'cancelled': self.count_jobs(JobStatus.CANCELLED),
        }

        # Average execution time for completed jobs
        cursor = conn.execute('''
            SELECT AVG(
                (julianday(completed_at) - julianday(started_at)) * 86400
            ) as avg_time
            FROM jobs
            WHERE status = ? AND started_at IS NOT NULL AND completed_at IS NOT NULL
        ''', (JobStatus.COMPLETED,))

        avg_time = cursor.fetchone()[0]
        stats['average_execution_time'] = round(avg_time, 2) if avg_time else None

        return stats

    def health_check(self) -> bool:
        """Check if database is accessible"""
        try:
            conn = self._get_connection()
            conn.execute('SELECT 1')
            return True
        except Exception as e:
            logger.error(f"Queue health check failed: {e}")
            return False

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """
        Convert SQLite row to job dictionary

        Args:
            row: SQLite Row object

        Returns:
            Job dictionary with parsed JSON fields
        """
        job = {
            'job_id': row['id'],
            'context': row['context'],
            'hash': row['hash'],
            'dry_run': bool(row['dry_run']),
            'status': row['status'],
            'created_at': datetime.fromisoformat(row['created_at']) if row['created_at'] else None,
            'started_at': datetime.fromisoformat(row['started_at']) if row['started_at'] else None,
            'completed_at': datetime.fromisoformat(row['completed_at']) if row['completed_at'] else None,
            'result': json.loads(row['result']) if row['result'] else None,
            'error': row['error']
        }

        return job

    def close(self):
        """Close all database connections across all threads"""
        # Close all tracked connections
        with self._conn_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass  # Connection might already be closed
            self._connections.clear()

        # Also close thread-local connection if exists
        if hasattr(self.local, 'conn'):
            delattr(self.local, 'conn')

    def __del__(self):
        """Cleanup on deletion"""
        self.close()
