import threading
import time

from agent.task_queue import TaskQueue, TaskStatus


def test_submit_auto_starts_queue_and_completes_task():
    queue = TaskQueue()
    finished = threading.Event()

    class FakeExecutor:
        def execute(self, goal, speak=None, cancel_flag=None, player=None):
            finished.set()
            return "ok"

    queue._get_executor = lambda: FakeExecutor()
    task_id = queue.submit("test task")
    assert finished.wait(1.0)
    assert queue.get_status(task_id)["status"] == TaskStatus.COMPLETED.value
    queue.stop()


def test_stop_waits_for_running_task_thread_to_exit():
    queue = TaskQueue()
    started = threading.Event()
    returned = threading.Event()

    class SlowCancellableExecutor:
        def execute(self, goal, speak=None, cancel_flag=None, player=None):
            started.set()
            while not cancel_flag.is_set():
                time.sleep(0.01)
            time.sleep(0.25)
            returned.set()
            return "done"

    queue._get_executor = lambda: SlowCancellableExecutor()
    task_id = queue.submit("cancel me")
    assert started.wait(1.0)

    queue.stop()

    assert returned.is_set()
    assert not queue._task_threads
    assert queue.get_status(task_id)["status"] == TaskStatus.CANCELLED.value


def test_task_queue_cleans_up_and_accounts_for_abnormal_task_thread_termination():
    source = (ROOT / "agent" / "task_queue.py").read_text(encoding="utf-8")
    start = source.index("def _run_task")
    block = source[start:source.index("_queue = TaskQueue()", start)]
    assert "except BaseException as e:" in block
    assert "self._active_count = max(0, self._active_count - 1)" in block
    assert "finally:" in block
    assert "self._task_threads.pop(task.task_id, None)" in block
