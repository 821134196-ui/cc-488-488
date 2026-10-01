"""进程内调度：定时执行迟到释放与场次提醒。"""
from __future__ import annotations

import threading
import time as _time


class Scheduler:
    def __init__(self, svc, interval: float = 20.0) -> None:
        self.svc = svc
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="house-scheduler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.svc.tick()
            except Exception as exc:  # 调度异常不应杀死线程
                self.svc.notify("调度异常", str(exc))

    def run_once_now(self) -> None:
        self.svc.tick()

    def stop(self) -> None:
        self._stop.set()
