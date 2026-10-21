"""
Queue Manager - Abstract interface for job queue backends

Supports multiple backend implementations (SQLite, Redis) with consistent API.
All jobs flow through the queue for sequential execution.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import uuid


class JobStatus:
    """Job status constants"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def all(cls) -> List[str]:
        """Get all valid status values"""
        return [cls.PENDING, cls.PROCESSING, cls.COMPLETED, cls.FAILED, cls.CANCELLED]


class QueueManager(ABC):
    """
    Abstract base class for job queue backends

    Implementations must provide:
    - Persistent job storage
    - FIFO queue ordering
    - Thread-safe operations
    - Job status tracking
    - Cleanup of old jobs
    """

    @abstractmethod
    def enqueue(self, context: Optional[str] = None, hash_filter: Optional[str] = None,
                dry_run: bool = False) -> str:
        """
        Add job to queue

        Args:
            context: Context filter for rules (weekly-cleanup, torrent-imported, etc.)
            hash_filter: Optional torrent hash to process single torrent
            dry_run: Run this job in dry-run mode even if the server isn't
                (it can't turn dry-run off when the server has it on)

        Returns:
            Job ID (UUID string)
        """
        pass

    @abstractmethod
    def dequeue(self) -> Optional[Dict[str, Any]]:
        """
        Get next pending job from queue

        Atomically retrieves and marks job as processing.
        Returns None if queue is empty.

        Returns:
            Job dictionary with all fields, or None if queue empty
        """
        pass

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get job by ID

        Args:
            job_id: Job ID (UUID)

        Returns:
            Job dictionary with all fields, or None if not found
        """
        pass

    @abstractmethod
    def list_jobs(
        self,
        status: Optional[str] = None,
        context: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List jobs with filtering and pagination

        Args:
            status: Filter by status (pending, processing, completed, failed, cancelled)
            context: Filter by context
            limit: Maximum number of jobs to return (max 100)
            offset: Pagination offset

        Returns:
            List of job dictionaries ordered by created_at DESC
        """
        pass

    @abstractmethod
    def count_jobs(self, status: Optional[str] = None) -> int:
        """
        Count jobs by status

        Args:
            status: Filter by status, or None for total count

        Returns:
            Number of jobs matching filter
        """
        pass

    @abstractmethod
    def update_status(
        self,
        job_id: str,
        status: str,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> bool:
        """
        Update job status and optional fields

        Args:
            job_id: Job ID
            status: New status (use JobStatus constants)
            started_at: Timestamp when job started processing
            completed_at: Timestamp when job completed/failed
            result: Job result data (for completed jobs)
            error: Error message (for failed jobs)

        Returns:
            True if updated, False if job not found
        """
        pass

    @abstractmethod
    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel pending job

        Can only cancel jobs with status=pending.

        Args:
            job_id: Job ID

        Returns:
            True if cancelled, False if job not found or not cancellable
        """
        pass

    @abstractmethod
    def cleanup_old_jobs(self, retention_period: int) -> int:
        """
        Remove old completed/failed/cancelled jobs

        Args:
            retention_period: Keep jobs newer than this many seconds

        Returns:
            Number of jobs deleted
        """
        pass

    @abstractmethod
    def get_queue_depth(self) -> int:
        """
        Get number of pending jobs in queue

        Returns:
            Count of jobs with status=pending
        """
        pass

    @abstractmethod
    def get_stats(self) -> Dict[str, Any]:
        """
        Get queue statistics

        Returns:
            Dictionary with stats:
            - total_jobs: Total number of jobs
            - pending: Pending jobs
            - processing: Currently processing jobs
            - completed: Completed jobs
            - failed: Failed jobs
            - cancelled: Cancelled jobs
            - average_execution_time: Average time in seconds (completed jobs only)
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        Check if queue backend is healthy and accessible

        Returns:
            True if healthy, False otherwise
        """
        pass

    @staticmethod
    def generate_job_id() -> str:
        """Generate unique job ID"""
        return str(uuid.uuid4())

    @staticmethod
    def validate_status(status: str) -> bool:
        """Validate status value"""
        return status in JobStatus.all()

    def create_job_dict(
        self,
        job_id: str,
        context: Optional[str],
        hash_filter: Optional[str],
        status: str = JobStatus.PENDING,
        created_at: Optional[datetime] = None,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Create standardized job dictionary

        Returns:
            Job dictionary with all fields
        """
        if created_at is None:
            created_at = datetime.now(timezone.utc)

        return {
            'job_id': job_id,
            'context': context,
            'hash': hash_filter,
            'dry_run': dry_run,
            'status': status,
            'created_at': created_at.isoformat() if isinstance(created_at, datetime) else created_at,
            'started_at': started_at.isoformat() if isinstance(started_at, datetime) else started_at,
            'completed_at': completed_at.isoformat() if isinstance(completed_at, datetime) else completed_at,
            'result': result,
            'error': error
        }


def create_queue(backend: str = 'sqlite', **kwargs) -> QueueManager:
    """
    Factory function to create queue backend

    Args:
        backend: Queue backend type ('sqlite' or 'redis')
        **kwargs: Backend-specific configuration
            For SQLite:
                - db_path: Path to database file (default: /config/qbt-rules.db)
            For Redis:
                - redis_url: Redis connection URL (default: redis://localhost:6379/0)

    Returns:
        QueueManager instance

    Raises:
        ValueError: If backend is unknown or dependencies missing

    Examples:
        >>> queue = create_queue('sqlite', db_path='/data/queue.db')
        >>> queue = create_queue('redis', redis_url='redis://localhost:6379/0')
    """
    if backend == 'sqlite':
        from qbt_rules.queue_backends.sqlite_queue import SQLiteQueue
        db_path = kwargs.get('db_path', '/config/qbt-rules.db')
        return SQLiteQueue(db_path=db_path)

    elif backend == 'redis':
        try:
            from qbt_rules.queue_backends.redis_queue import RedisQueue
        except ImportError:
            raise ValueError(
                "Redis backend requires 'redis' package. "
                "Install with: pip install qbt-rules[redis]"
            )
        redis_url = kwargs.get('redis_url', 'redis://localhost:6379/0')
        return RedisQueue(redis_url=redis_url)

    else:
        raise ValueError(f"Unknown queue backend: {backend}. Use 'sqlite' or 'redis'")
