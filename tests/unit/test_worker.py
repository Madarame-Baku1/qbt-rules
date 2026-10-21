"""
Comprehensive tests for worker.py - Background job processor

Test coverage for:
- Worker initialization and configuration
- Thread lifecycle (start, stop, is_alive)
- Job processing loop
- Job execution (success and failure)
- Status reporting
- Error handling and recovery
- Graceful shutdown
"""

import pytest
import time
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

from qbt_rules.worker import Worker
from qbt_rules.queue_manager import JobStatus


@pytest.fixture
def mock_queue(mocker):
    """Create mock queue manager"""
    queue = mocker.MagicMock()
    queue.dequeue.return_value = None
    queue.get_queue_depth.return_value = 0
    queue.update_status.return_value = True
    return queue


@pytest.fixture
def mock_api(mocker):
    """Create mock qBittorrent API"""
    api = mocker.MagicMock()
    return api


@pytest.fixture
def mock_config(mocker):
    """Create mock config"""
    config = mocker.MagicMock()
    config.is_dry_run.return_value = False
    return config


@pytest.fixture
def mock_engine(mocker):
    """Create mock RulesEngine"""
    engine = mocker.MagicMock()

    # Mock stats
    stats = mocker.MagicMock()
    stats.total_torrents = 10
    stats.processed = 8
    stats.rules_matched = 5
    stats.actions_executed = 3
    stats.actions_skipped = 2
    stats.errors = 0
    stats.by_rule = {}

    engine.stats = stats
    engine.run.return_value = None

    return engine


@pytest.fixture
def worker(mock_queue, mock_api, mock_config):
    """Create worker instance (not started)"""
    return Worker(
        queue=mock_queue,
        api=mock_api,
        config=mock_config,
        poll_interval=0.01  # Short interval for testing
    )


class TestWorkerInitialization:
    """Test Worker initialization"""

    def test_init_stores_queue(self, mock_queue, mock_api, mock_config):
        """Should store queue reference"""
        worker = Worker(mock_queue, mock_api, mock_config)

        assert worker.queue is mock_queue

    def test_init_stores_api(self, mock_queue, mock_api, mock_config):
        """Should store API reference"""
        worker = Worker(mock_queue, mock_api, mock_config)

        assert worker.api is mock_api

    def test_init_stores_config(self, mock_queue, mock_api, mock_config):
        """Should store config reference"""
        worker = Worker(mock_queue, mock_api, mock_config)

        assert worker.config is mock_config

    def test_init_sets_poll_interval(self, mock_queue, mock_api, mock_config):
        """Should set poll interval"""
        worker = Worker(mock_queue, mock_api, mock_config, poll_interval=2.5)

        assert worker.poll_interval == 2.5

    def test_init_default_poll_interval(self, mock_queue, mock_api, mock_config):
        """Should use default poll interval of 1.0"""
        worker = Worker(mock_queue, mock_api, mock_config)

        assert worker.poll_interval == 1.0

    def test_init_not_running(self, mock_queue, mock_api, mock_config):
        """Should not be running after init"""
        worker = Worker(mock_queue, mock_api, mock_config)

        assert worker.running is False

    def test_init_no_thread(self, mock_queue, mock_api, mock_config):
        """Should have no thread after init"""
        worker = Worker(mock_queue, mock_api, mock_config)

        assert worker.thread is None

    def test_init_no_last_job_completed(self, mock_queue, mock_api, mock_config):
        """Should have no last_job_completed after init"""
        worker = Worker(mock_queue, mock_api, mock_config)

        assert worker.last_job_completed is None


