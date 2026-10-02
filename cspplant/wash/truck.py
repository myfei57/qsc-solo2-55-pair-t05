"""清洗车。

车上带水箱，按喷淋时长消耗水量；水箱见底就拒绝继续喷淋，避免空泵。喷淋进度
按时间累计，只有进度满格才算洗完，避免"车到了就当作洗完"。

车载水位传感器通过 :meth:`report_water` 回读。回读可能缺失或过期，排程只信
"最后一次新鲜回读"；拿不到新鲜回读时按部分失败处理——水位按保守值（车上
自己的名义账本）走，绝不假设水还满。名义账本是下限估计：只记喷淋毛耗水与
回水回收，蒸发／滴漏只会让真实水位更低。
"""

from __future__ import annotations

from ..config import WashLimits
from ..errors import InterlockError

IDLE = "idle"
WASHING = "washing"
RETURNING = "returning"

# 回读超过该时长没有刷新即视为过期（部分失败），排程改按保守水位扣水。
READBACK_STALE_S = 120.0


class WashTruck:
    """镜面清洗车。"""

    def __init__(self, limits: WashLimits, water_m3: float | None = None) -> None:
        self._limits = limits
        self._mirror = ""
        self._state = IDLE
        self._elapsed_s = 0.0
        self._used_m3 = 0.0
        self._returned_m3 = 0.0
        self._water_m3 = float(limits.truck_capacity_m3 if water_m3 is None else water_m3)
        self._washes = 0
        # 水位回读账本：出厂即有一次与名义水位一致的新鲜回读（站内满水出
        # 发），之后只有显式 report_water(None) 才表示回读缺失。
        self._readback_m3: float | None = self._water_m3
        self._readback_ts: float | None = 0.0
        self._readback_available = True
        self._readback_missing_count = 0
        self._refills = 0
        self._refilled_m3 = 0.0

    @property
    def limits(self) -> WashLimits:
        return self._limits

    def arrive(self, mirror: str) -> str:
        """到达某面镜子并复位本镜进度。"""

        if not mirror:
            raise InterlockError("清洗必须指定镜子", component="wash")
        self._mirror = str(mirror)
        self._elapsed_s = 0.0
        self._state = IDLE
        return self._mirror

    def start_wash(self) -> str:
        """开始喷淋；未到位或没水即拒绝。"""

        if not self._mirror:
            raise InterlockError("清洗车尚未到达镜子", component="wash")
        if self._water_m3 <= 0.0:
            raise InterlockError("车载水箱已空，禁止继续喷淋", component="wash")
        self._state = WASHING
        return self._state

    def step(self, dt: float) -> float:
        """推进喷淋一个步长，返回喷淋进度。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        if self._state != WASHING:
            raise InterlockError("清洗车未在喷淋", component="wash")
        consume = self._limits.return_flow_m3_h * dt / 3600.0
        if consume > self._water_m3:
            consume = self._water_m3
        self._water_m3 -= consume
        self._used_m3 += consume
        self._elapsed_s += dt
        if self.progress() >= 1.0:
            self._state = RETURNING
            self._washes += 1
        return self.progress()

    def progress(self) -> float:
        """返回喷淋进度（0–1）。"""

        return min(1.0, self._elapsed_s / self._limits.wash_duration_s)

    def complete(self) -> bool:
        """喷淋是否完成。"""

        return self.progress() >= 1.0

    def state(self) -> str:
        """返回清洗车状态。"""

        return self._state

    def mirror(self) -> str:
        """返回当前作业的镜子名。"""

        return self._mirror

    def water_m3(self) -> float:
        """返回车载水箱名义剩余水量（下限账本）。"""

        return self._water_m3

    def used_m3(self) -> float:
        """返回本次作业累计用水量。"""

        return self._used_m3

    def returned_m3(self) -> float:
        """返回本次作业累计回收水量。"""

        return self._returned_m3

    def record_returned(self, amount_m3: float) -> float:
        """登记回收水量（由回水系统调用）。"""

        self._returned_m3 += max(0.0, float(amount_m3))
        return self._returned_m3

    def washes(self) -> int:
        """返回累计清洗镜面数。"""

        return self._washes

    def refill(self, amount_m3: float) -> float:
        """补水，返回补水后的水量。"""

        added = max(0.0, float(amount_m3))
        before = self._water_m3
        self._water_m3 = min(self._limits.truck_capacity_m3, self._water_m3 + added)
        actual = self._water_m3 - before
        if actual > 0.0:
            self._refills += 1
            self._refilled_m3 += actual
        # 站内补水枪是可信回读源：补水即恢复一次与满箱一致的新鲜回读，
        # 消除"回读缺失"的部分失败状态（时间戳由 refill_to_full 校准）。
        self._readback_m3 = self._water_m3
        self._readback_available = True
        if self._readback_ts is None:
            self._readback_ts = 0.0
        return self._water_m3

    def refill_to_full(self, ts: float | None = None) -> float:
        """回站补满水箱，返回补水后的新水位。"""

        added = self._limits.truck_capacity_m3 - self._water_m3
        self.refill(added)
        if ts is not None:
            self._readback_ts = float(ts)
        return self._water_m3

    def refill_amount(self) -> float:
        """返回累计站内补水量。"""

        return self._refilled_m3

    # ---- 水位回读 -----------------------------------------------------

    def report_water(self, amount_m3: float | None, ts: float) -> bool:
        """登记一次车载水位回读；``None`` 表示本次回读缺失。

        回读缺失不改变名义账本，只累计一次缺失并把保守开关留给
        :meth:`effective_water_m3` 判定；返回是否为新鲜有效回读。
        """

        if amount_m3 is None:
            self._readback_missing_count += 1
            # 缺失不清空最后回读值（保留供审计），只用可用开关标记部分失败。
            self._readback_available = False
            return False
        value = min(self._limits.truck_capacity_m3, max(0.0, float(amount_m3)))
        self._readback_m3 = value
        self._readback_ts = float(ts)
        self._readback_available = True
        return True

    def readback_fresh(self, now: float, stale_after_s: float = READBACK_STALE_S) -> bool:
        """最后一次回读是否存在、未过期且未被声明缺失。"""

        if not self._readback_available or self._readback_m3 is None or self._readback_ts is None:
            return False
        return float(now) - self._readback_ts <= stale_after_s

    def readback_degraded(self, now: float, stale_after_s: float = READBACK_STALE_S) -> bool:
        """水位回读是否处于部分失败（缺失或过期）。"""

        return not self.readback_fresh(now, stale_after_s)

    def effective_water_m3(self, now: float, stale_after_s: float = READBACK_STALE_S) -> float:
        """排程可用的保守水位。

        回读新鲜时取"回读值与名义账本的较小者"（任一侧偏大都不冒险）；
        回读缺失／过期属于部分失败，直接按名义账本这个下限值处理。
        """

        if self.readback_fresh(now, stale_after_s):
            return min(self._water_m3, float(self._readback_m3))
        return self._water_m3

    def water_status(self, now: float, stale_after_s: float = READBACK_STALE_S) -> dict:
        """返回水量回读状态（控制台与审计同一口径）。"""

        degraded = self.readback_degraded(now, stale_after_s)
        return {
            "water_m3": round(self._water_m3, 4),
            "effective_water_m3": round(self.effective_water_m3(now, stale_after_s), 4),
            "readback_m3": None if self._readback_m3 is None else round(self._readback_m3, 4),
            "readback_ts": self._readback_ts,
            "readback_degraded": degraded,
            "readback_missing": self._readback_missing_count,
            "capacity_m3": self._limits.truck_capacity_m3,
            "refills": self._refills,
            "refilled_m3": round(self._refilled_m3, 4),
        }

    def snapshot(self) -> dict:
        return {
            "state": self._state,
            "mirror": self._mirror,
            "progress": round(self.progress(), 3),
            "water_m3": round(self._water_m3, 4),
            "used_m3": round(self._used_m3, 4),
            "washes": self._washes,
        }
