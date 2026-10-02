"""镜面清洗。

清洗车按"到镜 → 喷淋 → 回水回收 → 排水"跑一个循环。回水回收这条链最容易
出错：排水没排完就把回水泵停掉，回水会溢到地面并流进电气设备。因此顺序被
写死成"先开排水阀、排空确认之后才停回水泵"。

排程层（:mod:`cspplant.wash.schedule`）在循环之上定死清洗次序：脏度优先、
同档区域连片，按一车净用水封批；水量回读缺失/过期是部分失败，按水量不足
回站补水后重排，绝不把车上的水用光。
"""

from __future__ import annotations

from .cycle import WashCycle, WashReport
from .dirtiness import (
    BAND_WATER_FACTOR,
    DIRT_BANDS,
    DirtinessLedger,
    band_of,
    band_rank,
)
from .drain import DrainSystem
from .persist import WashPersistence
from .schedule import (
    Batch,
    Schedule,
    ShortagePolicy,
    TruckCondition,
    WashScheduler,
)
from .snapshot import WashSnapshot
from .truck import WashTruck
from .water import WaterBudget, WaterQuote

__all__ = [
    "WashCycle",
    "WashReport",
    "DrainSystem",
    "WashSnapshot",
    "WashTruck",
    "DirtinessLedger",
    "DIRT_BANDS",
    "BAND_WATER_FACTOR",
    "band_of",
    "band_rank",
    "WaterBudget",
    "WaterQuote",
    "WashScheduler",
    "ShortagePolicy",
    "TruckCondition",
    "Batch",
    "Schedule",
    "WashPersistence",
]
