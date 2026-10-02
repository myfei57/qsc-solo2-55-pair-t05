"""清洗排程与批次规划。

排序规则**定死**为：

1. 主键：脏度档自脏到净（heavy → clean），同档内按脏度值降序；
2. 次序键（仅在同档内生效）：以上一面已选镜子为起点走最近邻，把同一片
   区域的镜子连片，减少空驶；
3. 兜底：镜名。

区域连片永远不能跨越脏度带——不允许为了少跑路把一面 average 镜排到
heavy 镜前面。初次排程与任何重排都走同一套排序，所以"重排有时序不能
乱"在算法层就是：已完成的镜子不动、批内已洗前缀不动，只有未开始的尾部
参与重排，且重排后仍按脏度优先出镜子。

批次边界由水量预算决定（一面净用水累加，留回站安全余量），并受
``max_batch`` 的硬性批上限约束（一批镜子对应一个避让区，不能无限大）。
水不够时缩批还是回站补水，按车况（剩余水位、回读是否部分失败、剩余尾批
大小）查 :class:`ShortagePolicy` 的决策表。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

from ..config import WashLimits
from .dirtiness import DIRT_BANDS, DirtinessLedger, band_rank
from .water import WaterBudget, WaterQuote

PLANNING = "planning"
WASHING_BATCH = "washing"
RETURN_FOR_REFILL = "returning"
IDLE = "idle"

# 车况决策动作。
CONTINUE = "continue"
TRIM_BATCH = "trim_batch"
REFILL_AND_RESUME = "refill_and_resume"
REFILL_BEFORE_BATCH = "refill_before_batch"
NEXT_BATCH = "next_batch"
DONE = "done"

Coords = dict[str, tuple[float, float]]


@dataclass(frozen=True)
class TruckCondition:
    """决策"缩批还是回站"用的车况快照。"""

    water_m3: float
    readback_degraded: bool
    remaining_mirrors: int
    next_batch_size: int

    @classmethod
    def from_snapshot(
        cls, water_m3: float, degraded: bool, remaining: int, next_batch: int
    ) -> "TruckCondition":
        return cls(float(water_m3), bool(degraded), int(remaining), int(next_batch))


@dataclass
class ShortagePolicy:
    """水量不够时按车况出确定性决策。

    批前：保守水位（已扣安全余量）不够覆盖本批，或回读处于部分失败，一律
    回站补满（``refill_before_batch``）——回读缺失按不足处理。

    批中在下一面之前（当前批还剩 ``tail`` 面、战役还剩 ``campaign_left``
    面未洗，均不含正在判定的这一面）：

    * 回读部分失败 → 立即回站补水后重排（``refill_and_resume``），绝不带
      未知水位继续；
    * 水能覆盖批内剩余 → 继续（``continue``）；
    * 按当前水能装出非空前缀，且战役剩下的镜数已不足一个满批（零头）→
      缩减本批洗完即止（``trim_batch``），不为零星几面空跑；
    * 其余情况（前缀太短、后面还有整批活）→ 回站补水后重排。
    """

    min_trim_fraction: float = 0.5

    def decide_before_batch(
        self, budget: WaterBudget, batch: list[WaterQuote], condition: TruckCondition
    ) -> str:
        """批前决策：水够且回读健康就开洗，否则回站补满。"""

        if not batch:
            return DONE
        if condition.readback_degraded:
            return REFILL_BEFORE_BATCH
        if budget.can_cover(batch, condition.water_m3):
            return CONTINUE
        return REFILL_BEFORE_BATCH

    def decide_mid_batch(
        self, budget: WaterBudget, tail: list[WaterQuote], condition: TruckCondition
    ) -> str:
        """批中在下一面之前的决策（只动执行游标，不换镜子次序）。

        ``condition.remaining_mirrors`` 是当前批之后战役还剩多少镜未排
        （不含当前批）；``condition.next_batch_size`` 是满批上限，用来判断
        剩下的活够不够再凑一整车。
        """

        if not tail:
            return DONE
        if condition.readback_degraded:
            return REFILL_AND_RESUME
        if budget.can_cover(tail, condition.water_m3):
            return CONTINUE
        count, _picked = budget.pack(tail, condition.water_m3)
        if count == 0:
            return REFILL_AND_RESUME
        # 当前水能洗完本批一个够长的前缀；后面的镜若已不足一整车则缩批。
        fraction = count / max(1, len(tail))
        campaign_left = condition.remaining_mirrors + len(tail) - count
        if fraction >= self.min_trim_fraction and campaign_left < condition.next_batch_size:
            return TRIM_BATCH
        return REFILL_AND_RESUME


@dataclass
class Batch:
    """一车水对应的一个清洗批次。"""

    seq: int
    mirrors: list[str]
    quotes: list[WaterQuote]
    net_water_m3: float
    state: str = "planned"

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "mirrors": list(self.mirrors),
            "size": len(self.mirrors),
            "bands": [item.band for item in self.quotes],
            "water": {
                "gross_m3": round(sum(item.gross_m3 for item in self.quotes), 4),
                "recovered_m3": round(sum(item.recovered_m3 for item in self.quotes), 4),
                "net_m3": round(self.net_water_m3, 4),
            },
            "state": self.state,
        }


@dataclass
class Schedule:
    """一份清洗排程：有序批次 + 规划参数。"""

    campaign_id: str
    batches: list[Batch] = field(default_factory=list)
    version: int = 0
    reason: str = "planned"

    def ordered_mirrors(self) -> list[str]:
        """跨批的完整清洗次序。"""

        return [mirror for batch in self.batches for mirror in batch.mirrors]

    def as_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "version": self.version,
            "reason": self.reason,
            "batch_count": len(self.batches),
            "mirror_count": len(self.ordered_mirrors()),
            "batches": [batch.as_dict() for batch in self.batches],
        }


class WashScheduler:
    """脏度优先、水量封批、可安全重排的清洗排程器。"""

    def __init__(
        self,
        limits: WashLimits,
        dirtiness: DirtinessLedger | None = None,
        budget: WaterBudget | None = None,
        policy: ShortagePolicy | None = None,
        max_batch: int = 12,
    ) -> None:
        self._limits = limits
        self.dirt = dirtiness or DirtinessLedger()
        self.budget = budget or WaterBudget(limits)
        self.policy = policy or ShortagePolicy()
        self.max_batch = max(1, int(max_batch))
        self._lock = threading.RLock()
        self._schedule: Schedule | None = None
        self._campaign_mirrors: list[str] = []
        self._batch_index = -1
        self._done_in_batch: list[str] = []
        self._washed: list[str] = []
        self._phase = IDLE

    @property
    def phase(self) -> str:
        return self._phase

    # ---- 排序（定死的规则） -------------------------------------------

    def rank(self, mirrors: list[str], coords: Coords | None = None) -> list[str]:
        """脏度降序为主键、同档内最近邻连片、镜名兜底。

        ``coords`` 给出镜名到 (x, y) 的坐标；缺坐标按原点处理（不影响脏度
        主键，只影响同档内的连片次序）。
        """

        coords = coords or {}
        ordered = list(dict.fromkeys(str(name) for name in mirrors))
        scored = sorted(
            ordered,
            key=lambda name: (band_rank(self.dirt.band(name)), -self.dirt.get(name), name),
        )
        bands: dict[str, list[str]] = {band: [] for band in DIRT_BANDS}
        for name in scored:
            bands[self.dirt.band(name)].append(name)

        result: list[str] = []
        current = (0.0, 0.0)
        for band in DIRT_BANDS:
            pending = list(bands[band])
            while pending:
                nearest = min(
                    pending,
                    key=lambda name: (self._distance(current, coords.get(name, (0.0, 0.0))), name),
                )
                pending.remove(nearest)
                result.append(nearest)
                current = coords.get(nearest, current)
        return result

    @staticmethod
    def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    # ---- 批次切分 -----------------------------------------------------

    def _build_batches(
        self, ranked: list[str], first_water_m3: float
    ) -> list[Batch]:
        """按"水量预算 + 批上限"把已排好序的镜子封成多批。"""

        batches: list[Batch] = []
        remaining = list(ranked)
        seq = 0
        water = float(first_water_m3)
        while remaining:
            seq += 1
            candidates = [
                self.budget.quote(name, self.dirt.get(name)) for name in remaining[: self.max_batch]
            ]
            count, picked = self.budget.pack(candidates, water)
            chosen = picked if picked else candidates[:1]
            names = [item.mirror for item in chosen]
            batches.append(
                Batch(
                    seq=seq,
                    mirrors=names,
                    quotes=list(chosen),
                    net_water_m3=sum(item.net_m3 for item in chosen),
                )
            )
            remaining = remaining[len(names) :]
            water = self._limits.truck_capacity_m3
        return batches

    # ---- 排程 ---------------------------------------------------------

    def plan(
        self,
        campaign_id: str,
        mirrors: list[str],
        water_m3: float,
        coords: Coords | None = None,
        *,
        reason: str = "planned",
    ) -> Schedule:
        """按脏度排序后，用一车水预算与批上限切成多批。"""

        with self._lock:
            ranked = self.rank(mirrors, coords)
            batches = self._build_batches(ranked, water_m3)
            self._schedule = Schedule(campaign_id=campaign_id, batches=batches, version=1, reason=reason)
            self._campaign_mirrors = list(ranked)
            self._batch_index = -1
            self._done_in_batch = []
            self._washed = []
            self._phase = PLANNING
            return self._schedule

    def schedule(self) -> Schedule | None:
        return self._schedule

    def require_schedule(self) -> Schedule:
        if self._schedule is None:
            raise ValueError("还没有清洗排程，先 plan()")
        return self._schedule

    def batches(self) -> list[Batch]:
        return list(self.require_schedule().batches)

    def current_batch(self) -> Batch | None:
        if self._schedule is None or not 0 <= self._batch_index < len(self._schedule.batches):
            return None
        return self._schedule.batches[self._batch_index]

    def pending_mirrors(self) -> list[str]:
        """战役中尚未完成的镜子。

        以战役全量名单为准（不依赖当前批次表），这样缩批／重排把尾镜从
        批次里摘掉时，它们仍然留在待洗池里参与下一次重排。
        """

        done = set(self._washed)
        return [name for name in self._campaign_mirrors if name not in done]

    # ---- 执行推进 -----------------------------------------------------

    def open_campaign(self) -> Batch:
        """进入首批。"""

        with self._lock:
            schedule = self.require_schedule()
            self._batch_index = 0
            self._done_in_batch = []
            self._phase = WASHING_BATCH
            schedule.batches[0].state = "active"
            return schedule.batches[0]

    def begin_next_batch(self) -> Batch | None:
        """进入下一批（当前批先置为完成）；全部完成返回 None。"""

        with self._lock:
            schedule = self.require_schedule()
            if 0 <= self._batch_index < len(schedule.batches):
                schedule.batches[self._batch_index].state = "done"
            self._batch_index += 1
            if self._batch_index >= len(schedule.batches):
                self._phase = IDLE
                return None
            nxt = schedule.batches[self._batch_index]
            nxt.state = "active"
            self._done_in_batch = []
            self._phase = WASHING_BATCH
            return nxt

    def before_batch(self, water_m3: float, readback_degraded: bool) -> str:
        """批前决策：水够且回读健康就开洗，否则回站。"""

        with self._lock:
            batch = self.current_batch()
            if batch is None:
                return DONE
            condition = TruckCondition.from_snapshot(
                water_m3, readback_degraded, len(batch.mirrors), len(batch.mirrors)
            )
            verdict = self.policy.decide_before_batch(self.budget, list(batch.quotes), condition)
            if verdict == REFILL_BEFORE_BATCH:
                self._phase = RETURN_FOR_REFILL
            return verdict

    def before_next_mirror(
        self, water_m3: float, readback_degraded: bool, campaign_left: int
    ) -> str:
        """批中决策：下一面之前是继续、缩批还是回站。

        ``campaign_left`` 是当前批之后战役中尚未排进当前批的镜数（不含
        当前批里还没洗的镜）。
        """

        with self._lock:
            batch = self.current_batch()
            if batch is None:
                return DONE
            tail_names = [name for name in batch.mirrors if name not in self._done_in_batch]
            tail = [self.budget.quote(name, self.dirt.get(name)) for name in tail_names]
            condition = TruckCondition.from_snapshot(
                water_m3, readback_degraded, campaign_left, self.max_batch
            )
            verdict = self.policy.decide_mid_batch(self.budget, tail, condition)
            if verdict == REFILL_AND_RESUME:
                self._phase = RETURN_FOR_REFILL
            return verdict

    def mark_washed(self, mirror: str) -> None:
        """登记一面已洗镜子（脏度回写最净档），不在这里做水位决策。"""

        with self._lock:
            batch = self.current_batch()
            if batch is None or mirror not in batch.mirrors:
                raise ValueError(f"镜子不在当前批次中: {mirror}")
            if mirror not in self._done_in_batch:
                self._done_in_batch.append(mirror)
                self._washed.append(mirror)
            self.dirt.mark_washed(mirror)

    def batch_state_after_mirror(self, water_m3: float, readback_degraded: bool) -> str:
        """洗完一面后，由当前位置与水位给出批次走向。

        返回 ``continue``（本批还有下一面）、``next_batch``（本批完成且水够
        续下一批）、``refill_before_batch``（本批完成但下一批前要回站）、
        ``done``（战役结束）。
        """

        with self._lock:
            batch = self.current_batch()
            if batch is None:
                return DONE
            if len(self._done_in_batch) < len(batch.mirrors):
                return CONTINUE
            batch.state = "done"
            nxt_index = self._batch_index + 1
            schedule = self.require_schedule()
            if nxt_index >= len(schedule.batches):
                self._phase = IDLE
                return DONE
            upcoming = schedule.batches[nxt_index]
            condition = TruckCondition.from_snapshot(
                water_m3, readback_degraded, len(upcoming.mirrors), len(upcoming.mirrors)
            )
            verdict = self.policy.decide_before_batch(
                self.budget, list(upcoming.quotes), condition
            )
            if verdict == CONTINUE:
                return NEXT_BATCH
            self._phase = RETURN_FOR_REFILL
            return REFILL_BEFORE_BATCH

    def trim_current_batch(self) -> list[str]:
        """摘掉当前批未洗部分并返回摘掉的镜名（缩批／回站前调用）。"""

        with self._lock:
            batch = self.current_batch()
            if batch is None:
                return []
            done = set(self._done_in_batch)
            trimmed = [name for name in batch.mirrors if name not in done]
            batch.mirrors = [name for name in batch.mirrors if name in done]
            batch.quotes = [item for item in batch.quotes if item.mirror in done]
            batch.net_water_m3 = sum(item.net_m3 for item in batch.quotes)
            batch.state = "trimmed" if trimmed else "done"
            return trimmed

    # ---- 重排 ---------------------------------------------------------

    def replan(
        self,
        water_m3: float,
        coords: Coords | None = None,
        *,
        reason: str = "reschedule",
        dirt_updates: dict[str, float] | None = None,
    ) -> Schedule:
        """只对战役中未开始的尾部重排；已完成镜子不参与，时序不乱。

        待洗池取自战役全量名单减去已完成镜（与当前批次表解耦），所以缩批
        摘掉的尾镜、回站时搁置的镜子都会重新入池。重排版本号加一，排序
        规则与初次排程完全一致（脏度优先、同档连片）。
        """

        with self._lock:
            schedule = self.require_schedule()
            if dirt_updates:
                self.dirt.update(dirt_updates)
            pending = self.pending_mirrors()
            ranked = self.rank(pending, coords)
            batches = self._build_batches(ranked, water_m3)
            for index, batch in enumerate(batches, start=1):
                batch.seq = index
            if batches:
                batches[0].state = "active"
            new_schedule = Schedule(
                campaign_id=schedule.campaign_id,
                batches=batches,
                version=schedule.version + 1,
                reason=reason,
            )
            self._schedule = new_schedule
            washed_ordered = [name for name in self._campaign_mirrors if name in set(self._washed)]
            self._campaign_mirrors = washed_ordered + ranked
            self._batch_index = 0 if batches else -1
            self._done_in_batch = []
            self._phase = WASHING_BATCH if batches else IDLE
            return new_schedule

    def advance_to_working_batch(self) -> Batch | None:
        """重排后把游标放到第一个未完成批。"""

        with self._lock:
            schedule = self.require_schedule()
            for index, batch in enumerate(schedule.batches):
                if batch.state != "done":
                    batch.state = "active"
                    self._batch_index = index
                    self._done_in_batch = []
                    self._phase = WASHING_BATCH
                    return batch
            self._batch_index = len(schedule.batches)
            self._phase = IDLE
            return None

    # ---- 查询与持久化 -------------------------------------------------

    def washed_mirrors(self) -> list[str]:
        return list(self._washed)

    def campaign_total(self) -> int:
        """本次战役的镜面总数（含已洗，不随批次表变化）。"""

        return len(self._campaign_mirrors)

    def progress(self) -> dict:
        """排程执行进度（控制台与审计共用）。"""

        with self._lock:
            schedule = self._schedule
            total = len(self._campaign_mirrors)
            done = len(self._washed)
            current = self.current_batch()
            return {
                "campaign_id": None if schedule is None else schedule.campaign_id,
                "version": 0 if schedule is None else schedule.version,
                "phase": self._phase,
                "total_mirrors": total,
                "washed_mirrors": done,
                "remaining_mirrors": total - done,
                "current_batch": None if current is None else current.seq,
                "batch_count": 0 if schedule is None else len(schedule.batches),
                "batches_done": sum(1 for batch in schedule.batches if batch.state == "done")
                if schedule
                else 0,
            }

    def restore_state(self, washed: list[str], batch_index: int, phase: str) -> None:
        """跨重启恢复执行游标（排程批次本体由下一次重排按台账重建）。"""

        with self._lock:
            self._washed = list(washed)
            self._done_in_batch = list(washed)
            self._campaign_mirrors = list(washed)
            self._batch_index = int(batch_index)
            self._phase = phase
