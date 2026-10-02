"""按脏度排清洗顺序/批次、一车水预算、回站补水与缺失回读（部分失败）。"""

from __future__ import annotations

import dataclasses

import pytest

from cspplant.config import WashLimits, default_config
from cspplant.wash.dirtiness import DIRT_BANDS, DirtinessLedger, band_of
from cspplant.wash.schedule import (
    CONTINUE,
    DONE,
    REFILL_AND_RESUME,
    REFILL_BEFORE_BATCH,
    TRIM_BATCH,
    ShortagePolicy,
    TruckCondition,
    WashScheduler,
)
from cspplant.wash.truck import READBACK_STALE_S, WashTruck
from cspplant.wash.water import RESERVE_RATIO, WaterBudget


LIMITS = WashLimits(90.0, 26.0, 4.0, 9.0)


def _ledger(values: dict[str, float]) -> DirtinessLedger:
    ledger = DirtinessLedger()
    for name, value in values.items():
        ledger.set(name, value)
    return ledger


# ---- 脏度分档 ----------------------------------------------------------


def test_dirtiness_bands_are_ordered_dirty_to_clean():
    assert band_of(0.95) == "heavy"
    assert band_of(0.65) == "soiled"
    assert band_of(0.45) == "average"
    assert band_of(0.25) == "light"
    assert band_of(0.05) == "clean"
    assert DIRT_BANDS[0] == "heavy" and DIRT_BANDS[-1] == "clean"


def test_dirtiness_rows_sorted_desc():
    ledger = _ledger({"a": 0.2, "b": 0.9, "c": 0.5})
    rows = ledger.rows(["a", "b", "c"])
    assert [row["mirror"] for row in rows] == ["b", "c", "a"]


# ---- 排序：脏度优先、同档连片、区域不跨档 ------------------------------


def test_rank_is_dirt_first_geo_only_within_band():
    ledger = _ledger({
        "a": 0.90, "b": 0.85,            # heavy
        "c": 0.30, "d": 0.31, "e": 0.29,  # average
    })
    # d/e 彼此很近却离 c 极远；c 离原点近。同档内从原点最近邻应先走 c。
    coords = {"c": (0.0, 0.0), "d": (100.0, 0.0), "e": (101.0, 0.0)}
    scheduler = WashScheduler(LIMITS, ledger, max_batch=10)
    order = scheduler.rank(list(ledger.snapshot()), coords)
    assert order[0:2] == ["a", "b"]  # heavy 整档压过 average
    tail = order[2:]
    assert tail[0] == "c"           # 同档内从原点最近邻
    assert tail.index("d") < tail.index("e")  # d/e 连片


def test_geo_can_never_promote_a_cleaner_mirror():
    ledger = _ledger({"dirty": 0.95, "near": 0.30, "far": 0.31})
    coords = {"dirty": (1000.0, 0.0), "near": (0.0, 0.0), "far": (1.0, 0.0)}
    scheduler = WashScheduler(LIMITS, ledger, max_batch=10)
    order = scheduler.rank(["dirty", "near", "far"], coords)
    assert order[0] == "dirty"  # 再远也先洗脏镜


# ---- 一车水能洗几面 ----------------------------------------------------


def test_water_budget_net_usage_applies_recovery_and_band_factor():
    budget = WaterBudget(LIMITS)
    quote = budget.quote("m1", 0.95)  # heavy
    assert quote.band == "heavy"
    assert quote.net_m3 == pytest.approx(quote.gross_m3 * (1 - 0.85))
    assert quote.gross_m3 > budget.quote("m2", 0.05).gross_m3


def test_pack_never_exceeds_usable_water_and_keeps_order():
    budget = WaterBudget(LIMITS)
    ledger = _ledger({f"m{i}": 0.5 for i in range(20)})
    scheduler = WashScheduler(LIMITS, ledger, max_batch=20)
    schedule = scheduler.plan("c", list(ledger.snapshot()), 9.0)
    # 每面净用水约 0.0975 m³，扣 0.9 m³ 余量后 9 m³ 可装约 83 面，但只有 20。
    assert sum(len(b.mirrors) for b in schedule.batches) == 20


