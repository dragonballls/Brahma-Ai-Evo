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
