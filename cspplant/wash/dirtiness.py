"""镜面脏度台账。

清洗排序的主键是"脏度"：脏得越厉害的镜子越先洗，区域连片只能在同一脏度带
内部优化路径，永远不允许用区域把低脏度镜子排到高脏度镜子前面——这条优先级
在初次排程与任何重排里都保持不变（见 :mod:`cspplant.wash.schedule`）。

脏度落在 0–1 闭区间（0 刚洗过、1 最脏），按固定阈值切成五档。分档结果是
排程、水量估算与控制台展示的共同口径；洗完一面镜子就把它回写到最干净档，
重排时自然沉底。
"""

from __future__ import annotations

import threading

# 脏度档自脏到净排列；索引 0 是最脏档，排程永远按这个顺序出镜子。
DIRT_BANDS: tuple[str, ...] = ("heavy", "soiled", "average", "light", "clean")

# 每档的含水上浮系数：越脏的镜子喷淋耗时／用水越多。水量预算按档取系数，
# 避免"脏镜和刚洗过的镜子按同一面值扣水"。
BAND_WATER_FACTOR: dict[str, float] = {
    "heavy": 1.30,
    "soiled": 1.15,
    "average": 1.00,
    "light": 0.90,
    "clean": 0.80,
}

# 分档上界（含），按脏度从高到低求值；最后一档兜底。
_BAND_CUTOFFS: tuple[tuple[str, float], ...] = (
    ("heavy", 0.80),
    ("soiled", 0.60),
    ("average", 0.40),
    ("light", 0.20),
)

# 清洗完成后回写的脏度（最干净档的带内值，不写成 0.0，避免落在分档歧义点上）。
WASHED_DIRTINESS = 0.05


def band_of(dirtiness: float) -> str:
    """把 0–1 的脏度映射到脏度档名。"""

    value = min(1.0, max(0.0, float(dirtiness)))
    for band, cutoff in _BAND_CUTOFFS:
        if value > cutoff:
            return band
    return "clean"


def band_rank(band: str) -> int:
    """返回脏度档的排序权重；值越小越脏、越先洗。未知档按最净处理。"""

    try:
        return DIRT_BANDS.index(band)
    except ValueError:
        return len(DIRT_BANDS) - 1


class DirtinessLedger:
    """按镜子登记脏度，供排程、水量估算与展示取同一份数。"""

    def __init__(self, default_dirtiness: float = 0.3) -> None:
        self._dirt: dict[str, float] = {}
        self._default = min(1.0, max(0.0, float(default_dirtiness)))
        self._lock = threading.RLock()

    def set(self, mirror: str, dirtiness: float) -> float:
        """登记／更新一面镜子的脏度。"""

        with self._lock:
            value = min(1.0, max(0.0, float(dirtiness)))
            self._dirt[str(mirror)] = value
            return value

    def update(self, readings: dict[str, float]) -> dict[str, str]:
        """批量更新脏度（重排输入），返回受影响镜子的新档名。"""

        changed: dict[str, str] = {}
        with self._lock:
            for mirror, value in readings.items():
                name = str(mirror)
                before = self.band(name)
                self.set(name, float(value))
                after = self.band(name)
                if after != before:
                    changed[name] = after
        return changed

    def get(self, mirror: str) -> float:
        """返回一面镜子的脏度（未登记取默认值）。"""

        with self._lock:
            return self._dirt.get(str(mirror), self._default)

    def band(self, mirror: str) -> str:
        """返回一面镜子当前的脏度档。"""

        return band_of(self.get(mirror))

    def mark_washed(self, mirror: str) -> float:
        """把刚洗完的镜子回写到最干净档。"""

        return self.set(mirror, WASHED_DIRTINESS)

    def rows(self, mirrors: list[str] | None = None) -> list[dict]:
        """返回脏度明细（脏度降序、同名次序稳定），供控制台展示。"""

        with self._lock:
            names = list(self._dirt) if mirrors is None else [str(item) for item in mirrors]
            ordered = sorted(
                dict.fromkeys(names),
                key=lambda name: (-self.get(name), name),
            )
            return [
                {
                    "mirror": name,
                    "dirtiness": round(self.get(name), 3),
                    "band": self.band(name),
                }
                for name in ordered
            ]

    def snapshot(self) -> dict[str, float]:
        """返回脏度原始值的拷贝（持久化用）。"""

        with self._lock:
            return dict(self._dirt)

    def restore(self, values: dict[str, float]) -> None:
        """从持久化文档恢复脏度台账。"""

        with self._lock:
            self._dirt = {str(name): min(1.0, max(0.0, float(value))) for name, value in values.items()}
