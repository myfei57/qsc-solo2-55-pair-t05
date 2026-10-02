"""按脏度排清洗顺序、批次、水量与回站补水，以及中途重排。"""

from __future__ import annotations

import pytest

from cspplant.config import default_config
from cspplant.errors import InterlockError
from cspplant.wash.plan import (
    TruckCondition,
    WATER_MISSING,
    WATER_OK,
    WashScheduler,
    WaterReading,
    decide_shortage,
    packable_count,
    per_mirror_m3,
    usable_water_m3,
)


@pytest.fixture()
def scheduler():
    limits = default_config().wash
    sched = WashScheduler(limits)
    rows = [
        ("m1", 5, "a"), ("m2", 4, "b"), ("m3", 5, "a"), ("m4", 3, "a"),
        ("m5", 4, "a"), ("m6", 2, "b"), ("m7", 5, "b"), ("m8", 1, "a"),
        ("m9", 4, "a"), ("m10", 3, "b"),
    ]
    for name, grade, group in rows:
        sched.dirt.set(name, grade, group)
    return sched


def test_per_mirror_and_full_tank_pack():
    limits = default_config().wash
    per = per_mirror_m3(limits)
    assert per == pytest.approx(0.65)
    # 9 m³ 水、0.6 m³ 回站保底，留 8.4 m³ 可用 -> 12 面。
    assert packable_count(limits.truck_capacity_m3 - limits.return_reserve_m3, per) == 12


def test_order_is_grade_first_with_group_tiebreak(scheduler):
    order = [entry.name for entry in scheduler.dirt.order()]
    # 脏度降序：5 档先于 4 档，区域只在同档裁决。
    grades = [scheduler.dirt.get(name).grade for name in order]
    assert grades == sorted(grades, reverse=True)
    assert order[0] in {"m1", "m3", "m7"}  # 三朵 5 档
    # 同档（5）里同分区 a 的 m1/m3 排在 b 区 m7 之前。
    fives = [name for name in order if scheduler.dirt.get(name).grade == 5]
    assert fives.index("m7") == 2


def test_initial_plan_packs_by_gross_water_and_inserts_refill(scheduler):
    scheduler.plan(9.0)
    view = scheduler.plan_view()
    assert view["mirrors_per_full_tank"] == 12  # 10 面一車装得下
    assert view["batches_total"] == 1
    assert scheduler.consistency_violations() == []


def test_plan_spills_into_multiple_batches_with_refill():
    limits = default_config().wash
    sched = WashScheduler(limits)
    for i in range(1, 31):
        sched.dirt.set(f"m{i:03d}", (i % 5) + 1, "a")
    sched.plan(limits.truck_capacity_m3)
    view = sched.plan_view()
    sizes = [b["count"] for b in view["plan"]["batches"]]
    assert sizes == [12, 12, 6]
    kinds = [t["kind"] for t in view["plan"]["tasks"]]
    # 批与批之间有补水任务，第一批之前没有。
    assert kinds[0] == "wash"
    assert "refill" in kinds
    assert sched.consistency_violations() == []


def test_shrink_batch_when_water_covers_prefix(scheduler):
    scheduler.plan(9.0)
    task = scheduler.begin_next_mirror()
    assert task.mirror in {"m1", "m3", "m7"}
    # 洗完一面只剩 2.0 m³：可洗 2 面（(2.0-0.6)/0.65=2），缩减本批，后缀推迟。
    result = scheduler.complete_mirror(task.mirror, WaterReading(2.0, WATER_OK, True, 0))
    assert result["decision"]["action"] == "shrink_batch"
    assert result["decision"]["remaining_m3"] == 2
    current = scheduler.plan_view()["current_batch"]
    # 已完成 1 面 + 本车实测水还洗得动 2 面。
    assert len(current["mirrors"]) == 3
    assert current["state"] == "active"
    assert scheduler.consistency_violations() == []


def test_return_to_station_when_water_covers_none(scheduler):
    scheduler.plan(9.0)
    task = scheduler.begin_next_mirror()
    result = scheduler.complete_mirror(task.mirror, WaterReading(0.3, WATER_OK, True, 0))
    assert result["decision"]["action"] == "return_refill"
    assert scheduler.mode == "returning"
    view = scheduler.plan_view()
    assert view["next_action"] == "refill"
    # 队首是补水时不得开洗。
    with pytest.raises(InterlockError):
        scheduler.begin_next_mirror()
    assert scheduler.consistency_violations() == []