class TestWorkerStart:
    """Test worker.start() method"""

    def test_start_sets_running_flag(self, worker):
        """Should set running flag to True"""
        worker.start()

        assert worker.running is True

        # Cleanup
        worker.stop()

    def test_start_creates_thread(self, worker):
        """Should create worker thread"""
        worker.start()

        assert worker.thread is not None
        assert isinstance(worker.thread, threading.Thread)

        # Cleanup
        worker.stop()

    def test_start_thread_is_alive(self, worker):
        """Should start thread that is alive"""
        worker.start()

        assert worker.thread.is_alive()

        # Cleanup
        worker.stop()

    def test_start_thread_name(self, worker):
        """Should name thread 'worker'"""
        worker.start()

        assert worker.thread.name == 'worker'

        # Cleanup
        worker.stop()

    def test_start_thread_daemon_false(self, worker):
        """Should create non-daemon thread"""
        worker.start()

        assert worker.thread.daemon is False

        # Cleanup
        worker.stop()

    def test_start_already_running_does_nothing(self, worker):
        """Should not start second thread if already running"""
        worker.start()
        first_thread = worker.thread

        worker.start()  # Try to start again

        assert worker.thread is first_thread  # Same thread

        # Cleanup
        worker.stop()

    def test_start_after_stop_creates_new_thread(self, worker):
        """Should create new thread after stop"""
        worker.start()
        first_thread = worker.thread
        worker.stop()

        worker.start()

        assert worker.thread is not first_thread
        assert worker.thread.is_alive()

        # Cleanup
        worker.stop()


class TestWorkerStop:
    """Test worker.stop() method"""

    def test_stop_sets_running_false(self, worker):
        """Should set running flag to False"""
        worker.start()

        worker.stop()

        assert worker.running is False

    def test_stop_waits_for_thread(self, worker):
        """Should wait for thread to finish"""
        worker.start()

        worker.stop(timeout=5.0)

        assert not worker.thread.is_alive()

    def test_stop_without_start_does_nothing(self, worker):
        """Should handle stop without start gracefully"""
        worker.stop()

        # Should not raise exception
        assert worker.running is False

    def test_stop_with_timeout(self, worker, mock_queue):
        """Should respect timeout parameter"""
        # Make dequeue block for a bit to test timeout
        mock_queue.dequeue.side_effect = lambda: time.sleep(0.1) or None

        worker.start()
        time.sleep(0.05)  # Let it start

        start_time = time.time()
        worker.stop(timeout=0.5)
        elapsed = time.time() - start_time

        # Should wait approximately timeout duration
        assert elapsed < 1.0  # Not hanging forever

    def test_stop_timeout_logs_warning(self, worker, mock_queue, caplog):
        """Should log warning when worker doesn't stop within timeout"""
        import logging
        # Make worker hang by blocking indefinitely
        import threading
        block_event = threading.Event()

        def blocking_dequeue():
            block_event.wait(timeout=5)  # Block for up to 5 seconds
            return None

        mock_queue.dequeue.side_effect = blocking_dequeue

        worker.start()
        time.sleep(0.05)  # Let worker start and enter dequeue

        # Try to stop with very short timeout (worker won't stop in time)
        with caplog.at_level(logging.WARNING):
            worker.stop(timeout=0.01)

        # Verify warning was logged
        assert any("Worker did not stop within" in record.message for record in caplog.records)
        assert any("0.01s timeout" in record.message for record in caplog.records)

        # Clean up: unblock the worker so test can end
        block_event.set()
        time.sleep(0.1)

    def test_stop_already_stopped_does_nothing(self, worker):
        """Should handle double stop gracefully"""
        worker.start()
        worker.stop()

        worker.stop()  # Stop again

        # Should not raise exception
        assert worker.running is False


class TestWorkerIsAlive:
    """Test worker.is_alive() method"""

    def test_is_alive_before_start(self, worker):
        """Should return False before start"""
        assert worker.is_alive() is False

    def test_is_alive_after_start(self, worker):
        """Should return True after start"""
        worker.start()

        assert worker.is_alive() is True

        # Cleanup
        worker.stop()

    def test_is_alive_after_stop(self, worker):
        """Should return False after stop"""
        worker.start()
        worker.stop()

        assert worker.is_alive() is False

    def test_is_alive_no_thread(self, worker):
        """Should return False when thread is None"""
        worker.thread = None

        assert worker.is_alive() is False


