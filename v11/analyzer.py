#!/usr/bin/env python3
"""
V11 分析层
===========
分数标定 + 多维度评估 + 参数稳定性检验

核心输出：分数标定表 — 每个排名段对应的历史交易统计。
"""

import json
import numpy as np
from typing import Dict, List
from collections import defaultdict
from dataclasses import dataclass, asdict

from v11.trade_sim import Trade


# ================================================================
# 分数标定
# ================================================================

@dataclass
class ScoreBucket:
    """一个分数段的统计"""
    percentile_range: str       # "0-10%"
    n_trades: int
    win_rate: float
    avg_return: float
    median_return: float
    std_return: float
    max_return: float
    min_return: float
    avg_hold_days: float
    profit_factor: float
    best_exit_rule: str         # 该段表现最好的退出规则
    exit_rule_stats: dict       # {exit_rule: {n, win_rate, avg_return}}


def calibrate_scores(trades: List[Trade], n_buckets: int = 10) -> List[ScoreBucket]:
    """
    按买入时的截面排名分桶，统计每个桶的交易特征。

    Args:
        trades: 交易列表
        n_buckets: 分桶数 (默认10个，每10%一个桶)

    Returns:
        按排名段排序的桶统计列表
    """
    if not trades:
        return []

    # 分桶
    buckets = defaultdict(list)
    for t in trades:
        if t.entry_percentile is None:
            continue
        bucket_idx = min(int(t.entry_percentile * n_buckets), n_buckets - 1)
        buckets[bucket_idx].append(t)

    result = []
    for bucket_idx in range(n_buckets):
        btrades = buckets.get(bucket_idx, [])
        if not btrades:
            result.append(ScoreBucket(
                percentile_range=f"{bucket_idx * 100//n_buckets}-{(bucket_idx+1) * 100//n_buckets}%",
                n_trades=0, win_rate=0, avg_return=0, median_return=0,
                std_return=0, max_return=0, min_return=0,
                avg_hold_days=0, profit_factor=0,
                best_exit_rule="n/a", exit_rule_stats={},
            ))
            continue

        returns = [t.return_pct for t in btrades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r < 0]

        # 按退出规则统计
        exit_stats = defaultdict(list)
        for t in btrades:
            exit_stats[t.exit_reason].append(t.return_pct)

        best_rule = "n/a"
        best_rule_avg = -999
        rule_details = {}
        for rule, rrets in exit_stats.items():
            avg_r = np.mean(rrets)
            wr = len([r for r in rrets if r > 0]) / len(rrets)
            rule_details[rule] = {"n": len(rrets), "win_rate": round(wr, 3), "avg_return": round(avg_r, 2)}
            if avg_r > best_rule_avg:
                best_rule_avg = avg_r
                best_rule = rule

        result.append(ScoreBucket(
            percentile_range=f"{bucket_idx * 100//n_buckets}-{(bucket_idx+1) * 100//n_buckets}%",
            n_trades=len(btrades),
            win_rate=round(len(wins) / len(btrades), 3),
            avg_return=round(np.mean(returns), 2),
            median_return=round(np.median(returns), 2),
            std_return=round(np.std(returns), 2),
            max_return=round(max(returns), 2),
            min_return=round(min(returns), 2),
            avg_hold_days=round(np.mean([t.holding_days for t in btrades]), 1),
            profit_factor=round(sum(r for r in returns if r > 0) / abs(sum(r for r in returns if r < 0)), 2)
                          if any(r < 0 for r in returns) else 99.0,
            best_exit_rule=best_rule,
            exit_rule_stats=rule_details,
        ))

    return result


def calibration_to_markdown(buckets: List[ScoreBucket]) -> str:
    """将标定表转为 Markdown"""
    lines = [
        "# 评分标定表",
        "",
        "> 每个排名段对应的历史交易统计。基于 Walk-Forward 回测的所有 out-of-sample 交易。",
        "",
        "| 截面排名 | 交易数 | 胜率 | 平均收益 | 中位收益 | 最大收益 | 最大亏损 | 平均持仓 | 盈亏比 | 最优退出 |",
        "|---------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|------|",
    ]

    for b in buckets:
        lines.append(
            f"| top {b.percentile_range} | {b.n_trades} | {b.win_rate:.0%} | "
            f"{b.avg_return:+.1f}% | {b.median_return:+.1f}% | "
            f"{b.max_return:+.1f}% | {b.min_return:+.1f}% | "
            f"{b.avg_hold_days}天 | {b.profit_factor:.1f} | {b.best_exit_rule} |"
        )

    # 策略建议
    lines.append("")
    lines.append("## 策略建议")
    lines.append("")

    for b in buckets:
        if b.n_trades == 0:
            continue
        if b.win_rate >= 0.6 and b.avg_return > 0:
            lines.append(f"- **top {b.percentile_range}**: ✅ 积极买入。胜率 {b.win_rate:.0%}，最优退出={b.best_exit_rule}")
        elif b.win_rate >= 0.5:
            lines.append(f"- **top {b.percentile_range}**: 🟡 谨慎买入。胜率 {b.win_rate:.0%}，需严格止损")
        elif b.avg_return < -1:
            lines.append(f"- **top {b.percentile_range}**: 🔴 不建议买入。平均亏损 {b.avg_return:+.1f}%")
        else:
            lines.append(f"- **top {b.percentile_range}**: ⚪ 中性。不具统计显著性")

    return "\n".join(lines)


