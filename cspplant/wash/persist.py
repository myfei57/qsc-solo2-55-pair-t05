"""清洗排程持久化。

跨重启必须保留的是脏度台账：它决定清洗次序与水量估值，丢了就会退回"固定
名单"。排程版本与执行游标也一并落盘，使控制台展示、审计与下次排程在重启
后仍按同一份口径继续。全部走 :class:`~cspplant.store.jsonfile.JsonStore`
原子文档。
"""

from __future__ import annotations

from ..store.jsonfile import JsonStore

DIRT_DOC = "wash/dirtiness"
SCHEDULE_DOC = "wash/schedule"


class WashPersistence:
    """清洗脏度与排程游标的读写入口。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    def write_dirtiness(self, values: dict[str, float]) -> str:
        """落盘脏度台账。"""

        payload = {str(name): round(min(1.0, max(0.0, float(value))), 4) for name, value in values.items()}
        return self._store.write(DIRT_DOC, payload)

    def read_dirtiness(self) -> dict[str, float] | None:
        """读回脏度台账；没有或损坏按不存在处理。"""

        raw = self._store.read(DIRT_DOC)
        if not isinstance(raw, dict):
            return None
        values: dict[str, float] = {}
        for name, value in raw.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values[str(name)] = min(1.0, max(0.0, float(value)))
        return values

    def write_schedule_state(self, state: dict) -> str:
        """落盘排程版本与执行游标。"""

        return self._store.write(SCHEDULE_DOC, state)

    def read_schedule_state(self) -> dict | None:
        """读回排程状态。"""

        raw = self._store.read(SCHEDULE_DOC)
        return raw if isinstance(raw, dict) else None

    def clear_schedule_state(self) -> bool:
        """战役结束后清掉游标文档。"""

        return self._store.remove(SCHEDULE_DOC)
