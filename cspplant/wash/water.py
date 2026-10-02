"""一车水能洗几面、什么时候回站。

每面镜子的用水不是同一面值：毛用水 = 喷淋流量 × 喷淋时长，再按脏度档上
浮；喷淋回水按固定比例回收到车载水箱，因此真正消耗水箱的是净用水
``gross × (1 - recovery)``。排程按净用水给每一批做预算，车上保留一股
回站安全余量（宁可少洗一面也不把水用光）。

回站判定只看保守水位（见 :meth:`cspplant.wash.truck.WashTruck.effective_water_m3`）：
水位回读缺失或过期属于部分失败，此时保守水位就是名义账本下限值，语义上
等同"水不足"，会提前触发回站而不是继续赌满箱。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import WashLimits
from .dirtiness import BAND_WATER_FACTOR, band_of

# 喷淋回水可回收比例（与 wash.cycle 保持同一口径）。
RECOVERY_RATIO = 0.85

# 水箱保留的回站安全余量占额定容量的比例。
RESERVE_RATIO = 0.10


@dataclass(frozen=True)
class WaterQuote:
    """一面镜子的用水量估值。"""

    mirror: str
    band: str
    gross_m3: float
    recovered_m3: float
    net_m3: float

    def as_dict(self) -> dict:
        return {
            "mirror": self.mirror,
            "band": self.band,
            "gross_m3": round(self.gross_m3, 4),
            "recovered_m3": round(self.recovered_m3, 4),
            "net_m3": round(self.net_m3, 4),
        }


class WaterBudget:
    """按清洗参数与脏度档做单车水量预算。"""

    def __init__(
        self,
        limits: WashLimits,
        recovery_ratio: float = RECOVERY_RATIO,
        reserve_ratio: float = RESERVE_RATIO,
    ) -> None:
        self._limits = limits
        self._recovery = min(1.0, max(0.0, float(recovery_ratio)))
        self._reserve = min(0.9, max(0.0, float(reserve_ratio)))

    @property
    def base_gross_m3(self) -> float:
        """一面 average 档镜子的喷淋毛用水。"""

        return self._limits.return_flow_m3_h * self._limits.wash_duration_s / 3600.0

    @property
    def reserve_m3(self) -> float:
        """回站安全余量（立方米）。"""

        return self._limits.truck_capacity_m3 * self._reserve

    def quote(self, mirror: str, dirtiness: float) -> WaterQuote:
        """估一面镜子的毛用水、回收水与净用水。"""

        band = band_of(dirtiness)
        gross = self.base_gross_m3 * BAND_WATER_FACTOR[band]
        recovered = gross * self._recovery
        return WaterQuote(mirror, band, gross, recovered, gross - recovered)

    def usable_water_m3(self, water_m3: float) -> float:
        """扣除回站安全余量后，本批真正允许花掉的水量。"""

        return max(0.0, float(water_m3) - self.reserve_m3)

    def pack(self, quotes: list[WaterQuote], water_m3: float) -> tuple[int, list[WaterQuote]]:
        """按给定次序贪心装入镜子，返回能安全洗完的面数与对应估值。

        次序由排程固定（脏度优先、带内连片），这里只决定"装到第几面为止"，
        绝不调换镜子顺序。任何一面放不下就截断本批。
        """

        budget = self.usable_water_m3(water_m3)
        picked: list[WaterQuote] = []
        spent = 0.0
        for item in quotes:
            if spent + item.net_m3 > budget and picked:
                break
            picked.append(item)
            spent += item.net_m3
            if spent > budget:
                # 单面就超预算也要允许它进批，由调用方在批前回站补满；
                # 满箱连一面都洗不了属于配置错误，直接交由上层告警。
                break
        return len(picked), picked

    def can_cover(self, quotes: list[WaterQuote], water_m3: float) -> bool:
        """当前保守水位是否够把给定镜子全部洗完并留安全余量。"""

        need = sum(item.net_m3 for item in quotes)
        return need <= self.usable_water_m3(water_m3)

    def expected_washes(self, water_m3: float, dirtiness: float = 0.3) -> int:
        """给定水位大约够洗几面（用于控制台预估）。"""

        net = self.quote("*", dirtiness).net_m3
        if net <= 0.0:
            return 0
        return int(self.usable_water_m3(water_m3) // net)

    def summary(self) -> dict:
        return {
            "base_gross_m3": round(self.base_gross_m3, 4),
            "recovery_ratio": self._recovery,
            "reserve_ratio": self._reserve,
            "reserve_m3": round(self.reserve_m3, 4),
            "capacity_m3": self._limits.truck_capacity_m3,
        }