def test_missing_readback_is_partial_failure_treated_as_shortage(scheduler):
    scheduler.plan(9.0)
    task = scheduler.begin_next_mirror()
    result = scheduler.complete_mirror(task.mirror, WaterReading(0.0, WATER_MISSING, True, 0))
    # 回读缺失按不足处理：回站补水，绝不带未知水量继续洗。
    assert result["decision"]["action"] == "return_refill"
    assert result["decision"]["reading_status"] == WATER_MISSING
    assert scheduler.mode == "returning"
    assert scheduler.consistency_violations() == []
    with pytest.raises(InterlockError):
        scheduler.begin_next_mirror()


def test_report_water_missing_switches_to_returning():
    limits = default_config().wash
    sched = WashScheduler(limits)
    for i in range(20):
        sched.dirt.set(f"m{i:02d}", 3, "a")
    sched.plan(9.0)
    out = sched.report_water(TruckCondition(water_reading_ok=False, water_m3=None))
    assert out["reading"]["status"] == WATER_MISSING
    assert out["usable_m3"] == 0.0
    assert sched.mode == "returning"
    assert sched.consistency_violations() == []


def test_truck_fault_forces_return_even_if_water_remains(scheduler):
    scheduler.plan(9.0)
    scheduler.begin_next_mirror()
    # 泵坏了：即便水够也不能就地缩减，必须回站。
    out = scheduler.report_water(TruckCondition(water_m3=8.0, pump_ok=False))
    assert out["reading"]["truck_ok"] is False
    assert scheduler.mode == "returning"


def test_reschedule_keeps_done_and_active_order(scheduler):
    scheduler.plan(9.0)
    first = scheduler.begin_next_mirror()
    assert first.mirror != "m8"
    scheduler.complete_mirror(first.mirror, WaterReading(8.0, WATER_OK, True, 0))
    # 中途把原本最干净（1 档）的 m8 抬到最高 5 档且放在 z 区最前：它成为未洗队首。
    scheduler.set_dirt("m8", 5, "0", -999.0, 0.0)
    view = scheduler.plan_view()
    pending = [t["mirror"] for t in view["plan"]["tasks"] if t["kind"] == "wash" and t["state"] == "pending"]
    assert pending[0] == "m8"
    done = [t["mirror"] for t in view["plan"]["tasks"] if t["kind"] == "wash" and t["state"] == "done"]
    assert done == [first.mirror]
    assert scheduler.consistency_violations() == []


def test_run_to_completion_consistent():
    limits = default_config().wash
    sched = WashScheduler(limits)
    for i in range(1, 41):
        sched.dirt.set(f"m{i:03d}", (i % 5) + 1, "a" if i % 2 else "b")
    sched.plan(limits.truck_capacity_m3)
    water = limits.truck_capacity_m3
    refills = 0
    for _ in range(500):
        view = sched.plan_view()
        if view["active_mirror"] is None and not any(
            t["kind"] == "wash" and t["state"] == "pending" for t in view["plan"]["tasks"]
        ):
            break
        try:
            task = sched.begin_next_mirror()
        except InterlockError:
            sched.complete_refill(limits.truck_capacity_m3)
            refills += 1
            water = limits.truck_capacity_m3
            continue
        water = max(0.0, water - per_mirror_m3(limits))
        sched.complete_mirror(task.mirror, WaterReading(water, WATER_OK, True, 0))
    view = sched.plan_view()
    assert sum(1 for t in view["plan"]["tasks"] if t["kind"] == "wash" and t["state"] == "done") == 40
    assert not [t for t in view["plan"]["tasks"] if t["kind"] == "refill" and t["state"] == "pending"]
    assert refills >= 1
    assert sched.consistency_violations() == []


def test_decide_shortage_reading_missing_always_returns():
    limits = default_config().wash
    reading = WaterReading(0.0, WATER_MISSING, True, 0)
    decision = decide_shortage(reading, 5, per_mirror_m3(limits), limits)
    assert decision.returns_to_station is True
    assert decision.reason == "water_readback_missing"


def test_invalid_grade_rejected(scheduler):
    with pytest.raises(InterlockError):
        scheduler.dirt.set("mx", 6, "a")
    with pytest.raises(InterlockError):
        scheduler.dirt.set("my", 0, "a")
