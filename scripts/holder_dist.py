"""统计「前十大流通股东合计持股占流通股比例」的真实分布。

用途：给 .env / README 里的阈值参考值提供实测依据（而不是拍脑袋写）。
数据源：`data/cache/top10_free_holding_*.json` 本地缓存，**不联网**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


def main() -> int:
    files = sorted(CACHE_DIR.glob("top10_free_holding_*.json"))
    if not files:
        print(f"没有缓存文件：{CACHE_DIR}")
        return 1

    path = files[-1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    print(f"缓存文件：{path.name}")
    print(f"顶层类型：{type(raw).__name__}")
    if isinstance(raw, dict):
        print(f"顶层键：{list(raw)[:6]}")

    ratios = raw
    if isinstance(raw, dict):
        for key in ("ratios", "data", "items"):
            if key in raw:
                ratios = raw[key]
                break

    values: list[float] = []
    if isinstance(ratios, dict):
        for v in ratios.values():
            if isinstance(v, (int, float)):
                values.append(float(v))
            elif isinstance(v, dict):  # 形如 {symbol: {"ratio": x}}
                for inner in v.values():
                    if isinstance(inner, (int, float)):
                        values.append(float(inner))
                        break
    elif isinstance(ratios, list):
        values = [float(v) for v in ratios if isinstance(v, (int, float))]

    if not values:
        print("没解析出数值，请检查缓存结构。")
        return 1

    values.sort()
    n = len(values)
    print(f"\n股票数：{n}")
    for q in (10, 25, 50, 75, 90):
        print(f"  P{q:<3} = {values[max(0, int(n * q / 100) - 1)]:.1f}%")

    print("\n阈值 -> 保留比例（仅按筹码维度）")
    for thr in (25, 30, 35, 40, 45, 50, 55, 60, 70):
        keep = sum(1 for v in values if v >= thr) / n
        print(f"  >={thr}%  ->  {keep * 100:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
