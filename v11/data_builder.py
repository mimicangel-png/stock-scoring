#!/usr/bin/env python3
"""
V11 Point-in-Time 数据构建器
============================
一次构建，多次复用。为每个历史日期生成数据快照，严格保证
无前瞻偏差：快照中只包含该日期及之前的数据。

使用 stock_db.py 的 SQLite 缓存 + 全量预计算。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stock_db import StockDB
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import json


class PoTDataBuilder:
    """Point-in-Time 数据构建器"""

    def __init__(self, db: StockDB = None):
        self.db = db or StockDB()
        self._klines_cache: Optional[Dict[str, List[dict]]] = None
        self._all_dates: Optional[List[str]] = None

    def build(self, codes: List[str], lookback_days: int = 300) -> Dict[str, dict]:
        """
        构建全量 PoT 快照。

        Returns:
            {
                "2025-01-15": {
                    "klines": {code: [...截至该日的K线...]},
                    "extra": {code: {行情快照}},
                    "events": {code: [公告]},
                    "fund_flow": {code: {资金流}},
                },
                ...
            }
        """
        print(f"[PoT] 构建快照，{len(codes)}只股票，回溯{lookback_days}天...")

        # 1. 拉取全量K线
        all_klines = self.db.get_klines(codes, days=lookback_days)
        print(f"  K线加载完成: {len(all_klines)}只")

        # 2. 收集所有日期
        self._all_dates = sorted(set(
            k["date"] for klines in all_klines.values() for k in klines
        ))
        print(f"  总日期数: {len(self._all_dates)}")

        # 只保留最近 lookback_days 个交易日
        self._all_dates = self._all_dates[-lookback_days:]

        # 3. 按日期索引K线
        kline_by_date = defaultdict(dict)
        for code, klines in all_klines.items():
            for k in klines:
                kline_by_date[k["date"]][code] = k

        # 4. 为每个日期构建快照
        snapshots = {}
        for i, date in enumerate(self._all_dates):
            if i < 60:  # 前60天K线不够，跳过
                continue

            # K线：date及之前的所有数据
            date_klines = {}
            for prev_i in range(i + 1):
                prev_date = self._all_dates[prev_i]
                for code, k in kline_by_date.get(prev_date, {}).items():
                    if code not in date_klines:
                        date_klines[code] = []
                    date_klines[code].append(k)

            snapshots[date] = {
                "klines": date_klines,
                "date": date,
            }

            if i % 50 == 0:
                print(f"  快照: {i}/{len(self._all_dates)} ({date})")

        print(f"  PoT快照构建完成: {len(snapshots)}个交易日")
        return snapshots

    @staticmethod
    def save(snapshots: dict, path: str):
        """保存快照到JSON文件（仅保存日期和股票代码列表，K线数据从DB读取）"""
        # 只保存元数据，K线数据从DB实时读取
        meta = {
            "dates": sorted(snapshots.keys()),
            "created_at": datetime.now().isoformat(),
        }
        with open(path, "w") as f:
            json.dump(meta, f, ensure_ascii=False)
        print(f"[PoT] 元数据已保存: {path} ({len(meta['dates'])}天)")

    @staticmethod
    def load(db: StockDB, path: str) -> List[str]:
        """加载日期列表"""
        with open(path) as f:
            meta = json.load(f)
        return meta["dates"]


# ================================================================
# 前向收益预计算
# ================================================================

def compute_forward_returns(
    all_klines: Dict[str, List[dict]],
    codes: List[str],
    horizons: List[int] = [1, 5, 10, 20],
) -> Dict[str, Dict[str, Dict[int, float]]]:
    """
    预计算所有日期、所有股票的T+N前向收益。

    Returns:
        {date: {code: {horizon: return_pct}}}
    """
    print(f"[FWD] 预计算前向收益 (horizons={horizons})...")

    # 收集所有日期
    all_dates_set = set()
    for klines in all_klines.values():
        for k in klines:
            all_dates_set.add(k["date"])
    all_dates = sorted(all_dates_set)

    # 日期 → 索引
    date_idx = {d: i for i, d in enumerate(all_dates)}

    # 日期 → {code: close}
    close_by_date = defaultdict(dict)
    for code, klines in all_klines.items():
        for k in klines:
            close_by_date[k["date"]][code] = k["close"]

    # 计算前向收益
    forward_rets = {}
    for i, date in enumerate(all_dates):
        fwd = defaultdict(dict)
        for code in codes:
            curr_close = close_by_date.get(date, {}).get(code)
            if curr_close is None or curr_close <= 0:
                continue
            for h in horizons:
                fwd_idx = i + h
                if fwd_idx >= len(all_dates):
                    continue
                fwd_date = all_dates[fwd_idx]
                fwd_close = close_by_date.get(fwd_date, {}).get(code)
                if fwd_close is not None and fwd_close > 0:
                    fwd[code][h] = (fwd_close / curr_close - 1) * 100
        forward_rets[date] = dict(fwd)

    print(f"  前向收益计算完成: {len(forward_rets)}天")
    return forward_rets
