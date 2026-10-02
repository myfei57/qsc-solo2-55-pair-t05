"""镜面清洗编排：开窗口 → 重算避让区 → 按排程逐批清洗（含水车补水）→ 关窗口。

窗口一开，批次里的镜子就退出聚焦；避让区必须按新批次重新求值，否则镜子会停
在朝着工位检修区的姿态上。排程由 :class:`~cspplant.wash.schedule.WashScheduler`
给出：脏度优先排序、按一车水封批。每面镜子清洗本身仍走"到镜 → 喷淋 → 排水
回收 → 停泵"的硬顺序。

水车水量在每面之前读一次保守水位：水位回读缺失/过期属部分失败，按不足处理
（立即回站补水、回来重排）；水不够时由车况策略决定缩减本批还是回站补水后
重排。回站补水期间清洗窗口保持打开，镜子保持避让姿态。
"""

from __future__ import annotations

from ..field.field import FieldController
from ..wash.cycle import WashCycle
from ..wash.drain import DrainSystem
from ..wash.schedule import (
    CONTINUE,
    DONE,
    NEXT_BATCH,
    REFILL_AND_RESUME,
    REFILL_BEFORE_BATCH,
    TRIM_BATCH,
    WashScheduler,
)
from ..wash.truck import WashTruck