class TestWorkerGetStatus:
    """Test worker.get_status() method"""

    def test_get_status_returns_dict(self, worker):
        """Should return dictionary"""
        status = worker.get_status()

        assert isinstance(status, dict)

    def test_get_status_includes_running(self, worker):
        """Should include running flag"""
        status = worker.get_status()

        assert 'running' in status
        assert status['running'] is False

    def test_get_status_includes_thread_alive(self, worker):
        """Should include thread_alive flag"""
        status = worker.get_status()

        assert 'thread_alive' in status
        assert status['thread_alive'] is False

    def test_get_status_includes_last_job_completed(self, worker):
        """Should include last_job_completed"""
        status = worker.get_status()

        assert 'last_job_completed' in status
        assert status['last_job_completed'] is None

    def test_get_status_includes_queue_depth(self, worker, mock_queue):
        """Should include queue depth"""
        mock_queue.get_queue_depth.return_value = 5

        status = worker.get_status()

        assert 'queue_depth' in status
        assert status['queue_depth'] == 5

    def test_get_status_running_worker(self, worker):
        """Should show running status for started worker"""
        worker.start()

        status = worker.get_status()

        assert status['running'] is True
        assert status['thread_alive'] is True

        # Cleanup
        worker.stop()

    def test_get_status_with_last_job_completed(self, worker):
        """Should format last_job_completed as ISO string"""
        completed_time = datetime(2025, 1, 1, 12, 0, 0)
        worker.last_job_completed = completed_time

        status = worker.get_status()

        assert status['last_job_completed'] == '2025-01-01T12:00:00'


class TestWorkerRunLoop:
    """Test worker._run_loop() internal method"""

    def test_run_loop_polls_queue(self, worker, mock_queue):
        """Should continuously poll queue for jobs"""
        worker.start()
        time.sleep(0.05)  # Let it poll a few times
        worker.stop()

        # Should have called dequeue multiple times
        assert mock_queue.dequeue.call_count > 1

    def test_run_loop_processes_job_when_available(self, worker, mock_queue, mocker):
        """Should process job when dequeued"""
        job = {
            'job_id': 'test-job-1',
            'context': 'test',
            'hash': None,
            'status': JobStatus.PROCESSING
        }

        # Return job once, then None
        mock_queue.dequeue.side_effect = [job, None]

        # Mock _process_job
        process_spy = mocker.patch.object(worker, '_process_job')

        worker.start()
        time.sleep(0.1)  # Let it process
        worker.stop()

        process_spy.assert_called_once_with(job)

    def test_run_loop_sleeps_when_queue_empty(self, worker, mock_queue, mocker):
        """Should sleep when queue is empty"""
        mock_queue.dequeue.return_value = None

        sleep_spy = mocker.patch('time.sleep')

        worker.start()
        time.sleep(0.05)
        worker.stop()

        # Should have slept with poll_interval
        sleep_calls = [c for c in sleep_spy.call_args_list if c[0][0] == worker.poll_interval]
        assert len(sleep_calls) > 0

    def test_run_loop_handles_dequeue_error(self, worker, mock_queue, mocker):
        """Should handle and log dequeue errors"""
        mock_queue.dequeue.side_effect = Exception("Queue error")

        sleep_spy = mocker.patch('time.sleep')

        worker.start()
        time.sleep(0.1)
        worker.stop()

        # Should still be running and sleeping after error
        sleep_calls = [c for c in sleep_spy.call_args_list if c[0][0] == worker.poll_interval]
        assert len(sleep_calls) > 0

    def test_run_loop_exits_when_running_false(self, worker, mock_queue):
        """Should exit loop when running flag becomes False"""
        mock_queue.dequeue.return_value = None

        worker.start()
        time.sleep(0.05)
        worker.running = False
        time.sleep(0.1)  # Let it exit

        assert not worker.thread.is_alive()


