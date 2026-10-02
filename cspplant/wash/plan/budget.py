"""水量预算与"缩减本批 / 回站补水"车况裁决。

口径**写死**，排程、执行与审计共用同一套算式，避免三处各算各的：

- 单面耗水按喷淋流量 × 时长的名义毛额计，回水回收只能让车多洗，绝不允许在
  排程阶段就把回收水当成可用水（回收可能失败，按毛额排才不会把水用干）。
- 可用水 = 回读水量 − 回站保底水量。保底水量是"这批洗完还能开回站"的硬底。
- 水箱另有安全余量，任何动作结束时水量不得跌破它（与保底取大者执行）。
- 水量回读缺失是**部分失败**：不当作崩溃，而是按水量不足处理（可用水记 0），
  因此裁决必然走向回站补水，绝不带着未知水量继续喷淋。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...config import WashLimits
from .models import DECISION_RETURN, DECISION_SHRINK, WATER_MISSING, WATER_OK, WaterReading


@dataclass(frozen=True)
class TruckCondition:
    """车况输入：决定水量不足时是缩减本批还是回站补水。"""

    water_reading_ok: bool = True   # 水量回读是否成功（False 即部分失败）
    water_m3: float | None = None   # 回读水量；回读失败时为 None
    pump_ok: bool = True            # 喷淋泵是否可用
    drive_ok: bool = True           # 行车机构是否可用
    near_station: bool = False      # 是否已在站附近（补水代价低）
    ts: float = 0.0

    def as_reading(self, limits: WashLimits) -> WaterReading:
        """归一化成水量回读；回读缺失按不足处理（水量记 0）。"""

        if not self.water_reading_ok or self.water_m3 is None:
            return WaterReading(0.0, WATER_MISSING, truck_ok=self.pump_ok and self.drive_ok, ts=self.ts)
        value = min(float(limits.truck_capacity_m3), max(0.0, float(self.water_m3)))
        return WaterReading(value, WATER_OK, truck_ok=self.pump_ok and self.drive_ok, ts=self.ts)


def per_mirror_m3(limits: WashLimits) -> float:
    """单面镜子名义毛耗水。"""

    return limits.water_per_mirror_m3()


def usable_water_m3(reading: WaterReading, limits: WashLimits) -> float:
    """可用于本批清洗的水量 = 回读水量 − 回站保底。

    回读缺失（部分失败）按水量不足处理：可用水直接记 0。
    """

    if reading.missing:
        return 0.0
    return max(0.0, reading.water_m3 - limits.return_reserve_m3)


def need_for(per_mirror: float, count: int) -> float:
    """洗 ``count`` 面镜子的名义毛耗水。"""

    return per_mirror * max(0, int(count))


def packable_count(usable_m3: float, per_mirror: float) -> int:
    """给定可用水，最多装得下几面镜子（仍守住保底，不把水用干）。"""

    if per_mirror <= 0.0:
        return 0
    return int(usable_m3 // per_mirror + 1e-9)


@dataclass(frozen=True)
class ShortageDecision:
    """水量不足时的车况裁决结果。"""

    action: str
    reason: str
    remaining_m3: int          # 缩减后本批保留几面（回站补水时为 0）
    defer_m3: int              # 推迟到补水后重排的镜面数
    reading_status: str

    @property
    def returns_to_station(self) -> bool:
        return self.action == DECISION_RETURN

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "remaining_m3": self.remaining_m3,
            "defer_m3": self.defer_m3,
            "reading_status": self.reading_status,
        }


def decide_shortage(
    reading: WaterReading,
    pending_count: int,
    per_mirror: float,
    limits: WashLimits,
    *,
    near_station: bool = False,
) -> ShortageDecision:
    """按车况决定水量不足时"缩减本批"还是"回站补水后重排"。

    允许缩减本批必须**同时**满足：

    1. 水量回读成功（部分失败时禁止就地缩减，因为水量未知）；
    2. 喷淋泵可用（坏了洗不了，只能回站）；
    3. 按回读水量确实还洗得动至少一面且守住回站保底；
    4. 洗得动的是当前序列的一个**非空前缀**（时序不能乱，不能跳着洗）。

    否则一律回站补水后重排。已在站附近时回站代价低，也直接回站补水。
    """

    total = max(0, int(pending_count))

    if not reading.truck_ok:
        return ShortageDecision(DECISION_RETURN, "truck_fault", 0, total, reading.status)
    if reading.missing:
        return ShortageDecision(DECISION_RETURN, "water_readback_missing", 0, total, WATER_MISSING)
    if near_station:
        return ShortageDecision(DECISION_RETURN, "near_station_refill", 0, total, reading.status)

    fit = packable_count(usable_water_m3(reading, limits), per_mirror)
    fit = min(fit, total)
    if fit <= 0:
        return ShortageDecision(DECISION_RETURN, "insufficient_for_even_one", 0, total, reading.status)
    return ShortageDecision(DECISION_SHRINK, "shrink_to_available_water", fit, total - fit, WATER_OK)
