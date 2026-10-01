"""模拟适配器：门锁与付款网关。

真实场景里这两个是外部服务；演示环境全部进程内模拟，
结果可通过注入失败率来制造 FAILED（测试退款幂等时使用）。
"""
from __future__ import annotations

import random
import threading


class DoorLockAdapter:
    """电子门锁：桌台开场解锁、散场落锁。记录最后动作，便于页面展示。"""

    def __init__(self) -> None:
        self._state: dict[int, str] = {}
        self._log: list[str] = []
        self._lock = threading.Lock()

    def unlock(self, table_id: int) -> dict:
        with self._lock:
            self._state[table_id] = "OPEN"
            msg = f"门锁适配器：桌台 {table_id} 已解锁"
            self._log.append(msg)
            return {"ok": True, "message": msg}

    def lock(self, table_id: int) -> dict:
        with self._lock:
            self._state[table_id] = "LOCKED"
            msg = f"门锁适配器：桌台 {table_id} 已落锁"
            self._log.append(msg)
            return {"ok": True, "message": msg}

    def state(self, table_id: int) -> str:
        return self._state.get(table_id, "LOCKED")

    def log_tail(self, n: int = 20) -> list[str]:
        with self._lock:
            return list(self._log[-n:])


class PaymentAdapter:
    """模拟付款网关：押金退款 / 赔付收款。

    用内存集合记录已受理请求，网关侧同样幂等：
    同一幂等键重复请求直接返回首次结果，绝不重复出账。
    """

    def __init__(self, fail_rate: float = 0.0) -> None:
        self.fail_rate = fail_rate
        self._processed: dict[str, dict] = {}  # 只留存成功结果
        self.attempts: dict[str, int] = {}     # 每个幂等键的请求次数
        self._lock = threading.Lock()

    def request(self, idem_key: str, amount: float) -> dict:
        with self._lock:
            self.attempts[idem_key] = self.attempts.get(idem_key, 0) + 1
            if idem_key in self._processed:
                # 成功结果永久幂等：重放首次结果，杜绝重复出账
                first = self._processed[idem_key]
                return {**first, "idempotent_replay": True}
            ok = self.fail_rate <= 0 or random.random() >= self.fail_rate
            result = {
                "ok": ok,
                "status": "SUCCESS" if ok else "FAILED",
                "amount": amount,
                "gateway_txn": f"GW-{idem_key}-{len(self._processed) + 1:04d}",
                "idempotent_replay": False,
            }
            if ok:
                # FAILED 不落表：网络/失败允许用同一幂等键重试
                self._processed[idem_key] = result
            return result

    def successful_keys(self) -> list[str]:
        with self._lock:
            return list(self._processed)
