"""清洗排程器。

排程器是排程的唯一事实来源（single source of truth）：脏度台账、当前版排程、
水量回读与车况都进这里，控制台展示、落盘持久化、审计快照读同一份数据。

不可变的三条铁律：

1. **脏度优先**：未洗镜子的顺序由 grade_first 全序决定，区域连片只在同档内裁决，
   绝不跨档把脏镜子挤到后面。
2. **不把水用干**：每批按"名义毛耗水"装箱并预留回站保底水量；回读缺失是部分
   失败，按水量不足处理，直接回站补水，绝不带未知水量继续洗。
3. **重排不乱时序**：已洗完的镜子与正在洗的镜子冻结，重排只动未洗部分；任务在
   队列里的先后位置就是作业时序，回站补水任务永远排在它后面那批清洗之前。

车有两个互斥模式：``washing``（当前批可继续用本车的水）与 ``returning``（必须先
回站补水，未洗序列队首是补水任务）。

持久事实（跨重排版本稳定）只有四类：已关闭历史批次 ``_finished_batches``、当前
批已完成前缀 ``_done_in_current``、当前批号 ``_current_batch_no``、已完成补水
``_refills_done``。每次重排都从这些事实**确定性地重建**整版任务与批次视图。
"""

from __future__ import annotations

import threading

from ...config import WashLimits
from ...errors import InterlockError
from .budget import (
    TruckCondition,
    decide_shortage,
    need_for,
    packable_count,
    per_mirror_m3,
    usable_water_m3,
)
from .dirt import DirtLedger
from .models import (
    BATCH_ACTIVE,
    BATCH_DONE,
    BATCH_PLANNED,
    Batch,
    RefillTask,
    TASK_ACTIVE,
    TASK_DONE,
    TASK_PENDING,
    TASK_REFILL,
    TASK_WASH,
    WATER_MISSING,
    WashPlan,
    WashTask,
    WaterReading,
)

REASON_INITIAL = "initial_plan"
REASON_WATER = "water_reading"
REASON_DIRT = "dirt_updated"
REASON_MIRROR_START = "mirror_started"
REASON_BATCH_DONE = "batch_completed"
REASON_SHORTAGE = "batch_shortage"
REASON_REFILLED = "refilled"
REASON_TRUCK = "truck_condition"

MODE_WASHING = "washing"
MODE_RETURNING = "returning"


