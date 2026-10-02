"""按脏度排程驱动的镜面清洗管理。

``WashManager`` 是"排程 → 执行 → 回读 → 重排"闭环在平台侧的持有者：它拥有
:class:`~cspplant.wash.plan.scheduler.WashScheduler`，并在控制台/编排与真实清洗
循环之间翻译。三种入口：

- ``load_dirt``：把镜场台账里的位置与本次上报的脏度灌进排程器并建排；
- ``run_due``：执行到下一个不可继续点为止（洗完一面、需要回站补水，或全部洗完）；
- ``refill_and_resume``：回站补满水后继续。

它本身**不直接发审计**：返回结构化报告（步骤、决策、违约项、排程快照），由平台
统一发布事件，保证排程、水量与审计三份口径一致。真实喷淋仍走
:class:`~cspplant.wash.cycle.WashCycle` 的"到镜→喷淋→排空→停泵"硬顺序，避让
窗口仍由镜场开关，排程器只决定先洗谁、洗几面、何时回站。
"""

from __future__ import annotations

from ..config import WashLimits
from ..field.field import FieldController
from ..eventbus import Event
from .cycle import WashCycle
from .drain import DrainSystem
from .plan.budget import TruckCondition
from .plan.models import WATER_MISSING, WaterReading
from .plan.scheduler import MODE_RETURNING, WashScheduler
from .truck import WashTruck


