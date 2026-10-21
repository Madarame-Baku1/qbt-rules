"""
Redis Queue Backend

In-memory queue implementation using Redis with:
- High-performance operations
- Connection pooling
- Automatic retry logic
- Optional persistence (depends on Redis configuration)
"""

import json
import logging
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone

try:
    import redis
except ImportError:
    raise ImportError(
        "Redis backend requires 'redis' package. "
        "Install with: pip install qbt-rules[redis]"
    )

from qbt_rules.queue_manager import QueueManager, JobStatus

logger = logging.getLogger(__name__)


class RedisQueue(QueueManager):
    """
    Redis-based queue implementation

    Uses Redis data structures:
    - LIST: Pending job queue (FIFO)
    - HASH: Job data storage
    - SET: Job indexes by status and context
    - ZSET: Time-sorted jobs for cleanup

    Key patterns:
    - qbt_rules:queue:pending - Pending job IDs (LIST)
    - qbt_rules:jobs:{id} - Job data (HASH)
    - qbt_rules:jobs:status:{status} - Jobs by status (SET)
    - qbt_rules:jobs:context:{context} - Jobs by context (SET)
    - qbt_rules:jobs:by_time - Jobs sorted by created_at (ZSET)
    """

    KEY_PREFIX = "qbt_rules"

    def __init__(self, redis_url: str = 'redis://localhost:6379/0'):
        """
        Initialize Redis queue

        Args:
            redis_url: Redis connection URL
                      Format: redis://[:password@]host[:port][/database]
        """
        self.redis_url = redis_url

        # Initialize Redis connection pool
        try:
            self.pool = redis.ConnectionPool.from_url(
                redis_url,
                decode_responses=True,  # Auto-decode bytes to strings
                max_connections=10,
                socket_timeout=5,
                socket_connect_timeout=5
            )
            self.redis = redis.Redis(connection_pool=self.pool)

            # Test connection
            self.redis.ping()

            logger.info(f"Redis queue initialized: {redis_url}")

        except redis.ConnectionError as e:
            raise ConnectionError(f"Cannot connect to Redis at {redis_url}: {e}")
        except Exception as e:
            raise RuntimeError(f"Redis initialization failed: {e}")

    def _key(self, *parts: str) -> str:
        """Build Redis key with prefix"""
        return ':'.join([self.KEY_PREFIX] + list(parts))

    def enqueue(self, context: Optional[str] = None, hash_filter: Optional[str] = None,
                dry_run: bool = False) -> str:
        """Add job to queue"""
        job_id = self.generate_job_id()
        created_at = datetime.now(timezone.utc)
        timestamp = created_at.timestamp()

        # Create job data
        job_data = {
            'id': job_id,
            'context': context or '',
            'hash': hash_filter or '',
            'dry_run': '1' if dry_run else '0',
            'status': JobStatus.PENDING,
            'created_at': created_at.isoformat(),
            'started_at': '',
            'completed_at': '',
            'result': '',
            'error': ''
        }

        pipeline = self.redis.pipeline()

        # Store job data
        job_key = self._key('jobs', job_id)
        pipeline.hset(job_key, mapping=job_data)

        # Add to pending queue (FIFO)
        pipeline.rpush(self._key('queue', 'pending'), job_id)

        # Add to status index
        pipeline.sadd(self._key('jobs', 'status', JobStatus.PENDING), job_id)

        # Add to context index (if context provided)
        if context:
            pipeline.sadd(self._key('jobs', 'context', context), job_id)

        # Add to time-sorted set for cleanup
        pipeline.zadd(self._key('jobs', 'by_time'), {job_id: timestamp})

        pipeline.execute()

        logger.debug(f"Enqueued job {job_id} (context={context}, hash={hash_filter})")
        return job_id

    def dequeue(self) -> Optional[Dict[str, Any]]:
        """Get next pending job and mark as processing"""
        # Atomic pop from pending queue
        job_id = self.redis.lpop(self._key('queue', 'pending'))

        if not job_id:
            return None

        started_at = datetime.now(timezone.utc)

        pipeline = self.redis.pipeline()

        # Update job status
        job_key = self._key('jobs', job_id)
        pipeline.hset(job_key, 'status', JobStatus.PROCESSING)
        pipeline.hset(job_key, 'started_at', started_at.isoformat())

        # Update status indexes
        pipeline.srem(self._key('jobs', 'status', JobStatus.PENDING), job_id)
        pipeline.sadd(self._key('jobs', 'status', JobStatus.PROCESSING), job_id)

        # Get full job data
        pipeline.hgetall(job_key)

        results = pipeline.execute()
        job_data = results[-1]  # Last result is hgetall

        if not job_data:
            return None

        return self._hash_to_dict(job_data)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job by ID"""
        job_key = self._key('jobs', job_id)
        job_data = self.redis.hgetall(job_key)

        if not job_data:
            return None

        return self._hash_to_dict(job_data)

    def list_jobs(
        self,
        status: Optional[str] = None,
        context: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """List jobs with filtering"""
        limit = min(limit, 100)

        # Get job IDs from appropriate index
        if status and context:
            # Intersection of status and context sets
            job_ids = list(self.redis.sinter(
                self._key('jobs', 'status', status),
                self._key('jobs', 'context', context)
            ))
        elif status:
            job_ids = list(self.redis.smembers(self._key('jobs', 'status', status)))
        elif context:
            job_ids = list(self.redis.smembers(self._key('jobs', 'context', context)))
        else:
            # Get all jobs from time-sorted set
            job_ids = self.redis.zrevrange(self._key('jobs', 'by_time'), 0, -1)

        # Sort by created_at (newest first) using time-sorted set scores
        if job_ids and not (status or context):
            # Already sorted by time from zrevrange
            pass
        else:
            # Need to sort manually
            job_ids = sorted(
                job_ids,
                key=lambda jid: float(self.redis.zscore(self._key('jobs', 'by_time'), jid) or 0),
                reverse=True
            )

        # Apply pagination
        paginated_ids = job_ids[offset:offset + limit]

        # Fetch job data
        jobs = []
        for job_id in paginated_ids:
            job = self.get_job(job_id)
            if job:
                jobs.append(job)

        return jobs

    def count_jobs(self, status: Optional[str] = None) -> int:
        """Count jobs by status"""
        if status:
            return self.redis.scard(self._key('jobs', 'status', status))
        else:
            # Total jobs from time-sorted set
            return self.redis.zcard(self._key('jobs', 'by_time'))

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

        job = self.get_job(job_id)
        if not job:
            return False

        old_status = job['status']
        job_key = self._key('jobs', job_id)

        pipeline = self.redis.pipeline()

        # Update job fields
        pipeline.hset(job_key, 'status', status)

        if started_at:
            pipeline.hset(job_key, 'started_at', started_at.isoformat())

        if completed_at:
            pipeline.hset(job_key, 'completed_at', completed_at.isoformat())

        if result is not None:
            pipeline.hset(job_key, 'result', json.dumps(result))

        if error is not None:
            pipeline.hset(job_key, 'error', error)

        # Update status indexes
        if old_status != status:
            pipeline.srem(self._key('jobs', 'status', old_status), job_id)
            pipeline.sadd(self._key('jobs', 'status', status), job_id)

        pipeline.execute()

        return True

    def cancel_job(self, job_id: str) -> bool:
        """Cancel pending job"""
        job = self.get_job(job_id)

        if not job or job['status'] != JobStatus.PENDING:
            return False

        pipeline = self.redis.pipeline()

        # Update status
        job_key = self._key('jobs', job_id)
        pipeline.hset(job_key, 'status', JobStatus.CANCELLED)

        # Remove from pending queue (need to scan and remove)
        # Note: This is O(N) operation, but pending queue should be small
        pending_key = self._key('queue', 'pending')
        pipeline.lrem(pending_key, 0, job_id)

        # Update status indexes
        pipeline.srem(self._key('jobs', 'status', JobStatus.PENDING), job_id)
        pipeline.sadd(self._key('jobs', 'status', JobStatus.CANCELLED), job_id)

        pipeline.execute()

        logger.debug(f"Cancelled job {job_id}")
        return True

    def cleanup_old_jobs(self, retention_period: int) -> int:
        """Remove old completed/failed/cancelled jobs"""
        cutoff_date = datetime.now(timezone.utc) - timedelta(seconds=retention_period)
        cutoff_timestamp = cutoff_date.timestamp()

        # Get old job IDs from time-sorted set
        old_job_ids = self.redis.zrangebyscore(
            self._key('jobs', 'by_time'),
            '-inf',
            cutoff_timestamp
        )

        if not old_job_ids:
            return 0

        deleted = 0
        cleanup_statuses = [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]

        for job_id in old_job_ids:
            job = self.get_job(job_id)

            if not job or job['status'] not in cleanup_statuses:
                continue

            # Delete job
            pipeline = self.redis.pipeline()

            # Remove job data
            pipeline.delete(self._key('jobs', job_id))

            # Remove from indexes
            pipeline.zrem(self._key('jobs', 'by_time'), job_id)
            pipeline.srem(self._key('jobs', 'status', job['status']), job_id)

            if job.get('context'):
                pipeline.srem(self._key('jobs', 'context', job['context']), job_id)

            pipeline.execute()
            deleted += 1

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old jobs older than {cutoff_date}")

        return deleted

    def get_queue_depth(self) -> int:
        """Get number of pending jobs"""
        return self.redis.llen(self._key('queue', 'pending'))

    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics"""
        stats = {
            'total_jobs': self.count_jobs(),
            'pending': self.count_jobs(JobStatus.PENDING),
            'processing': self.count_jobs(JobStatus.PROCESSING),
            'completed': self.count_jobs(JobStatus.COMPLETED),
            'failed': self.count_jobs(JobStatus.FAILED),
            'cancelled': self.count_jobs(JobStatus.CANCELLED),
        }

        # Average execution time for completed jobs
        completed_jobs = self.list_jobs(status=JobStatus.COMPLETED, limit=100)
        execution_times = []

        for job in completed_jobs:
            if job.get('started_at') and job.get('completed_at'):
                try:
                    started = datetime.fromisoformat(job['started_at'])
                    completed = datetime.fromisoformat(job['completed_at'])
                    duration = (completed - started).total_seconds()
                    execution_times.append(duration)
                except (ValueError, TypeError):
                    continue

        if execution_times:
            avg_time = sum(execution_times) / len(execution_times)
            stats['average_execution_time'] = round(avg_time, 2)
        else:
            stats['average_execution_time'] = None

        return stats

    def health_check(self) -> bool:
        """Check if Redis is accessible"""
        try:
            self.redis.ping()
            return True
        except Exception as e:
            logger.error(f"Queue health check failed: {e}")
            return False

    def _hash_to_dict(self, hash_data: Dict[str, str]) -> Dict[str, Any]:
        """
        Convert Redis hash to job dictionary

        Args:
            hash_data: Redis HASH data

        Returns:
            Job dictionary with parsed JSON fields
        """
        job = {
            'job_id': hash_data.get('id', ''),
            'context': hash_data.get('context') or None,
            'hash': hash_data.get('hash') or None,
            # Absent on jobs queued before per-job dry-run existed
            'dry_run': hash_data.get('dry_run') == '1',
            'status': hash_data.get('status', ''),
            'created_at': hash_data.get('created_at', ''),
            'started_at': hash_data.get('started_at') or None,
            'completed_at': hash_data.get('completed_at') or None,
            'result': json.loads(hash_data['result']) if hash_data.get('result') else None,
            'error': hash_data.get('error') or None
        }

        return job

    def close(self):
        """Close Redis connection pool"""
        if hasattr(self, 'pool') and self.pool is not None:
            self.pool.disconnect()

    def __del__(self):
        """Cleanup on deletion"""
        self.close()