class WashScheduler:
    """按脏度与车况排出清洗顺序、批次与回站补水点。"""

    def __init__(self, limits: WashLimits, *, full_tank_m3: float | None = None) -> None:
        if limits.batch_policy != "grade_first":  # 装配已拦，这里再守一道
            raise InterlockError("批次策略必须是 grade_first", component="wash")
        self._limits = limits
        self._dirt = DirtLedger(limits)
        self._full_m3 = float(limits.truck_capacity_m3 if full_tank_m3 is None else full_tank_m3)
        self._plan = WashPlan(version=0)
        self._reading: WaterReading | None = None
        self._condition = TruckCondition()
        self._mode = MODE_WASHING
        # ---- 跨版本持久事实 ---------------------------------------------
        self._batch_seq = 0                          # 已分配过的最大稳定批号
        self._current_batch_no: int | None = None    # 当前批稳定号；批关闭后置空
        self._finished_batches: list[tuple[int, list[str]]] = []
        self._done_in_current: list[str] = []        # 当前批已完成前缀（按完成顺序）
        self._active_mirror: str | None = None       # 正在洗的镜子（独立持久事实）
        self._refills_done: list[str] = []
        self._lock = threading.RLock()
        self._reschedules: list[dict] = []

    @property
    def limits(self) -> WashLimits:
        return self._limits

    @property
    def dirt(self) -> DirtLedger:
        return self._dirt

    @property
    def per_mirror_m3(self) -> float:
        return per_mirror_m3(self._limits)

    @property
    def mode(self) -> str:
        return self._mode

    # ---- 脏度输入 -----------------------------------------------------

    def _current_water(self) -> float:
        """重排时采用的记账水量：最近回读水量；缺失（部分失败）按 0。"""

        if self._reading is None:
            return self._full_m3
        return 0.0 if self._reading.missing else self._reading.water_m3

    def set_dirt(
        self, name: str, grade: int, group: str = "a", x_m: float = 0.0, y_m: float = 0.0
    ) -> dict:
        """登记/更新一面镜子的脏度；若已有排程则只重排未洗部分。"""

        with self._lock:
            entry = self._dirt.set(name, grade, group, x_m, y_m)
            if self._has_plan:
                self._reschedule(REASON_DIRT, self._current_water())
            return entry.as_dict()

    def set_dirt_bulk(self, rows: list[dict]) -> list[dict]:
        """批量登记脏度，登记完统一重排一次。"""

        with self._lock:
            out = [
                self._dirt.set(
                    str(row["name"]),
                    int(row["grade"]),
                    str(row.get("group", "a")),
                    float(row.get("x_m", 0.0)),
                    float(row.get("y_m", 0.0)),
                ).as_dict()
                for row in rows
            ]
            if self._has_plan:
                self._reschedule(REASON_DIRT, self._current_water())
            return out

    @property
    def _has_plan(self) -> bool:
        return self._plan.version > 0

    def _done_names(self) -> set[str]:
        names = set(self._done_in_current)
        for _no, members in self._finished_batches:
            names.update(members)
        return names

    def _pending_order(self, frozen_mirror: str | None) -> list[str]:
        """未洗序列 = grade_first 全序，去掉已完成与在洗镜子。"""

        done = self._done_names()
        return [
            entry.name
            for entry in self._dirt.order()
            if entry.name != frozen_mirror and entry.name not in done
        ]

    def _task(self, name: str, batch_id: str, state: str) -> WashTask:
        entry = self._dirt.get(name)
        return WashTask(name, entry.grade, entry.group, self.per_mirror_m3, batch_id, state)

    # ---- 重建整版排程 ---------------------------------------------------

    def _rebuild(self, reason: str, water_m3: float, *, commit_next: bool = False) -> WashPlan:
        """从持久事实确定性地重建任务与批次视图。

        ``commit_next``：补水完成时为真，把队首未来批提交为新的当前批（推进稳定
        批号并占用它），其前的补水点同时落账。
        """

        per = self.per_mirror_m3
        frozen = self._active_mirror
        ordered = self._pending_order(frozen)

        batches: list[Batch] = []
        tasks: list[WashTask | RefillTask] = []
        version = self._plan.version + 1
        done_refills = set(self._refills_done)

        def emit(bid: str, seq: list[str], leaving: float, state: str,
                 states: list[str]) -> None:
            entries = [self._dirt.get(name) for name in seq]
            batches.append(
                Batch(
                    batch_id=bid,
                    mirrors=tuple(seq),
                    grades=tuple(e.grade for e in entries),
                    groups=tuple(e.group for e in entries),
                    water_need_m3=need_for(per, len(seq)),
                    water_leaving_m3=max(0.0, leaving),
                    state=state,
                )
            )
            for name, tstate in zip(seq, states):
                tasks.append(self._task(name, bid, tstate))

        # 1) 已关闭历史批（批 k 前若有已完成补水 R(k-1)，先排它）。
        for no, members in self._finished_batches:
            leading = f"R{no - 1:03d}"
            if no > 1 and leading in done_refills:
                tasks.append(RefillTask(task_id=leading, state=TASK_DONE))
            emit(f"B{no:03d}", members, 0.0, BATCH_DONE, [TASK_DONE] * len(members))

        rest = ordered
        current_open = self._current_batch_no is not None

        # 2) 当前批（打开着）：已完成前缀 + 在洗面 + 本车实测水还洗得动的成员。
        if current_open:
            no = self._current_batch_no
            bid = f"B{no:03d}"
            seq = list(self._done_in_current)
            states = [TASK_DONE] * len(seq)
            if frozen is not None:
                seq.append(frozen)
                states.append(TASK_ACTIVE)
            budget = usable_water_m3(WaterReading(max(0.0, water_m3), "ok", True, 0.0), self._limits)
            remaining = budget - (per if frozen is not None else 0.0)
            kept: list[str] = []
            if self._mode == MODE_WASHING:
                for name in rest:
                    if remaining + 1e-9 < per:
                        break
                    kept.append(name)
                    remaining -= per
            seq.extend(kept)
            states.extend([TASK_PENDING] * len(kept))
            rest = rest[len(kept):]
            is_active = frozen is not None or bool(self._done_in_current)
            emit(bid, seq, water_m3 - need_for(per, len(seq)),
                 BATCH_ACTIVE if is_active else BATCH_PLANNED, states)

        # 3) 未来满水箱批：按满水箱装箱，批号与"引入该批的补水号 R(k-1)"都取稳定
        #    号。补水完成（commit_next）时，队首未来批提交为新的当前批，推进稳定
        #    批号；其余仍是 PLANNED 未来批（不推进序号）。
        full_usable = max(0.0, self._full_m3 - self._limits.return_reserve_m3)
        cap_full = max(1, packable_count(full_usable, per))
        future_no = self._batch_seq
        future_index = 0
        while rest:
            future_no += 1
            seq = rest[:cap_full]
            bid_no = future_no
            is_committed = commit_next and future_index == 0 and not current_open
            if is_committed:
                self._batch_seq = bid_no
                self._current_batch_no = bid_no
                current_open = True
            bid = f"B{bid_no:03d}"
            refill_state = TASK_DONE if is_committed else TASK_PENDING
            tasks.append(RefillTask(task_id=f"R{bid_no - 1:03d}", state=refill_state))
            emit(bid, seq, self._full_m3 - need_for(per, len(seq)),
                 BATCH_ACTIVE if is_committed else BATCH_PLANNED,
                 [TASK_PENDING] * len(seq))
            future_index += 1
            rest = rest[cap_full:]

        return WashPlan(
            version=version,
            batches=batches,
            tasks=tasks,
            reason=reason,
            water_m3=max(0.0, water_m3),
            usable_m3=usable_water_m3(WaterReading(max(0.0, water_m3), "ok", True, 0.0), self._limits)
            if self._mode == MODE_WASHING and current_open
            else 0.0,
        )

    # ---- 建排程与重排 ---------------------------------------------------

    def plan(self, water_m3: float | None = None) -> WashPlan:
        """首次建排程：当前批按车上水装箱（批间补水点由重建自动插入）。"""

        with self._lock:
            if self._has_plan:
                raise InterlockError("排程已存在，变化应走重排", component="wash")
            water = self._full_m3 if water_m3 is None else max(0.0, float(water_m3))
            self._mode = MODE_WASHING
            self._batch_seq += 1
            self._current_batch_no = self._batch_seq
            self._plan = self._rebuild(REASON_INITIAL, water)
            self._log_reschedule(REASON_INITIAL, water)
            return self._plan

    def _reschedule(
        self, reason: str, water_m3: float, *, detail: str = "", commit_next: bool = False
    ) -> WashPlan:
        self._plan = self._rebuild(reason, water_m3, commit_next=commit_next)
        self._log_reschedule(reason, water_m3, detail=detail)
        return self._plan

    def _log_reschedule(self, reason: str, water_m3: float, *, detail: str = "") -> None:
        self._reschedules.append(
            {
                "version": self._plan.version,
                "reason": reason,
                "detail": detail,
                "mode": self._mode,
                "current_batch": None if self._current_batch_no is None else f"B{self._current_batch_no:03d}",
                "water_m3": round(water_m3, 4),
            }
        )

    # ---- 状态机推进 -----------------------------------------------------

    def current_batch(self) -> Batch | None:
        with self._lock:
            return next(
                (b for b in self._plan.batches if b.state in (BATCH_ACTIVE, BATCH_PLANNED)), None
            )

    def active_wash(self) -> WashTask | None:
        """返回正在洗的任务（由持久事实 ``_active_mirror`` 决定）。"""

        with self._lock:
            if self._active_mirror is None:
                return None
            return next(
                (t for t in self._plan.tasks
                 if t.kind == TASK_WASH and t.mirror == self._active_mirror),
                None,
            )

    def _pending_tasks(self) -> list:
        return [t for t in self._plan.tasks if t.state == TASK_PENDING]

    def begin_next_mirror(self) -> WashTask:
        """把未洗序列队首的 wash 置 active；队首是补水任务时拒绝（先回站）。"""

        with self._lock:
            if self.active_wash() is not None:
                raise InterlockError("有镜子尚未洗完，不能开始下一面", component="wash")
            upcoming = self._pending_tasks()
            if upcoming and upcoming[0].kind == TASK_REFILL:
                raise InterlockError("水量不足，必须先回站补水再继续清洗", component="wash")
            task = next((t for t in upcoming if t.kind == TASK_WASH), None)
            if task is None:
                raise InterlockError("排程里没有待洗的镜子", component="wash")
            self._active_mirror = task.mirror
            # 重建当前批，使本面以 active 冻结进当前批（其余成员按实测水重装箱）。
            water = self._reading.water_m3 if self._reading is not None else self._full_m3
            self._mode = MODE_WASHING
            self._plan = self._rebuild(REASON_MIRROR_START, water)
            return task

    def complete_mirror(
        self, mirror: str, reading: WaterReading | None = None, *, near_station: bool | None = None
    ) -> dict:
        """一面洗完：登记回读，按车况决定继续/缩减/回站，再据此重排。"""

        with self._lock:
            task = self.active_wash()
            if task is None or task.mirror != mirror:
                raise InterlockError(f"{mirror} 不在清洗中，不能登记完成", component="wash")
            if reading is not None:
                self._reading = reading
            back = self._reading
            if back is None:
                back = WaterReading(0.0, WATER_MISSING, True, self._condition.ts)
                self._reading = back

            # 本面落账：进入当前批已完成前缀，并清除"在洗"事实。
            if mirror not in self._done_in_current:
                self._done_in_current.append(mirror)
            self._active_mirror = None

            remaining_after = self._pending_order(None)
            result = {
                "mirror": mirror,
                "batch_id": task.batch_id,
                "remaining_in_plan": len(remaining_after),
                "rescheduled": False,
                "decision": None,
            }

            # 全部洗完：关闭当前批（成员前缀即整批），收尾。
            if not remaining_after:
                self._close_current_batch()
                self._mode = MODE_WASHING
                self._reschedule(REASON_BATCH_DONE, back.water_m3)
                result["rescheduled"] = True
                return result

            near = self._condition.near_station if near_station is None else near_station
            decision = decide_shortage(
                back, len(remaining_after), self.per_mirror_m3, self._limits, near_station=near
            )
            result["decision"] = decision.as_dict()
            if decision.returns_to_station:
                # 回站补水后重排：当前批以已完成前缀关闭，剩余进补水后的新批。
                self._close_current_batch()
                self._mode = MODE_RETURNING
                self._reschedule(REASON_SHORTAGE, back.water_m3, detail=decision.reason)
            else:
                # 车况允许：当前批继续，缩减到实测水洗得动的前缀（重建自动装箱）。
                self._mode = MODE_WASHING
                self._reschedule(REASON_SHORTAGE, back.water_m3, detail=decision.reason)
            result["rescheduled"] = True
            return result

    def _close_current_batch(self) -> None:
        """把当前批（已完成前缀）落进历史，并清空当前批号。"""

        if self._current_batch_no is None:
            return
        members = list(self._done_in_current)
        self._finished_batches.append((self._current_batch_no, members))
        self._done_in_current = []
        self._current_batch_no = None

    # ---- 水量回读（部分失败）与补水 --------------------------------------

    def report_water(self, condition: TruckCondition) -> dict:
        """登记车况与水量回读；回读缺失/车况异常/已在站旁则回站，否则按实测重排。"""

        with self._lock:
            self._condition = condition
            reading = condition.as_reading(self._limits)
            self._reading = reading
            returning = reading.missing or not reading.truck_ok or condition.near_station
            self._mode = MODE_RETURNING if returning else MODE_WASHING
            # 切到回站模式且当前没有在洗面：当前批以已完成前缀关闭，剩余进补水后新批。
            if returning and self._active_mirror is None and self._current_batch_no is not None:
                self._close_current_batch()
            if self._has_plan:
                self._reschedule(
                    REASON_TRUCK if (reading.missing or not reading.truck_ok) else REASON_WATER,
                    reading.water_m3,
                )
            return {"reading": reading.as_dict(), "usable_m3": usable_water_m3(reading, self._limits)}

    def complete_refill(self, water_m3: float | None = None) -> dict:
        """回站补水完成：水量回满，提交下一批（推进稳定批号），回 washing 模式。"""

        with self._lock:
            # 下一批的稳定号即"待提交未来批"号：_batch_seq+1，引入它的补水是 R(batch_seq)。
            next_no = self._batch_seq + 1
            expected_refill = f"R{next_no - 1:03d}"
            refill_id = None
            upcoming = self._pending_tasks()
            if any(t.kind == TASK_REFILL and t.task_id == expected_refill for t in upcoming):
                refill_id = expected_refill
                self._refills_done.append(refill_id)
            filled = self._full_m3 if water_m3 is None else max(0.0, float(water_m3))
            self._reading = WaterReading(filled, "ok", True, self._condition.ts)
            self._mode = MODE_WASHING
            self._reschedule(REASON_REFILLED, filled, commit_next=True)
            return {"filled_m3": filled, "rescheduled": True, "refill_id": refill_id}

    # ---- 查询 / 一致性 --------------------------------------------------

    def mirrors_per_full_tank(self) -> int:
        """满水箱（留保底）一车水够洗几面。"""

        return packable_count(
            max(0.0, self._full_m3 - self._limits.return_reserve_m3), self.per_mirror_m3
        )

    def plan_view(self) -> dict:
        """控制台与审计共用的排程快照。"""

        with self._lock:
            active = self.active_wash()
            current = self.current_batch()
            reading = self._reading
            upcoming = next((t for t in self._plan.tasks if t.state == TASK_PENDING), None)
            return {
                "version": self._plan.version,
                "reason": self._plan.reason,
                "mode": self._mode,
                "batch_policy": self._limits.batch_policy,
                "per_mirror_m3": round(self.per_mirror_m3, 4),
                "truck_capacity_m3": self._limits.truck_capacity_m3,
                "return_reserve_m3": self._limits.return_reserve_m3,
                "truck_safety_m3": self._limits.truck_safety_m3,
                "mirrors_per_full_tank": self.mirrors_per_full_tank(),
                "water": None if reading is None else reading.as_dict(),
                "usable_m3": (
                    round(usable_water_m3(reading, self._limits), 4) if reading is not None else None
                ),
                "active_mirror": None if active is None else active.mirror,
                "current_batch": None if current is None else current.as_dict(),
                "next_action": None if upcoming is None else upcoming.kind,
                "batches_total": len(self._plan.batches),
                "batches_finished": len(self._finished_batches),
                "refills_completed": len(self._refills_done),
                "plan": self._plan.as_dict(),
                "reschedules": list(self._reschedules),
            }

    def consistency_violations(self) -> list[str]:
        """排程、水量与审计三方一致性自检，返回违约项（空列表即一致）。"""

        violations: list[str] = []
        plan = self._plan
        wash_tasks = [t for t in plan.tasks if t.kind == TASK_WASH]
        batch_ids = {b.batch_id for b in plan.batches}

        # 1) 任务↔批次：每面 wash 必须落在存在的批次，成员与顺序一致。
        for task in wash_tasks:
            if task.batch_id not in batch_ids:
                violations.append(f"task_without_batch:{task.mirror}")
        for batch in plan.batches:
            members = [t for t in wash_tasks if t.batch_id == batch.batch_id]
            if len(members) != len(batch.mirrors):
                violations.append(f"batch_task_mismatch:{batch.batch_id}")
            elif [t.mirror for t in members] != list(batch.mirrors):
                violations.append(f"batch_order_mismatch:{batch.batch_id}")

        # 2) 不把水用干：计划（未发生）批次毛装量不得超过 满水−保底 装箱上限。
        cap_full = self.mirrors_per_full_tank()
        for batch in plan.batches:
            if batch.state == BATCH_PLANNED and len(batch.mirrors) > cap_full:
                violations.append(f"batch_exceeds_water:{batch.batch_id}")

        # 3) 脏度优先：pending wash 必须与 grade_first 全序一致（在洗面冻结不参与）。
        pending_names = [t.mirror for t in wash_tasks if t.state == TASK_PENDING]
        global_order = [entry.name for entry in self._dirt.order()]
        ranked = [name for name in global_order if name in pending_names]
        if ranked != pending_names:
            violations.append("grade_order_violated")

        # 4) 时序：至多一个 active wash；回站模式未洗队首必须是补水任务。
        if len([t for t in wash_tasks if t.state == TASK_ACTIVE]) > 1:
            violations.append("multiple_active_wash")
        upcoming = [t for t in plan.tasks if t.state == TASK_PENDING]
        has_pending_wash = any(t.kind == TASK_WASH for t in upcoming)
        if self._mode == MODE_RETURNING and has_pending_wash:
            if not upcoming or upcoming[0].kind != TASK_REFILL:
                violations.append("returning_without_leading_refill")

        # 5) 回读缺失按不足处理：未洗队首必须是补水任务，不得直接继续洗。
        reading = self._reading
        if reading is not None and reading.missing and has_pending_wash:
            if not upcoming or upcoming[0].kind != TASK_REFILL:
                violations.append("missing_readback_without_refill")

        # 6) 审计一致：已完成补水台账必须与队列里 done 的补水任务一一对应。
        done_refill_ids = {
            t.task_id for t in plan.tasks if t.kind == TASK_REFILL and t.state == TASK_DONE
        }
        if done_refill_ids != set(self._refills_done):
            violations.append("refill_ledger_mismatch")
        return violations