class TestWorkerProcessJob:
    """Test worker._process_job() internal method"""

    def test_process_job_calls_execute_job(self, worker, mocker):
        """Should call _execute_job with context and hash"""
        job = {
            'job_id': 'job-1',
            'context': 'weekly-cleanup',
            'hash': 'abc123'
        }

        execute_spy = mocker.patch.object(
            worker, '_execute_job',
            return_value={'torrents_processed': 5}
        )

        worker._process_job(job)

        execute_spy.assert_called_once_with('weekly-cleanup', 'abc123', False)

    def test_process_job_updates_status_on_success(self, worker, mock_queue, mocker):
        """Should update job status to COMPLETED on success"""
        job = {
            'job_id': 'job-1',
            'context': 'test',
            'hash': None
        }

        result = {'torrents_processed': 5, 'actions_executed': 2}
        mocker.patch.object(worker, '_execute_job', return_value=result)

        worker._process_job(job)

        mock_queue.update_status.assert_called_once()
        call_args = mock_queue.update_status.call_args

        assert call_args[1]['job_id'] == 'job-1'
        assert call_args[1]['status'] == JobStatus.COMPLETED
        assert call_args[1]['result'] == result

    def test_process_job_sets_started_at(self, worker, mock_queue, mocker):
        """Should set started_at timestamp"""
        job = {'job_id': 'job-1', 'context': None, 'hash': None}

        mocker.patch.object(worker, '_execute_job', return_value={})

        before = datetime.now(timezone.utc)
        worker._process_job(job)
        after = datetime.now(timezone.utc)

        call_args = mock_queue.update_status.call_args[1]
        started_at = call_args['started_at']

        assert before <= started_at <= after

    def test_process_job_sets_completed_at(self, worker, mock_queue, mocker):
        """Should set completed_at timestamp"""
        job = {'job_id': 'job-1', 'context': None, 'hash': None}

        mocker.patch.object(worker, '_execute_job', return_value={})

        before = datetime.now(timezone.utc)
        worker._process_job(job)
        after = datetime.now(timezone.utc)

        call_args = mock_queue.update_status.call_args[1]
        completed_at = call_args['completed_at']

        assert before <= completed_at <= after

    def test_process_job_updates_last_job_completed(self, worker, mocker):
        """Should update last_job_completed timestamp"""
        job = {'job_id': 'job-1', 'context': None, 'hash': None}

        mocker.patch.object(worker, '_execute_job', return_value={})

        worker._process_job(job)

        assert worker.last_job_completed is not None
        assert isinstance(worker.last_job_completed, datetime)

    def test_process_job_handles_execution_error(self, worker, mock_queue, mocker):
        """Should handle execution errors and mark job as FAILED"""
        job = {'job_id': 'job-1', 'context': None, 'hash': None}

        mocker.patch.object(
            worker, '_execute_job',
            side_effect=Exception("Execution error")
        )

        worker._process_job(job)

        mock_queue.update_status.assert_called_once()
        call_args = mock_queue.update_status.call_args[1]

        assert call_args['job_id'] == 'job-1'
        assert call_args['status'] == JobStatus.FAILED

    def test_process_job_sets_error_on_failure(self, worker, mock_queue, mocker):
        """Should set error traceback on failure"""
        job = {'job_id': 'job-1', 'context': None, 'hash': None}

        mocker.patch.object(
            worker, '_execute_job',
            side_effect=ValueError("Test error")
        )

        worker._process_job(job)

        call_args = mock_queue.update_status.call_args[1]
        error = call_args['error']

        assert 'ValueError' in error
        assert 'Test error' in error
        assert 'Traceback' in error

    def test_process_job_does_not_update_last_job_on_failure(self, worker, mocker):
        """Should not update last_job_completed on failure"""
        job = {'job_id': 'job-1', 'context': None, 'hash': None}

        mocker.patch.object(
            worker, '_execute_job',
            side_effect=Exception("Error")
        )

        worker._process_job(job)

        assert worker.last_job_completed is None