def _mirror_list(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _refill(
    steps: list[dict],
    truck: WashTruck,
    scheduler: WashScheduler,
    now: float,
    coords: dict,
    reason: str,
    batch_seq: int,
    *,
    trim: bool = False,
) -> None:
    """回站补水，并（可选）摘掉本批尾镜后只对未洗尾部重排。"""

    trimmed: list[str] = []
    if trim:
        trimmed = scheduler.trim_current_batch()
    steps.append(
        {
            "component": "wash",
            "action": "water_return_refill",
            "detail": {
                "batch_seq": batch_seq,
                "reason": reason,
                "deferred_mirrors": _mirror_list(trimmed),
                "deferred_count": len(trimmed),
            },
        }
    )
    # 先补水、再按补水后的保守水位重排；顺序反了会按低水位封批，每批只
    # 装得下一面，导致无谓的反复回站。
    truck.refill_to_full(ts=now)
    schedule = scheduler.replan(
        truck.effective_water_m3(now), coords,
        reason="after_trim_refill" if trim else "after_refill",
    )
    scheduler.advance_to_working_batch()
    steps.append(
        {
            "component": "wash",
            "action": "wash_schedule_replanned",
            "detail": {
                "campaign_id": schedule.campaign_id,
                "version": schedule.version,
                "reason": schedule.reason,
                "ordered_mirrors": _mirror_list(schedule.ordered_mirrors()),
            },
        }
    )


def wash_campaign(
    field: FieldController,
    scheduler: WashScheduler,
    cycle: WashCycle,
    truck: WashTruck,
    drain: DrainSystem,
    mirrors: list[str],
    zone: str,
    now: float,
    dt: float = 1.0,
    *,
    campaign_id: str = "campaign",
    readback_water_m3: float | None = None,
) -> dict:
    """按脏度排程跑完一个清洗战役，返回带步骤、批次账与违约项的报告。

    ``readback_water_m3`` 模拟车载水位传感器：给 ``None`` 即按回读缺失处理，
    编排只通过保守水位读水，回读缺失时按水量不足回站补水。
    """

    steps: list[dict] = []
    violations: list[str] = []
    batch_reports: list[dict] = []
    coords = field.mirror_coords()

    window = field.open_window(mirrors, zone, now)
    steps.append(
        {
            "component": "field",
            "action": "open_window",
            "detail": {"zone": window.zone, "mirror_count": len(window.mirrors)},
        }
    )
    refreshed = field.refresh_zones(now)
    steps.append(
        {"component": "field", "action": "refresh_zones", "detail": {"refreshed": len(refreshed)}}
    )
    if len(refreshed) != len(window.mirrors):
        violations.append("zone_refresh_incomplete")

    truck.report_water(readback_water_m3, now)
    schedule = scheduler.plan(
        campaign_id, list(window.mirrors), truck.effective_water_m3(now), coords
    )
    steps.append(
        {
            "component": "wash",
            "action": "wash_schedule_planned",
            "detail": {
                "campaign_id": schedule.campaign_id,
                "version": schedule.version,
                "batch_count": len(schedule.batches),
                "mirror_count": len(schedule.ordered_mirrors()),
                "ordered_mirrors": _mirror_list(schedule.ordered_mirrors()),
                "water_m3": round(truck.effective_water_m3(now), 4),
                "readback_degraded": truck.readback_degraded(now),
            },
        }
    )

    washed_total = 0
    refills = 0
    trims = 0
    degraded_events = 0
    scheduler.open_campaign()
    guard = 0

    while guard < 100_000:
        guard += 1
        batch = scheduler.current_batch()
        if batch is None or not scheduler.pending_mirrors():
            break

        # 批前水量门：不足或回读部分失败都先回站补满，回来只重排未洗尾部。
        degraded = truck.readback_degraded(now)
        if scheduler.before_batch(truck.effective_water_m3(now), degraded) == REFILL_BEFORE_BATCH:
            if degraded:
                degraded_events += 1
                violations.append("water_readback_degraded")
            refills += 1
            _refill(
                steps, truck, scheduler, now, coords,
                reason="readback_degraded" if degraded else "insufficient_before_batch",
                batch_seq=batch.seq,
            )
            continue

        batch_washed = 0
        batch_water_before = truck.water_m3()
        while True:
            batch = scheduler.current_batch()
            if batch is None:
                break
            pending_in_batch = [m for m in batch.mirrors if m not in scheduler.washed_mirrors()]
            if not pending_in_batch:
                break
            mirror = pending_in_batch[0]

            degraded = truck.readback_degraded(now)
            tail_in_batch = [m for m in batch.mirrors if m not in scheduler.washed_mirrors()]
            # 当前批之后、战役里还没排进本批的镜数（本批尾部之外的未洗镜）。
            campaign_left = max(0, len(scheduler.pending_mirrors()) - len(tail_in_batch))
            verdict = scheduler.before_next_mirror(
                truck.effective_water_m3(now), degraded, campaign_left
            )
            if degraded and degraded_events == 0:
                degraded_events += 1
                violations.append("water_readback_degraded")

            if verdict == REFILL_AND_RESUME:
                refills += 1
                _refill(
                    steps, truck, scheduler, now, coords,
                    reason="readback_degraded" if degraded else "insufficient_mid_batch",
                    batch_seq=batch.seq,
                )
                break
            if verdict == TRIM_BATCH:
                trims += 1
                refills += 1
                steps.append(
                    {
                        "component": "wash",
                        "action": "wash_batch_trimmed",
                        "detail": {"batch_seq": batch.seq},
                    }
                )
                _refill(
                    steps, truck, scheduler, now, coords,
                    reason="trim_then_refill", batch_seq=batch.seq, trim=True,
                )
                break
            if verdict == DONE:
                break

            report = cycle.run(truck, drain, mirror, dt)
            steps.extend(report.steps)
            violations.extend(report.violations)
            if report.ok:
                batch_washed += 1
                washed_total += 1
                # 洗完后的车载读数反映真实名义水位（回读健康时）；缺失场景
                # 补水后也以名义水位为准，不能再拿战役开始时的旧读数覆盖。
                if readback_water_m3 is not None or refills > 0:
                    truck.report_water(truck.water_m3(), now)
                scheduler.mark_washed(mirror)
                flow = scheduler.batch_state_after_mirror(
                    truck.effective_water_m3(now), truck.readback_degraded(now)
                )
                steps.append(
                    {
                        "component": "wash",
                        "action": "mirror_washed",
                        "detail": {
                            "mirror": mirror,
                            "used_m3": round(report.used_m3, 4),
                            "recovered_m3": round(report.recovered_m3, 4),
                            "water_m3": round(truck.water_m3(), 4),
                            "effective_water_m3": round(truck.effective_water_m3(now), 4),
                            "next": flow,
                        },
                    }
                )
                if flow == CONTINUE:
                    continue
                if flow == NEXT_BATCH:
                    scheduler.begin_next_batch()
                    break
                if flow == REFILL_BEFORE_BATCH:
                    refills += 1
                    _refill(
                        steps, truck, scheduler, now, coords,
                        reason="insufficient_before_batch", batch_seq=batch.seq + 1,
                    )
                    break
                if flow == DONE:
                    break
            else:
                # 单面清洗失败（联锁拒绝等）：不洗成不计入排程，跳出避免空转。
                break

        batch_reports.append(
            {
                "seq": batch.seq if batch else -1,
                "washed": batch_washed,
                "planned": len(batch.mirrors) if batch else 0,
                "water_before_m3": round(batch_water_before, 4),
                "water_after_m3": round(truck.water_m3(), 4),
            }
        )
        if not scheduler.pending_mirrors():
            break

    closed = field.close_window(now)
    steps.append(
        {
            "component": "field",
            "action": "close_window",
            "detail": {"zone": closed.zone, "closed_at": closed.closed_at},
        }
    )

    # 关键一致性检查：排程面数、实际洗完面数与审计步骤里的完成数必须对齐。
    planned_total = scheduler.campaign_total()
    audited_washed = sum(1 for step in steps if step["action"] == "mirror_washed")
    if washed_total != len(scheduler.washed_mirrors()) or washed_total != audited_washed:
        violations.append("wash_account_mismatch")
    if washed_total != planned_total:
        violations.append("wash_schedule_incomplete")

    return {
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "washed": washed_total,
        "mirrors": planned_total,
        "campaign_id": schedule.campaign_id,
        "schedule_version": schedule.version,
        "batches": batch_reports,
        "refills": refills,
        "trims": trims,
        "readback_degraded_events": degraded_events,
        "water": {
            "remaining_m3": round(truck.water_m3(), 4),
            "effective_m3": round(truck.effective_water_m3(now), 4),
            "readback_degraded": truck.readback_degraded(now),
            "refilled_m3": round(truck.refill_amount(), 4),
        },
    }


def wash_mirrors(
    field: FieldController,
    cycle: WashCycle,
    truck: WashTruck,
    drain: DrainSystem,
    mirrors: list[str],
    zone: str,
    now: float,
    dt: float = 1.0,
) -> dict:
    """兼容旧签名：构造临时脏度排程器跑一个脏度优先战役。"""

    from ..wash.dirtiness import DirtinessLedger

    scheduler = WashScheduler(truck.limits, DirtinessLedger())
    return wash_campaign(
        field, scheduler, cycle, truck, drain, mirrors, zone, now, dt
    )