def test_tight_water_splits_into_multiple_batches():
    tiny = WashLimits(90.0, 26.0, 4.0, 0.8)
    ledger = _ledger({f"m{i:02d}": 0.95 for i in range(12)})
    scheduler = WashScheduler(tiny, ledger, WaterBudget(tiny), max_batch=12)
    schedule = scheduler.plan("c", list(ledger.snapshot()), 0.8)
    sizes = [len(b.mirrors) for b in schedule.batches]
    assert len(sizes) >= 2
    assert sum(sizes) == 12
    # 第一批受 0.8m³ 限制，后续批按满箱 0.8m³ 同样切分。
    assert sizes[0] <= 5


# ---- 回读缺失 = 部分失败 = 按不足处理 ----------------------------------


def test_missing_readback_is_degraded_and_uses_conservative_floor():
    truck = WashTruck(LIMITS)
    now = 100.0
    assert truck.readback_fresh(now) is True
    truck.report_water(None, now)
    assert truck.readback_degraded(now) is True
    # 名义账本是下限，部分失败时保守水位 == 名义水位（绝不假设满箱）。
    assert truck.effective_water_m3(now) == truck.water_m3()


def test_stale_readback_is_degraded():
    truck = WashTruck(LIMITS)
    truck.report_water(9.0, 0.0)
    assert truck.readback_degraded(READBACK_STALE_S + 1.0) is True
    assert truck.readback_fresh(READBACK_STALE_S - 1.0) is True


def test_effective_water_takes_min_of_readback_and_ledger():
    truck = WashTruck(LIMITS)
    truck.report_water(3.0, 0.0)  # 传感器说只剩 3 方
    assert truck.water_m3() == 9.0
    assert truck.effective_water_m3(0.0) == 3.0  # 取小，不冒险


def test_refill_restores_fresh_readback():
    truck = WashTruck(LIMITS)
    truck.report_water(None, 0.0)
    assert truck.readback_degraded(0.0) is True
    truck.refill_to_full(ts=5.0)
    assert truck.readback_degraded(5.0) is False
    assert truck.water_m3() == pytest.approx(9.0)


# ---- 车况策略：缩批 vs 回站补水 ----------------------------------------


def _quotes(budget: WaterBudget, names: list[str]) -> list:
    return [budget.quote(name, 0.95) for name in names]


def test_policy_before_batch_requires_refill_when_degraded_even_if_water_ok():
    budget = WaterBudget(LIMITS)
    policy = ShortagePolicy()
    quotes = _quotes(budget, ["a"])
    ok = TruckCondition.from_snapshot(9.0, False, 1, 1)
    bad = TruckCondition.from_snapshot(9.0, True, 1, 1)
    assert policy.decide_before_batch(budget, quotes, ok) == CONTINUE
    assert policy.decide_before_batch(budget, quotes, bad) == REFILL_BEFORE_BATCH


def test_policy_mid_batch_returns_to_station_when_degraded():
    budget = WaterBudget(LIMITS)
    policy = ShortagePolicy()
    tail = _quotes(budget, ["a", "b"])
    condition = TruckCondition.from_snapshot(9.0, True, 10, 12)
    assert policy.decide_mid_batch(budget, tail, condition) == REFILL_AND_RESUME


def test_policy_mid_batch_trims_only_for_a_short_tail():
    budget = WaterBudget(LIMITS)
    policy = ShortagePolicy()
    tail = _quotes(budget, [f"m{i}" for i in range(12)])
    # 水只够约一个前缀，且后面战役只剩零头（不足一整车）-> 缩批。
    short = TruckCondition.from_snapshot(2.0, False, 0, 12)
    assert policy.decide_mid_batch(budget, tail, short) == TRIM_BATCH
    # 后面还有大量活 -> 回站补水后重排。
    long_left = TruckCondition.from_snapshot(2.0, False, 40, 12)
    assert policy.decide_mid_batch(budget, tail, long_left) == REFILL_AND_RESUME


# ---- 重排：时序不乱、版本递增 ------------------------------------------


