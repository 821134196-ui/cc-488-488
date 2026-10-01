# -*- coding: utf-8 -*-
"""进程内后台任务：迟到释放与开场提醒。守护线程，可注入时钟便于测试。"""
from __future__ import annotations

import logging
import threading

from .catalog import REMIND_BEFORE_MIN
from .services import ChessRoomService

log = logging.getLogger("chessroom.scheduler")


class Scheduler:
    def __init__(self, svc: ChessRoomService, interval: float = 20.0,
                 remind_lead_min: int = REMIND_BEFORE_MIN):
        self.svc = svc
        self.interval = interval
        self.lead = remind_lead_min * 60
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.tick_count = 0

    def tick(self) -> dict:
        released = self.svc.release_late()
        reminded = self.svc.due_reminders(self.lead)
        self.tick_count += 1
        if released:
            log.info("迟到释放场次 %s", released)
        if reminded:
            log.info("开场提醒 %s", [r["id"] for r in reminded])
        return {"released": released, "reminded": [r["id"] for r in reminded]}

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 后台任务不能因单次异常退出
                log.exception("后台任务执行失败")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="room-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