# ================================================================
# 参数稳定性检验
# ================================================================

def check_param_stability(window_results: List[Dict]) -> Dict:
    """
    检查各窗口间的参数稳定性。
    如果某指标的标准差/均值 > 0.5，说明不稳定。
    """
    if len(window_results) < 2:
        return {"status": "insufficient_data", "message": "需要至少2个窗口"}

    metrics = ["win_rate", "avg_return", "n_trades"]
    stability = {}

    for metric in metrics:
        vals = [w.get(metric, 0) for w in window_results]
        vals = [v for v in vals if v != 0]
        if not vals:
            stability[metric] = {"mean": 0, "std": 0, "cv": 0, "stable": True}
            continue

        mean_v = np.mean(vals)
        std_v = np.std(vals)
        cv = std_v / mean_v if mean_v != 0 else 0

        stability[metric] = {
            "mean": round(float(mean_v), 4),
            "std": round(float(std_v), 4),
            "cv": round(float(cv), 4),
            "stable": cv < 0.5,
        }

    unstable = [k for k, v in stability.items() if not v["stable"]]
    stability["unstable_metrics"] = unstable
    stability["overall_stable"] = len(unstable) == 0

    return stability


# ================================================================
# 环境分解
# ================================================================

def regime_analysis(
    trades: List[Trade],
    market_regimes: Dict[str, str],  # {date: "bull"|"bear"|"range"}
) -> Dict:
    """
    按市场状态分段统计交易表现。

    Args:
        trades: 所有交易
        market_regimes: 每日市场状态 {date: regime}

    Returns:
        {regime: {n_trades, win_rate, avg_return, sharpe}}
    """
    regime_trades = defaultdict(list)
    for t in trades:
        # 用入场日期的市场状态
        regime = market_regimes.get(t.entry_date, "unknown")
        regime_trades[regime].append(t)

    result = {}
    for regime, rtrades in regime_trades.items():
        rets = [t.return_pct for t in rtrades]
        wins = [r for r in rets if r > 0]
        result[regime] = {
            "n_trades": len(rtrades),
            "win_rate": round(len(wins) / len(rtrades), 3) if rtrades else 0,
            "avg_return": round(np.mean(rets), 2) if rets else 0,
            "total_return": round(sum(rets), 2) if rets else 0,
        }

    return result


# ================================================================
# 报告生成
# ================================================================

def generate_full_report(wfo_report: Dict, output_dir: str = ".") -> Dict[str, str]:
    """
    生成完整的 V11 回测报告（JSON + Markdown）。

    Returns:
        {file_type: file_path}
    """
    import os

    # 1. JSON 报告
    json_path = os.path.join(output_dir, "v11_report.json")
    with open(json_path, "w") as f:
        json.dump(wfo_report, f, ensure_ascii=False, indent=2)

    # 2. 分数标定
    trades_data = wfo_report.get("trades", [])
    trades = [Trade(**t) if isinstance(t, dict) else t for t in trades_data]
    calibration = calibrate_scores([t for t in trades if isinstance(t, Trade)])
    calibration_md = calibration_to_markdown(calibration)

    md_path = os.path.join(output_dir, "v11_calibration.md")
    with open(md_path, "w") as f:
        f.write(calibration_md)

    # 3. 交易明细 CSV
    csv_path = os.path.join(output_dir, "v11_trades.csv")
    with open(csv_path, "w") as f:
        f.write("code,entry_date,exit_date,return_pct,holding_days,exit_reason,entry_percentile\n")
        for t_data in trades_data:
            if isinstance(t_data, dict):
                f.write(f"{t_data['code']},{t_data['entry_date']},{t_data['exit_date']},"
                       f"{t_data['return_pct']},{t_data['holding_days']},"
                       f"{t_data['exit_reason']},{t_data['entry_percentile']}\n")

    return {
        "json": json_path,
        "calibration_md": md_path,
        "trades_csv": csv_path,
    }
