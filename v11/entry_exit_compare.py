#!/usr/bin/env python3
"""
买卖点优化对比 — ICIR 加权评分下，不同买卖规则的胜负

变体列表：
  基线 — top 15% 买入, -8% 固定止损, +15% 止盈, 信号走弱/20天退出

  买入优化:
    pullback   — top 15% + 当日涨幅 < 3% (不追高)
    direction  — top 15% + 当日收阳 (close > open)
    momentum   — top 15% + 评分趋势向上 (score > yesterday)
    reversal    — top 15% + 当日微跌(-3% ~ 0%) (买回调)

  止损优化:
    trailing   — 移动止损: 从买入后最高价回撤 8%
    scored     — 分级止损: top10%=12%, 10-20%=8%, 20-30%=5%
    tight       — 全部收紧到 -5%

  组合:
    best_combo — pullback + trailing
"""

import sys, os, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from stock_db import StockDB
from v11.factor_engine import (
    compute_all_factors, compute_factor_ic,
    FactorICTracker, FactorGraveyard, FACTOR_NAMES,
)
from v11.data_builder import compute_forward_returns
from scheme_comparison import ICIRWeightedScheme, _cross_sectional_normalize, _snapshot


# ================================================================
# 可配置的交易模拟器
# ================================================================

@dataclass
class Trade:
    code: str; entry_date: str; entry_price: float; exit_date: str; exit_price: float
    exit_reason: str; return_pct: float; holding_days: int; entry_percentile: float

@dataclass  
class Position:
    code: str; entry_date: str; entry_price: float; entry_percentile: float
    peak_price: float = 0  # 买入后最高价（移动止损用）


class ConfigurableSimulator:
    """可配置买卖规则的回测模拟器"""

    def __init__(self, config: dict):
        self.top_pct = config.get("top_pct", 0.15)
        self.max_positions = config.get("max_positions", 20)
        self.stop_loss_pct = config.get("stop_loss_pct", -8.0)
        self.take_profit_pct = config.get("take_profit_pct", 15.0)
        self.max_hold_days = config.get("max_hold_days", 20)
        self.signal_decay_pct = config.get("signal_decay_pct", 0.5)
        self.entry_mode = config.get("entry_mode", "baseline")
        self.exit_mode = config.get("exit_mode", "fixed")
        self.trailing_stop_pct = config.get("trailing_stop_pct", -8.0)
        self.scored_stops = config.get("scored_stops", {})

    def run(self, daily_scores, daily_prices, daily_returns=None):
        """daily_returns: {date: {code: return_pct}} 百分比, 如 2.5 表示涨 2.5%"""
        if daily_returns is None: daily_returns = {}
        positions: Dict[str, Position] = {}
        trades: List[Trade] = []
        cash = 1_000_000
        all_dates = sorted(daily_scores.keys())

        # 昨天的评分（给 momentum 模式用）
        prev_scores = {}

        for date in all_dates:
            prices = daily_prices.get(date, {})
            scores = daily_scores.get(date, {})
            returns = daily_returns.get(date, {})
            if not scores: continue

            # ===== 卖出检查 =====
            for code in list(positions.keys()):
                pos = positions[code]
                # 更新峰值
                cur_price = prices.get(code, pos.entry_price)
                if cur_price > pos.peak_price:
                    pos.peak_price = cur_price

                exit_signal = self._check_exit(pos, date, scores, prices)
                if exit_signal:
                    exit_price = prices.get(code, pos.entry_price)
                    holding_days = (datetime.strptime(date, "%Y-%m-%d") -
                                    datetime.strptime(pos.entry_date, "%Y-%m-%d")).days
                    trades.append(Trade(
                        code=code, entry_date=pos.entry_date, entry_price=pos.entry_price,
                        exit_date=date, exit_price=exit_price, exit_reason=exit_signal,
                        return_pct=round((exit_price/pos.entry_price-1)*100, 2),
                        holding_days=holding_days, entry_percentile=pos.entry_percentile,
                    ))
                    cash += 100 * exit_price
                    del positions[code]

            # ===== 买入筛选 =====
            buy_candidates = []
            for code, sr in scores.items():
                if code in positions: continue
                percentile = sr.get("percentile", 1.0)
                if percentile >= self.top_pct: continue
                if prices.get(code, 0) <= 0: continue

                # 买入条件过滤
                if self.entry_mode == "pullback":
                    ret = returns.get(code, 999)
                    if ret >= 3: continue  # 涨超3%不追
                elif self.entry_mode == "direction":
                    if returns.get(code, -999) <= 0: continue  # 必须收阳
                elif self.entry_mode == "momentum":
                    prev_pct = prev_scores.get(code, {}).get("percentile", 1.0)
                    if percentile >= prev_pct: continue  # 排名必须改善
                elif self.entry_mode == "reversal":
                    ret = returns.get(code, 999)
                    if not (-3 < ret < 0): continue  # 必须是微跌回调

                buy_candidates.append((code, sr))

            buy_candidates.sort(key=lambda x: x[1].get("percentile", 1.0))
            slots = self.max_positions - len(positions)

            for code, sr in buy_candidates[:slots]:
                bp = prices[code]
                if cash < bp * 100: continue
                positions[code] = Position(
                    code=code, entry_date=date, entry_price=bp,
                    entry_percentile=sr.get("percentile", 0), peak_price=bp,
                )
                cash -= 100 * bp

            prev_scores = scores

        return trades

    def _check_exit(self, pos: Position, date, scores, prices) -> Optional[str]:
        price = prices.get(pos.code)
        if not price or price <= 0: return None

        # 止损（按模式）
        if self.exit_mode == "trailing":
            loss = (price / pos.peak_price - 1) * 100
            if loss <= self.trailing_stop_pct:
                return "trailing_stop"
        elif self.exit_mode == "scored":
            pct = pos.entry_percentile
            if pct < 0.10: stop = -12
            elif pct < 0.20: stop = -8
            else: stop = -5
            if (price / pos.entry_price - 1) * 100 <= stop:
                return "scored_stop"
        elif self.exit_mode == "tight":
            if (price / pos.entry_price - 1) * 100 <= -5:
                return "tight_stop"
        else:  # fixed
            if (price / pos.entry_price - 1) * 100 <= self.stop_loss_pct:
                return "stop_loss"

        # 止盈
        if (price / pos.entry_price - 1) * 100 >= self.take_profit_pct:
            return "take_profit"

        # 信号走弱
        sr = scores.get(pos.code, {})
        if sr.get("percentile", 0) > self.signal_decay_pct:
            return "signal_decay"

        # 时间退出
        days = (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(pos.entry_date, "%Y-%m-%d")).days
        if days >= self.max_hold_days:
            return "time_exit"

        return None