class WashManager:
    """持有排程器并按其结果驱动清洗循环。"""

    def __init__(
        self,
        limits: WashLimits,
        field: FieldController,
        cycle: WashCycle,
        truck: WashTruck,
        drain: DrainSystem,
        *,
        scheduler: WashScheduler | None = None,
    ) -> None:
        self._limits = limits
        self._field = field
        self._cycle = cycle
        self._truck = truck
        self._drain = drain
        self.scheduler = scheduler or WashScheduler(limits)
        self._zone = ""
        self._open_window_mirrors: list[str] = []
        # 排程记账水量：按单面名义毛耗水递减，**不把回水回收记成可用水**。物理车上的
        # 回收水只会让真实余量更宽裕，作为额外安全垫，绝不允许在排程阶段提前透支。
        self._book_water: float | None = None

    @property
    def limits(self) -> WashLimits:
        return self._limits

    # ---- 脏度装载与建排 -------------------------------------------------

    def load_dirt(self, dirt_rows: list[dict], zone: str, water_m3: float | None = None) -> dict:
        """按 ``[{name, grade}]`` 装载脏度（位置取自镜场台账），建排并打开避让窗口。"""

        registry = self._field.registry
        enriched: list[dict] = []
        for row in dirt_rows:
            name = str(row["name"])
            mirror = registry.get(name)
            enriched.append(
                {
                    "name": name,
                    "grade": int(row["grade"]),
                    "group": mirror.group,
                    "x_m": mirror.x_m,
                    "y_m": mirror.y_m,
                }
            )
        self.scheduler.set_dirt_bulk(enriched)
        self._book_water = (
            float(water_m3) if water_m3 is not None else float(self._limits.truck_capacity_m3)
        )
        plan = self.scheduler.plan(self._book_water)
        self._zone = str(zone)
        due = self._current_window_mirrors()
        if due:
            window = self._field.open_window(due, self._zone, 0.0)
            self._open_window_mirrors = list(window.mirrors)
        return {
            "plan": self.scheduler.plan_view(),
            "window": {"zone": self._zone, "mirrors": list(self._open_window_mirrors)},
            "queued": len(plan.tasks),
        }

    def _current_window_mirrors(self) -> list[str]:
        """当前批（不跨补水点）要避让的镜子：从队首到第一个补水任务为止。"""

        view = self.scheduler.plan_view()["plan"]["tasks"]
        names: list[str] = []
        for task in view:
            if task["kind"] == "refill":
                break
            if task["kind"] == "wash" and task["state"] in ("pending", "active"):
                names.append(task["mirror"])
        return names

    # ---- 执行一步 -------------------------------------------------------

    def run_due(self, now: float, dt: float = 1.0) -> dict:
        """执行到下一个挂起点，返回结构化报告。"""

        steps: list[dict] = []
        violations: list[str] = []
        mode_before = self.scheduler.mode

        # 需要先回站补水：不喷淋，挂起等补水。
        try:
            task = self.scheduler.begin_next_mirror()
        except Exception as error:
            return {
                "ok": False,
                "suspended": "refill",
                "reason": getattr(error, "message", str(error)),
                "steps": steps,
                "violations": violations,
                "schedule": self.scheduler.plan_view(),
                "washed": 0,
            }

        mirror = task.mirror
        report = self._cycle.run(self._truck, self._drain, mirror, dt)
        steps.extend(report.steps)
        violations.extend(report.violations)

        # 排程按名义毛耗水记账（不预付回水回收）：每面扣一个单面毛额，物理回收水只
        # 作为额外安全垫。回读失败按部分失败（缺失）处理，交排程裁决回站。
        per = self._limits.water_per_mirror_m3()
        if self._book_water is None:
            self._book_water = self._limits.truck_capacity_m3
        self._book_water = max(0.0, self._book_water - per)
        reading = self.read_truck(self._book_water, ok=report.ok)
        outcome = self.scheduler.complete_mirror(mirror, reading)

        # 回站（本批结束/水量不足）或全部洗完时，都要关闭当前避让窗口；下一批会按
        # 新名单重开，全部洗完则让镜子回到自动追日。
        view = self.scheduler.plan_view()
        finished_all = (
            view["active_mirror"] is None
            and not [t for t in view["plan"]["tasks"] if t["kind"] == "wash" and t["state"] == "pending"]
        )
        if self.scheduler.mode == MODE_RETURNING or finished_all:
            self._close_window_if_open(now, steps)

        return {
            "ok": report.ok and not violations,
            "suspended": self.scheduler.mode if self.scheduler.mode == MODE_RETURNING else None,
            "mirror": mirror,
            "batch_id": outcome["batch_id"],
            "decision": outcome["decision"],
            "water": reading.as_dict(),
            "book_water_m3": round(self._book_water, 4),
            "truck_water_m3": round(self._truck.water_m3(), 4),
            "used_m3": round(report.used_m3, 4),
            "recovered_m3": round(report.recovered_m3, 4),
            "steps": steps,
            "violations": violations,
            "schedule": self.scheduler.plan_view(),
            "washed": 1 if report.ok else 0,
            "mode_before": mode_before,
        }

    def read_truck(self, water_m3: float | None, *, ok: bool = True) -> WaterReading:
        """把清洗车水量回读归一化；失败/越界按部分失败（回读缺失）处理。"""

        if not ok or water_m3 is None:
            return WaterReading(0.0, WATER_MISSING, True, 0.0)
        value = max(0.0, float(water_m3))
        status = "ok"
        return WaterReading(value, status, True, 0.0)

    def refill_and_resume(self, now: float, water_m3: float | None = None) -> dict:
        """回站补满水，按新批重开避让窗口，返回可继续的下一批信息。"""

        steps: list[dict] = []
        filled = (
            float(water_m3) if water_m3 is not None else float(self._limits.truck_capacity_m3)
        )
        refill = self.scheduler.complete_refill(filled)
        self._book_water = refill["filled_m3"]
        steps.append({"component": "wash", "action": "return_refill",
                      "detail": {"filled_m3": round(refill["filled_m3"], 4)}})
        # 物理车也补到目标水位（回收水已在车上，按差额补到满）。
        if refill["filled_m3"] > self._truck.water_m3():
            self._truck.refill(refill["filled_m3"] - self._truck.water_m3())
        # 上一窗口已在回站时关闭；按新一批名单重开。
        self._close_window_if_open(now, steps)
        due = self._current_window_mirrors()
        window_mirrors: list[str] = []
        if due:
            window = self._field.open_window(due, self._zone, now)
            window_mirrors = list(window.mirrors)
        return {
            "ok": True,
            "suspended": None,
            "steps": steps,
            "violations": [],
            "schedule": self.scheduler.plan_view(),
            "window": {"zone": self._zone, "mirrors": window_mirrors},
            "filled_m3": round(refill["filled_m3"], 4),
            "book_water_m3": round(self._book_water, 4),
            "truck_water_m3": round(self._truck.water_m3(), 4),
        }

    def _close_window_if_open(self, now: float, steps: list[dict]) -> None:
        if self._field.sweeps.active() is None:
            return
        closed = self._field.close_window(now)
        self._open_window_mirrors = []
        steps.append(
            {"component": "field", "action": "close_window",
             "detail": {"zone": closed.zone, "closed_at": closed.closed_at}}
        )

    # ---- 中途脏度变化 ---------------------------------------------------

    def update_dirt(self, name: str, grade: int) -> dict:
        """更新一面镜子的脏度（位置仍取台账），排程自动只重排未洗部分。"""

        mirror = self._field.registry.get(name)
        entry = self.scheduler.set_dirt(name, int(grade), mirror.group, mirror.x_m, mirror.y_m)
        # 当前批/避让窗口可能因脏度重排而改变，按新队首同步窗口名单。
        self._resync_window()
        return {"entry": entry, "schedule": self.scheduler.plan_view()}

    def _resync_window(self) -> None:
        """重排后把避让窗口对齐到新的当前批名单（仅当窗口开着且在洗间隙）。"""

        if self.scheduler.mode == MODE_RETURNING:
            return  # 回站期间窗口已关闭，等补水后按新批重开
        due = self._current_window_mirrors()
        active = self._field.sweeps.active()
        if active is None:
            if due:
                window = self._field.open_window(due, self._zone, 0.0)
                self._open_window_mirrors = list(window.mirrors)
            return
        if set(due) != set(active.mirrors):
            self._field.close_window(0.0)
            if due:
                window = self._field.open_window(due, self._zone, 0.0)
                self._open_window_mirrors = list(window.mirrors)

    # ---- 查询 -----------------------------------------------------------

    def report_condition(self, condition: TruckCondition) -> dict:
        """外部车况/水量回读入口（回读缺失在此被标记为部分失败）。"""

        return self.scheduler.report_water(condition)

    def view(self) -> dict:
        return self.scheduler.plan_view()

    def consistency_violations(self) -> list[str]:
        return self.scheduler.consistency_violations()


def wash_events(report: dict) -> list[Event]:
    """把一次 run_due/refill 报告翻译成要发布的审计事件（平台调用）。"""

    events: list[Event] = []
    for step in report.get("steps", []):
        detail = step.get("detail", {})
        flat = {k: v for k, v in dict(detail).items() if not isinstance(v, (dict, list))}
        events.append(
            Event.of(step["component"], step["component"], step["action"],
                     asset=str(report.get("mirror", "")), **flat)
        )
    for violation in report.get("violations", []):
        events.append(
            Event.of("audit", "audit", "interlock_rejected",
                     asset=str(report.get("mirror", "")), severity="alarm",
                     message=f"清洗被拒绝：{violation}", violation=violation)
        )
    schedule = report.get("schedule")
    if schedule is not None:
        events.append(
            Event.of("wash", "wash", "wash_reschedule",
                     severity="notice",
                     version=schedule["version"],
                     mode=schedule["mode"],
                     next_action=schedule.get("next_action") or "",
                     suspended=report.get("suspended") or "",
                     batches=schedule["batches_total"])
        )
    return events
