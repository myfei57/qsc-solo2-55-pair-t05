"""清洗排程领域模型。

排程层只描述"先洗谁、一批装几面、什么时候回站补水"，不直接驱动清洗车。字段
全部可 JSON 化，控制台展示、落盘审计与跨重启恢复读的是同一份结构。

任务分两类：``wash``（洗一面镜子）与 ``refill``（回站补满水）。批次只装 wash
任务，refill 任务插在两批之间，靠任务先后位置写死时序。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---- 任务与批次状态（状态机，取值写死，不许出现其它字符串） ----------------

TASK_PENDING = "pending"
TASK_ACTIVE = "active"
TASK_DONE = "done"
TASK_DROPPED = "done"  # 完成/被缩减都视为离开队列，保留 DONE 名字便于审计区分

BATCH_PLANNED = "planned"
BATCH_ACTIVE = "active"
BATCH_DONE = "done"

TASK_WASH = "wash"
TASK_REFILL = "refill"

# 回站补水决策结果。
DECISION_SHRINK = "shrink_batch"   # 水量够洗剩余前缀：缩减本批
DECISION_RETURN = "return_refill"  # 水量不够：回站补水后重排

# 水量回读状态。
WATER_OK = "ok"
WATER_MISSING = "missing"  # 部分失败：回读缺失，按水量不足处理


@dataclass(frozen=True)
class WashTask:
    """清洗一面镜子的任务。"""

    mirror: str
    grade: int
    group: str
    water_m3: float
    batch_id: str
    state: str = TASK_PENDING

    @property
    def kind(self) -> str:
        return TASK_WASH

    def as_dict(self) -> dict:
        return {
            "kind": TASK_WASH,
            "mirror": self.mirror,
            "grade": self.grade,
            "group": self.group,
            "water_m3": round(self.water_m3, 4),
            "batch_id": self.batch_id,
            "state": self.state,
        }


@dataclass(frozen=True)
class RefillTask:
    """回站补水任务（插在两批清洗之间）。"""

    task_id: str
    state: str = TASK_PENDING

    @property
    def kind(self) -> str:
        return TASK_REFILL

    def as_dict(self) -> dict:
        return {"kind": TASK_REFILL, "task_id": self.task_id, "state": self.state}


@dataclass(frozen=True)
class Batch:
    """一批清洗：装得下若干面镜子，并且洗完仍留得住回站保底水量。"""

    batch_id: str
    mirrors: tuple[str, ...]
    grades: tuple[int, ...]
    groups: tuple[str, ...]
    water_need_m3: float          # 名义毛耗水
    water_leaving_m3: float       # 洗完这批后水箱预计剩余（名义）
    state: str = BATCH_PLANNED

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "mirrors": list(self.mirrors),
            "grades": list(self.grades),
            "groups": list(self.groups),
            "water_need_m3": round(self.water_need_m3, 4),
            "water_leaving_m3": round(self.water_leaving_m3, 4),
            "count": len(self.mirrors),
            "state": self.state,
        }


@dataclass(frozen=True)
class WaterReading:
    """一次车载水量回读；``status == missing`` 表示部分失败。"""

    water_m3: float
    status: str
    truck_ok: bool
    ts: float

    @property
    def missing(self) -> bool:
        return self.status == WATER_MISSING

    def as_dict(self) -> dict:
        return {
            "water_m3": round(self.water_m3, 4),
            "status": self.status,
            "truck_ok": self.truck_ok,
            "ts": round(self.ts, 3),
        }


@dataclass
class WashPlan:
    """某一版完整排程：队列（任务）与批次视图一一对应。"""

    version: int
    batches: list[Batch] = field(default_factory=list)
    tasks: list[WashTask | RefillTask] = field(default_factory=list)
    reason: str = ""
    water_m3: float = 0.0
    usable_m3: float = 0.0

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "reason": self.reason,
            "water_m3": round(self.water_m3, 4),
            "usable_m3": round(self.usable_m3, 4),
            "batches": [batch.as_dict() for batch in self.batches],
            "tasks": [task.as_dict() for task in self.tasks],
        }