# ================================================================
# 变体定义
# ================================================================

VARIANTS = {
    "baseline":    {"entry_mode": "baseline", "exit_mode": "fixed",  "stop_loss_pct": -8},
    "pullback":    {"entry_mode": "pullback", "exit_mode": "fixed",  "stop_loss_pct": -8},
    "direction":   {"entry_mode": "direction","exit_mode": "fixed",  "stop_loss_pct": -8},
    "momentum":    {"entry_mode": "momentum", "exit_mode": "fixed",  "stop_loss_pct": -8},
    "reversal":    {"entry_mode": "reversal", "exit_mode": "fixed",  "stop_loss_pct": -8},
    "trailing":    {"entry_mode": "baseline", "exit_mode": "trailing","trailing_stop_pct": -8},
    "scored":      {"entry_mode": "baseline", "exit_mode": "scored"},
    "tight":       {"entry_mode": "baseline", "exit_mode": "tight"},
    "best_combo":  {"entry_mode": "pullback", "exit_mode": "trailing","trailing_stop_pct": -8},
}


def compute_daily_returns(codes, klines_all, all_dates):
    """计算每日收益率 {date: {code: return_pct}}"""
    daily_rets = {}
    for date in all_dates:
        rets = {}
        for code in codes:
            kls = klines_all.get(code, [])
            last_two = [k for k in kls if k["date"] <= date][-2:]
            if len(last_two) >= 2 and last_two[-2]["close"] > 0:
                rets[code] = (last_two[-1]["close"] / last_two[-2]["close"] - 1) * 100
        if rets:
            daily_rets[date] = rets
    return daily_rets


