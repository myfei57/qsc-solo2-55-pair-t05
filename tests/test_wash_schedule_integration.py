"""脏度清洗排程与平台、避让窗口、审计、控制台的集成。"""

from __future__ import annotations

from cspplant.console.api import Api
from cspplant.console.pages import render_page


def _run_until_done(plant, *, inject_missing_after=None):
    """按排程把全部镜子洗完，缺水时回站补水。"""

    washed = 0
    refills = 0
    injected = False
    for _ in range(500):
        sched = plant.status()["wash_schedule"]
        pending = [t for t in sched["plan"]["tasks"] if t["kind"] == "wash" and t["state"] == "pending"]
        if sched["active_mirror"] is None and not pending:
            break
        if (
            inject_missing_after is not None
            and not injected
            and washed >= inject_missing_after
            and sched["next_action"] == "refill"
        ):
            plant.wash_refill()
            refills += 1
            injected = True
            continue
        report = plant.wash_step()
        if report.get("washed"):
            washed += 1
        if inject_missing_after is not None and not injected and washed == inject_missing_after:
            plant.wash_report_water(water_reading_ok=False)
        if report.get("suspended") == "returning":
            plant.wash_refill()
            refills += 1
    return washed, refills


def test_grade_plan_washes_all_in_batches_and_closes_window(platform):
    platform.start()
    platform.track(800.0, 720.0)
    dirt = [{"name": f"m{i:03d}", "grade": (i % 5) + 1} for i in range(1, 31)]
    result = platform.wash_plan(dirt, "maint-north")
    assert result["plan"]["mirrors_per_full_tank"] == 12
    assert len(platform.mirrors_in_window()) == 12  # 只避让当前批
    washed, refills = _run_until_done(platform)
    assert washed == 30
    assert refills == 2  # 12/12/6
    assert platform.mirrors_in_window() == []      # 全部洗完窗口关闭
    assert platform.truck.washes() == 30
    assert platform.status()["wash_consistency"] == []


def test_first_batch_ordered_by_dirt_grade(platform):
    platform.start()
    platform.track(800.0, 720.0)
    dirt = [{"name": f"m{i:03d}", "grade": i % 3 + 1} for i in range(1, 20)]
    platform.wash_plan(dirt, "maint-north")
    # 直接从排程批次取脏度，确认首批按脏度非递增。
    batch = next(b for b in platform.status()["wash_schedule"]["plan"]["batches"])
    assert batch["grades"] == sorted(batch["grades"], reverse=True)
    first_batch = platform.status()["wash_schedule"]["current_batch"]["mirrors"]
    assert first_batch == batch["mirrors"]


def test_missing_water_readback_is_partial_failure(platform):
    platform.start()
    platform.track(800.0, 720.0)
    dirt = [{"name": f"m{i:03d}", "grade": 3} for i in range(1, 26)]
    platform.wash_plan(dirt, "maint-north")
    platform.wash_step()
    platform.wash_step()
    out = platform.wash_report_water(water_reading_ok=False)
    assert out["reading"]["status"] == "missing"
    assert out["usable_m3"] == 0.0
    # 下一面被挂起，必须先补水，绝不带未知水量喷淋。
    suspended = platform.wash_step()
    assert suspended["suspended"] == "refill"
    assert platform.status()["wash_consistency"] == []


def test_midflight_dirt_change_reorders_only_pending(platform):
    platform.start()
    platform.track(800.0, 720.0)
    dirt = [{"name": f"m{i:03d}", "grade": 3} for i in range(1, 26)]
    platform.wash_plan(dirt, "maint-north")
    platform.wash_step()
    platform.wash_manager.scheduler.set_dirt("m025", 5, "c", 1.0, 2.0)
    sched = platform.status()["wash_schedule"]
    pending = [t["mirror"] for t in sched["plan"]["tasks"] if t["kind"] == "wash" and t["state"] == "pending"]
    assert pending[0] == "m025"
    # 已完成的那一面不受重排影响。
    done = [t["mirror"] for t in sched["plan"]["tasks"] if t["kind"] == "wash" and t["state"] == "done"]
    assert len(done) == 1
    assert platform.status()["wash_consistency"] == []


def test_wash_schedule_audited_and_served_by_console(platform):
    platform.start()
    platform.track(800.0, 720.0)
    dirt = [{"name": f"m{i:03d}", "grade": 4} for i in range(1, 14)]
    platform.wash_plan(dirt, "maint-north")
    _run_until_done(platform)
    actions = {event.action for event in platform.bus.history("wash")}
    assert "wash_plan_built" in actions
    assert "wash_reschedule" in actions
    # 审计流水里能看到重排事件。
    audit_actions = {entry["action"] for entry in platform.audit.entries(limit=400)}
    assert "wash_reschedule" in audit_actions

    api = Api(platform)
    status, payload = api.handle("GET", "/api/wash/plan", {}, {})
    assert status == 200
    assert payload["schedule"]["batch_policy"] == "grade_first"
    # 页面可渲染。
    html = render_page("/wash", platform)
    assert html is not None and "清洗控制台" in html


def test_wash_plan_api_rejects_empty_dirt(platform):
    platform.start()
    api = Api(platform)
    status, payload = api.handle("POST", "/api/wash/plan", {}, {"dirt": []})
    assert status == 409
    assert payload["component"] == "console"