class TestWorkerExecuteJob:
    """Test worker._execute_job() internal method"""

    def test_execute_job_creates_rules_engine(self, worker, mock_api, mock_config, mocker):
        """Should create RulesEngine instance"""
        engine_spy = mocker.patch('qbt_rules.worker.RulesEngine')
        engine_spy.return_value.stats = mocker.MagicMock(
            total_torrents=0, processed=0, rules_matched=0,
            actions_executed=0, actions_skipped=0, errors=0
        )

        worker._execute_job(context=None, hash_filter=None)

        engine_spy.assert_called_once_with(
            api=mock_api,
            config=mock_config,
            dry_run=False,
            notifications_config=None,
            integrations_config=None
        )

    def test_execute_job_uses_dry_run_from_config(self, worker, mocker):
        """Should get dry_run setting from config"""
        worker.config.is_dry_run.return_value = True

        engine_spy = mocker.patch('qbt_rules.worker.RulesEngine')
        engine_spy.return_value.stats = mocker.MagicMock(
            total_torrents=0, processed=0, rules_matched=0,
            actions_executed=0, actions_skipped=0, errors=0
        )

        worker._execute_job(context=None, hash_filter=None)

        engine_spy.assert_called_once()
        assert engine_spy.call_args[1]['dry_run'] is True

    @pytest.mark.parametrize('config_dry_run, job_dry_run, expected', [
        (False, False, False),
        (False, True, True),    # the job asked for it
        (True, False, True),    # a job can't turn the server's dry-run off
        (True, True, True),
    ])
    def test_execute_job_combines_job_and_config_dry_run(self, worker, mocker,
                                                         config_dry_run, job_dry_run, expected):
        worker.config.is_dry_run.return_value = config_dry_run
        engine_spy = mocker.patch('qbt_rules.worker.RulesEngine')
        engine_spy.return_value.stats = mocker.MagicMock(
            total_torrents=0, processed=0, rules_matched=0,
            actions_executed=0, actions_skipped=0, errors=0
        )

        worker._execute_job(None, None, job_dry_run)

        assert engine_spy.call_args[1]['dry_run'] is expected

    def test_process_job_passes_job_dry_run(self, worker, mocker):
        execute_spy = mocker.patch.object(worker, '_execute_job', return_value={})

        worker._process_job({'job_id': 'job-1', 'context': 'manual', 'hash': None, 'dry_run': True})

        execute_spy.assert_called_once_with('manual', None, True)

    def test_execute_job_calls_engine_run(self, worker, mocker):
        """Should call engine.run with context and hash"""
        engine_mock = mocker.MagicMock()
        engine_mock.stats = mocker.MagicMock(
            total_torrents=10, processed=8, rules_matched=5,
            actions_executed=3, actions_skipped=2, errors=0
        )

        mocker.patch('qbt_rules.worker.RulesEngine', return_value=engine_mock)

        worker._execute_job(context='weekly-cleanup', hash_filter='abc123')

        engine_mock.run.assert_called_once_with(
            context='weekly-cleanup',
            torrent_hash='abc123'
        )

    def test_execute_job_returns_result_dict(self, worker, mocker):
        """Should return result dictionary with statistics"""
        engine_mock = mocker.MagicMock()
        engine_mock.stats = mocker.MagicMock(
            total_torrents=10,
            processed=8,
            rules_matched=5,
            actions_executed=3,
            actions_skipped=2,
            errors=0
        )

        mocker.patch('qbt_rules.worker.RulesEngine', return_value=engine_mock)

        result = worker._execute_job(context=None, hash_filter=None)

        assert result['total_torrents'] == 10
        assert result['torrents_processed'] == 8
        assert result['rules_matched'] == 5
        assert result['actions_executed'] == 3
        assert result['actions_skipped'] == 2
        assert result['errors'] == 0
        assert result['dry_run'] is False

    def test_execute_job_includes_dry_run_in_result(self, worker, mocker):
        """Should include dry_run flag in result"""
        worker.config.is_dry_run.return_value = True

        engine_mock = mocker.MagicMock()
        engine_mock.stats = mocker.MagicMock(
            total_torrents=0, processed=0, rules_matched=0,
            actions_executed=0, actions_skipped=0, errors=0
        )

        mocker.patch('qbt_rules.worker.RulesEngine', return_value=engine_mock)

        result = worker._execute_job(context=None, hash_filter=None)

        assert result['dry_run'] is True

    def test_execute_job_propagates_engine_errors(self, worker, mocker):
        """Should propagate exceptions from engine"""
        engine_mock = mocker.MagicMock()
        engine_mock.run.side_effect = ValueError("Engine error")

        mocker.patch('qbt_rules.worker.RulesEngine', return_value=engine_mock)

        with pytest.raises(ValueError, match="Engine error"):
            worker._execute_job(context=None, hash_filter=None)