def main():
    print("=" * 60)
    print("买卖点优化对比")
    print("=" * 60)

    # 加载数据
    db = StockDB()
    codes_file = os.path.join(os.path.dirname(__file__), "..", "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip()]
    klines_all = db.get_klines(codes, days=300)
    extra_all = db.get_extra_info(codes)
    forward_returns = compute_forward_returns(klines_all, codes)
    all_dates = sorted(set(k["date"] for kl in klines_all.values() for k in kl))
    daily_returns = compute_daily_returns(codes, klines_all, all_dates)

    # WFO 窗口
    train_size, test_size, purge = 200, 50, 5
    windows = []
    start = 0
    while start + train_size + purge + test_size <= len(all_dates):
        windows.append({
            "train": all_dates[start:start+train_size],
            "purge": all_dates[start+train_size:start+train_size+purge],
            "test": all_dates[start+train_size+purge:start+train_size+purge+test_size],
        })
        start += test_size
    print(f"WFO: train={train_size}, test={test_size} → {len(windows)}个窗口")

    # 预计算因子（所有方案共享）
    print("预计算因子...")
    all_test_factors = {}
    all_test_prices = {}

    for w in windows:
        # Train 窗口 — 计算因子 + IC
        train_factors = {}
        for date in w["train"]:
            snap = _snapshot(date, klines_all)
            if len(snap) < 30: continue
            fvals = compute_all_factors(snap, extra_all, today_str=date)
            if fvals: train_factors[date] = fvals

        # Test 窗口 — 预存评分
        for date in w["test"]:
            snap = _snapshot(date, klines_all)
            if len(snap) < 30: continue
            fvals = compute_all_factors(snap, extra_all, today_str=date)
            if not fvals: continue

            scheme = ICIRWeightedScheme()
            raw = scheme.score(fvals, FACTOR_NAMES)
            codes_list = list(raw.keys())
            vals = np.array([raw[c] for c in codes_list])
            si = np.argsort(vals)[::-1]
            n = len(si)
            all_test_factors[date] = {
                codes_list[i]: {"percentile": rank/n, "ensemble_z": float(vals[i])}
                for rank, i in enumerate(si)
            }
            all_test_prices[date] = {c: snap[c][-1]["close"] for c in snap}

    # 逐变体跑
    results = []
    for name, config in VARIANTS.items():
        sim = ConfigurableSimulator(config)
        t0 = time.time()
        trades = sim.run(all_test_factors, all_test_prices, daily_returns)
        elapsed = time.time() - t0

        rets = [t.return_pct for t in trades]
        wins = [r for r in rets if r > 0]
        stops = [t for t in trades if "stop" in t.exit_reason]

        result = {
            "name": name,
            "entry": config["entry_mode"],
            "exit": config["exit_mode"],
            "n_trades": len(trades),
            "win_rate": round(len(wins)/max(1,len(trades)), 3),
            "avg_return": round(np.mean(rets), 2) if rets else 0,
            "median_return": round(np.median(rets), 2) if rets else 0,
            "stop_rate": round(len(stops)/max(1,len(trades)), 3),
            "stop_avg_loss": round(np.mean([t.return_pct for t in stops]), 1) if stops else 0,
            "time": round(elapsed, 1),
        }
        results.append(result)
        print(f"  {name:15s}: {len(trades):>4}笔 胜率{result['win_rate']:.1%} "
              f"均{result['avg_return']:>+5.1f}% 止损率{result['stop_rate']:.0%} ({elapsed:.0f}s)")

    # 排序
    print(f"\n{'方案':<15} {'交易数':>5} {'胜率':>7} {'均收益':>7} {'中位':>7} {'止损率':>7} {'止损均亏':>9} {'买入策':>9} {'退出策':>9}")
    print("-" * 85)
    for r in sorted(results, key=lambda x: -x["win_rate"] * 10 - x["avg_return"]):
        print(f"{r['name']:<15} {r['n_trades']:>5} {r['win_rate']:>6.1%} "
              f"{r['avg_return']:>+6.1f}% {r['median_return']:>+6.1f}% "
              f"{r['stop_rate']:>6.0%} {r['stop_avg_loss']:>+9.1f}% "
              f"{r['entry']:>9} {r['exit']:>9}")

    best = max(results, key=lambda r: r["win_rate"] * 0.5 + r["avg_return"] * 0.01)
    print(f"\n🏆 最优: {best['name']} — 胜率{best['win_rate']:.1%} 均收益{best['avg_return']:+.1f}% 止损率{best['stop_rate']:.0%}")


if __name__ == "__main__":
    main()
