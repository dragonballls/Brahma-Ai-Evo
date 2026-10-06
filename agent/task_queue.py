import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any


class TaskStatus(Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class TaskPriority(Enum):
    LOW    = 3
    NORMAL = 2
    HIGH   = 1   


@dataclass(order=True)
class Task:
    priority:    int                       
    created_at:  float = field(compare=False)
    task_id:     str   = field(compare=False)
    goal:        str   = field(compare=False)
    status:      TaskStatus = field(compare=False, default=TaskStatus.PENDING)
    result:      Any        = field(compare=False, default=None)
    error:       str        = field(compare=False, default="")
    speak:       Any        = field(compare=False, default=None)   
    on_complete: Any        = field(compare=False, default=None)  
    cancel_flag: threading.Event = field(compare=False, default_factory=threading.Event)
    player:      Any        = field(compare=False, default=None)


class TaskQueue:
    def __init__(self, max_concurrent: int = 1):
        self._queue:        list[Task]       = []
        self._lock:         threading.Lock   = threading.Lock()
        self._condition:    threading.Condition = threading.Condition(self._lock)
        self._tasks:        dict[str, Task]  = {} 
        self._running:      bool             = False
        self._worker_thread: threading.Thread | None = None
        self._max_concurrent = max_concurrent
        self._active_count   = 0
        self._executor       = None
        self._executor_lock   = threading.Lock()
        self._history_limit   = 1000
        self._task_threads: dict[str, threading.Thread] = {}
        self._stop_timeout = 5.0

    def _get_executor(self):
        executor = self._executor
        if executor is not None:
            return executor
        with self._executor_lock:
            if self._executor is None:
                from agent.executor import AgentExecutor
                self._executor = AgentExecutor()
            return self._executor

    def start(self) -> None:
        with self._condition:
            if self._running:
                return
            self._running = True
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="AgentTaskQueue"
            )
            self._worker_thread.start()
        print("[TaskQueue] ✅ Started")

    def stop(self) -> None:
        deadline = time.monotonic() + self._stop_timeout
        with self._condition:
            self._running = False
            for task in self._tasks.values():
                if task.status == TaskStatus.PENDING:
                    task.cancel_flag.set()
                    task.status = TaskStatus.CANCELLED
            self._queue.clear()
            for task in self._tasks.values():
                if task.status == TaskStatus.RUNNING:
                    task.cancel_flag.set()
            self._condition.notify_all()
        thread = self._worker_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._condition:
            task_threads = list(self._task_threads.values())
        for task_thread in task_threads:
            if task_thread is threading.current_thread():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            task_thread.join(timeout=remaining)
        with self._condition:
            if self._worker_thread is thread and (thread is None or not thread.is_alive()):
                self._worker_thread = None
        print("[TaskQueue] 🔴 Stopped")

    def is_running(self) -> bool:
        with self._condition:
            return bool(self._running and self._worker_thread is not None and self._worker_thread.is_alive())

    def submit(
        self,
        goal:        str,
        priority:    TaskPriority = TaskPriority.NORMAL,
        speak:       Callable | None = None,
        on_complete: Callable | None = None,
        player:      Any = None,
    ) -> str:

        self.start()
        task_id = str(uuid.uuid4())[:8]
        task    = Task(
            priority    = priority.value,
            created_at  = time.time(),
            task_id     = task_id,
            goal        = goal,
            speak       = speak,
            on_complete = on_complete,
            player      = player,
        )

        with self._condition:
            terminal_ids = [
                task_id for task_id, existing in self._tasks.items()
                if existing.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
            ]
            while len(self._tasks) >= self._history_limit and terminal_ids:
                old_id = terminal_ids.pop(0)
                self._tasks.pop(old_id, None)
            self._queue[:] = [
                queued for queued in self._queue
                if queued.status == TaskStatus.PENDING and not queued.cancel_flag.is_set()
            ]
            self._queue.append(task)
            self._queue.sort(key=lambda t: (t.priority, t.created_at))
            self._tasks[task_id] = task
            self._condition.notify()

        print(f"[TaskQueue] 📥 Task queued: [{task_id}] {goal[:60]}")
        return task_id

    def cancel(self, task_id: str) -> bool:

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                return False

            task.cancel_flag.set()
            if task.status == TaskStatus.PENDING:
                try:
                    self._queue.remove(task)
                except ValueError:
                    pass
            task.status = TaskStatus.CANCELLED
            print(f"[TaskQueue] 🚫 Task cancelled: [{task_id}]")
            return True

    def get_status(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            return {
                "task_id": task.task_id,
                "goal":    task.goal,
                "status":  task.status.value,
                "result":  task.result,
                "error":   task.error,
            }

    def get_all_statuses(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "task_id": t.task_id,
                    "goal":    t.goal[:50],
                    "status":  t.status.value,
                }
                for t in self._tasks.values()
            ]

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._queue if t.status == TaskStatus.PENDING)

    def _worker_loop(self) -> None:
        while self._running:
            task = None

            with self._condition:
                while self._running and not self._next_task():
                    self._condition.wait(timeout=1.0)
                task = self._next_task()
                if task:
                    task.status = TaskStatus.RUNNING
                    self._active_count += 1
                    try:
                        self._queue.remove(task)
                    except ValueError:
                        pass

            if task:
                task_thread = threading.Thread(
                    target=self._run_task,
                    args=(task,),
                    daemon=True,
                    name=f"AgentTask-{task.task_id}"
                )
                self._task_threads[task.task_id] = task_thread
                task_thread.start()

    def _next_task(self) -> Task | None:
        if self._active_count >= self._max_concurrent:
            return None
        for task in self._queue:
            if task.status == TaskStatus.PENDING and not task.cancel_flag.is_set():
                return task
        return None

    def _run_task(self, task: Task) -> None:
        print(f"[TaskQueue] ▶️ Running: [{task.task_id}] {task.goal[:60]}")
        try:
            executor = self._get_executor()
            result   = executor.execute(
                goal        = task.goal,
                speak       = task.speak,
                cancel_flag = task.cancel_flag,
                player      = task.player,
            )

            with self._lock:
                if task.cancel_flag.is_set():
                    task.status = TaskStatus.CANCELLED
                else:
                    task.status = TaskStatus.COMPLETED
                    task.result = result
                self._active_count -= 1

            if task.on_complete and not task.cancel_flag.is_set():
                try:
                    task.on_complete(task.task_id, result)
                except Exception as e:
                    print(f"[TaskQueue] ⚠️ on_complete callback error: {e}")

            print(f"[TaskQueue] ✅ Completed: [{task.task_id}]")

        except Exception as e:
            with self._lock:
                if task.cancel_flag.is_set():
                    task.status = TaskStatus.CANCELLED
                    task.error = ""
                else:
                    task.status = TaskStatus.FAILED
                    task.error  = str(e)
                self._active_count -= 1
            if task.cancel_flag.is_set():
                print(f"[TaskQueue] 🚫 Cancelled: [{task.task_id}]")
            else:
                print(f"[TaskQueue] ❌ Failed: [{task.task_id}] {e}")

        with self._condition:
            self._task_threads.pop(task.task_id, None)
            self._condition.notify()

_queue = TaskQueue()
_queue_lock = threading.Lock()


def get_queue() -> TaskQueue:
    with _queue_lock:
        _queue.start()
    return _queue