class TestWorkerRepr:
    """Test worker.__repr__() method"""

    def test_repr_before_start(self, worker):
        """Should show not running, not alive"""
        repr_str = repr(worker)

        assert 'Worker' in repr_str
        assert 'running=False' in repr_str
        assert 'alive=False' in repr_str

    def test_repr_after_start(self, worker):
        """Should show running and alive"""
        worker.start()

        repr_str = repr(worker)

        assert 'Worker' in repr_str
        assert 'running=True' in repr_str
        assert 'alive=True' in repr_str

        # Cleanup
        worker.stop()

    def test_repr_after_stop(self, worker):
        """Should show not running, not alive"""
        worker.start()
        worker.stop()

        repr_str = repr(worker)

        assert 'running=False' in repr_str
        assert 'alive=False' in repr_str


class TestWorkerIntegration:
    """Integration tests for worker with real threading"""

    def test_worker_processes_multiple_jobs(self, worker, mock_queue, mocker):
        """Should process multiple jobs sequentially"""
        jobs = [
            {'job_id': f'job-{i}', 'context': 'test', 'hash': None}
            for i in range(3)
        ]

        # Return jobs then None
        mock_queue.dequeue.side_effect = jobs + [None]

        mocker.patch.object(
            worker, '_execute_job',
            return_value={'torrents_processed': 1}
        )

        worker.start()
        time.sleep(0.2)  # Let it process all jobs
        worker.stop()

        # Should have updated status for all 3 jobs
        assert mock_queue.update_status.call_count == 3

    def test_worker_continues_after_job_error(self, worker, mock_queue, mocker):
        """Should continue processing after job error"""
        jobs = [
            {'job_id': 'job-1', 'context': None, 'hash': None},
            {'job_id': 'job-2', 'context': None, 'hash': None}
        ]

        # Return jobs then None
        mock_queue.dequeue.side_effect = jobs + [None]

        # First job fails, second succeeds
        mocker.patch.object(
            worker, '_execute_job',
            side_effect=[Exception("Error"), {'torrents_processed': 1}]
        )

        worker.start()
        time.sleep(0.2)
        worker.stop()

        # Should have processed both jobs
        assert mock_queue.update_status.call_count == 2

    def test_worker_graceful_shutdown_waits_for_job(self, worker, mock_queue, mocker):
        """Should wait for current job to complete before shutting down"""
        job = {'job_id': 'job-1', 'context': None, 'hash': None}
        mock_queue.dequeue.side_effect = [job, None]

        # Make job take a bit to process
        def slow_execute(*args, **kwargs):
            time.sleep(0.15)
            return {'torrents_processed': 1}

        mocker.patch.object(worker, '_execute_job', side_effect=slow_execute)

        worker.start()
        time.sleep(0.1)  # Let it start processing

        worker.stop(timeout=5.0)

        # Job should have completed (status updated)
        assert mock_queue.update_status.call_count == 1
        # Should have stopped gracefully
        assert not worker.is_alive()