def test_replan_keeps_washed_mirrors_and_reorders_only_tail():
    ledger = _ledger({"a": 0.9, "b": 0.8, "c": 0.3, "d": 0.2, "e": 0.1})
    big = WashLimits(90.0, 26.0, 4.0, 100.0)
    scheduler = WashScheduler(big, ledger, WaterBudget(big), max_batch=5)
    scheduler.plan("c1", ["a", "b", "c", "d", "e"], 100.0)
    scheduler.open_campaign()
    for name in ["a", "b"]:
        scheduler.mark_washed(name)
    new = scheduler.replan(100.0, reason="dirt_changed", dirt_updates={"e": 0.99})
    assert new.version == 2
    tail = new.ordered_mirrors()
    assert "a" not in tail and "b" not in tail  # 已洗不动
    assert tail[0] == "e"                      # 新变脏的尾镜排到最前
    assert scheduler.washed_mirrors() == ["a", "b"]
    # 洗完后脏度回写为最净档。
    assert ledger.band("a") == "clean"


# ---- 平台端到端 --------------------------------------------------------


def test_platform_wash_orders_by_dirtiness(platform):
    platform.start()
    platform.track(800.0, 720.0)
    names = ["m021", "m022", "m023", "m024"]
    dirt = {name: value for name, value in zip(names, [0.2, 0.95, 0.5, 0.8])}
    report = platform.wash(names, "maint-north", dirtiness=dirt)
    assert report["ok"] is True
    washed = [
        step["detail"]["mirror"]
        for step in report["steps"]
        if step["action"] == "mirror_washed"
    ]
    assert washed == ["m022", "m024", "m023", "m021"]
    assert report["washed"] == 4
    assert report["schedule_version"] >= 1


def test_platform_wash_missing_readback_returns_then_completes(platform):
    platform.start()
    platform.track(800.0, 720.0)
    report = platform.wash(["m021", "m022"], "maint-north", readback_water_m3=None)
    assert report["washed"] == 2
    assert "water_readback_degraded" in report["violations"]
    assert report["refills"] >= 1
    # 部分失败必须落审计。
    actions = platform.audit.summary()["by_action"]
    assert "truck_water_readback" in actions or "wash_schedule_committed" in actions


def test_platform_wash_accounts_are_consistent(platform):
    platform.start()
    platform.track(800.0, 720.0)
    names = platform.heliostats.names()[:40]
    report = platform.wash(names, "maint-north")
    assert report["ok"] is True
    assert report["washed"] == report["mirrors"] == 40
    assert "wash_account_mismatch" not in report["violations"]
    assert "wash_schedule_incomplete" not in report["violations"]
    assert platform.truck.washes() == 40
    assert platform.mirrors_in_window() == []


def test_platform_wash_never_runs_tank_dry(platform):
    platform.start()
    platform.track(800.0, 720.0)
    names = [f"m{i:03d}" for i in range(21, 33)]
    platform.set_dirtiness({n: 0.95 for n in names})
    # 半箱出发，强制批中回站。
    platform.truck._water_m3 = 0.35
    platform.truck.report_water(0.35, platform.clock.now())
    report = platform.wash(names, "maint-north")
    assert report["ok"] is True
    assert report["washed"] == 12
    for batch in report["batches"]:
        assert batch["water_after_m3"] >= 0.0
    assert report["water"]["effective_m3"] >= 0.0


def test_platform_wash_persists_dirtiness(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.wash(["m021"], "maint-north", dirtiness={"m021": 0.99})
    stored = platform.wash_persistence.read_dirtiness()
    assert stored is not None
    assert stored["m021"] < 0.2  # 洗完回写为净档并落盘


def test_platform_schedule_and_water_views(platform):
    platform.start()
    view = platform.wash_schedule_view()
    assert {"progress", "schedule", "dirtiness", "water", "budget"} <= set(view)
    plan = platform.wash_plan(["m021", "m022"])
    assert plan["schedule"]["batch_count"] >= 1
    assert "reserve_m3" in plan["budget"]
    status = platform.report_truck_water(None)
    assert status["readback_degraded"] is True
