import asyncio
import threading
import time

from . import db


class RunState:
    def __init__(self, run_id: int):
        self.run_id = run_id
        self.cancel = threading.Event()
        self.events = []
        self.subscribers = []
        self.thread = None


class JobManager:
    def __init__(self):
        self.runs = {}
        self.lock = threading.Lock()
        self.loop = None

    def set_loop(self, loop):
        self.loop = loop

    def start(self, run_id: int, target) -> RunState:
        state = RunState(run_id)
        with self.lock:
            self.runs[run_id] = state
        thread = threading.Thread(target=self._wrap, args=(state, target), daemon=True)
        state.thread = thread
        thread.start()
        return state

    def _wrap(self, state: RunState, target):
        try:
            target(state)
        except Exception as exc:
            db.finish_run(state.run_id, "failed", error=str(exc))
            self.emit(state.run_id, f"FATAL: {exc}")
        finally:
            self.emit(state.run_id, "__done__", type_="done")

    def emit(self, run_id: int, message: str, stage: str = None, type_: str = "log"):
        with self.lock:
            state = self.runs.get(run_id)
        if state is None:
            return
        event = {"type": type_, "message": message, "stage": stage, "ts": time.time()}
        state.events.append(event)
        if self.loop is not None:
            for queue in list(state.subscribers):
                try:
                    self.loop.call_soon_threadsafe(queue.put_nowait, event)
                except Exception:
                    pass

    def subscribe(self, run_id: int) -> asyncio.Queue:
        queue = asyncio.Queue()
        with self.lock:
            state = self.runs.get(run_id)
            if state is not None:
                state.subscribers.append(queue)
        return queue

    def unsubscribe(self, run_id: int, queue: asyncio.Queue):
        with self.lock:
            state = self.runs.get(run_id)
            if state is not None and queue in state.subscribers:
                state.subscribers.remove(queue)

    def backlog(self, run_id: int) -> list:
        with self.lock:
            state = self.runs.get(run_id)
            return list(state.events) if state else []

    def cancel(self, run_id: int) -> bool:
        with self.lock:
            state = self.runs.get(run_id)
        if state is None:
            return False
        state.cancel.set()
        return True


job_manager = JobManager()
