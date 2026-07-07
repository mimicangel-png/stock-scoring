#!/usr/bin/env python3
"""
V11 多方案对比回测
====================
对比三种评分方案的 Walk-Forward 表现：
  1. GLM 集成 (多周期 Walk-Forward Ridge) — V11 当前方案
  2. 等权组合 (所有活跃因子等权)
  3. ICIR 加权 (基于第一次回测的因子 ICIR 作为初始权重)

输出：胜率、平均收益、Sharpe、因子存活状态对比。
"""

import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np
from stock_db import StockDB
from v11.factor_engine import (
    compute_all_factors, compute_factor_ic,
    FactorICTracker, FactorGraveyard, FACTOR_NAMES, FACTOR_REGISTRY,
)
from v11.glm_model import MultiPeriodGLM
from v11.trade_sim import TradeSimulator
from v11.data_builder import compute_forward_returns


# ================================================================
# 基于第一次回测结果的因子初始权重
# ================================================================

# ICIR来自第一次回测：ICIR > 0.05 的因子加权
# v3.0: 原始权重
ICIR_WEIGHTS_V3 = {
    "turnover_z":      0.451,   # strongest
    "log_mcap":        0.162,
    "mfi":             0.153,
    "pct_52w":         0.091,
    "pe_percentile":   0.078,
    "pb_percentile":   0.078,
    "gap_open":        0.072,
    "max_dd_20d":      0.052,
    "ma_trend":        0.030,
    "rsi_signal":      0.028,
    "macd_signal":     0.026,
    "cmf":             0.025,
    "vwap_premium":    0.022,
    "event_score":     0.020,
    "event_count":     0.018,
    "sector_rsi":      0.015,
    "sector_momentum": 0.012,
    "inflow_rate":     0.010,
    "main_flow_5d":    0.008,
    "main_flow_20d":   0.006,
    "amplitude_z":     0.005,
    "ret_20d":         0.004,
    "volatility_20d":  0.003,
    "ma_bull":         0.029,
    "vol_price":       0.025,
    "dev_ma20":        0.024,
    "vol_ratio_5d":    0.023,
    "ret_5d":          0.021,
    "streak":          0.020,
    "roe_rank":        0.001,    # 无数据
    "gross_margin_rank":0.001,
    "ocf_ratio_rank":  0.001,
}

# v4.0: mfi/pct_52w 取反 (基于 GLM 方向矛盾诊断)
ICIR_WEIGHTS_V4 = dict(ICIR_WEIGHTS_V3)
ICIR_WEIGHTS_V4["mfi"] = -0.153
ICIR_WEIGHTS_V4["pct_52w"] = -0.091

# 兼容旧引用
ICIR_WEIGHTS = ICIR_WEIGHTS_V3

# 归一化 (v3 和 v4 各自归一化)
_total_v3 = sum(abs(w) for w in ICIR_WEIGHTS_V3.values())
ICIR_NORMALIZED_V3 = {k: v / _total_v3 for k, v in ICIR_WEIGHTS_V3.items()}
_total_v4 = sum(abs(w) for w in ICIR_WEIGHTS_V4.values())
ICIR_NORMALIZED_V4 = {k: v / _total_v4 for k, v in ICIR_WEIGHTS_V4.items()}
ICIR_NORMALIZED = ICIR_NORMALIZED_V3  # 默认 v3


# ================================================================
# 评分方案
# ================================================================

class ScoringScheme:
    """评分方案基类"""
    name: str = ""

    def score(self, factor_values: dict, active_factors: list) -> dict:
        """{code: score}，score 越大越好"""
        raise NotImplementedError


class GLMEnsembleScheme(ScoringScheme):
    """GLM 三周期集成 (Walk-Forward)"""
    name = "GLM集成"

    def __init__(self):
        self.glm = MultiPeriodGLM()

    def train(self, train_factors, forward_returns, train_end, active_factors):
        return self.glm.train(train_factors, forward_returns, train_end, active_factors)

    def score(self, factor_values, active_factors=None):
        predictions = self.glm.predict(factor_values)
        return {
            code: pred.get("ensemble_z", 0)
            for code, pred in predictions.items()
        }


class EqualWeightScheme(ScoringScheme):
    """等权组合"""
    name = "等权组合"

    def score(self, factor_values, active_factors):
        scores = {}
        for code, fvals in factor_values.items():
            vals = [fvals.get(fname, 0) for fname in active_factors]
            scores[code] = np.mean(vals) if vals else 0
        return _cross_sectional_normalize(scores)


class ICIRWeightedScheme(ScoringScheme):
    """ICIR 加权 (基于预标定的因子有效性)"""
    def __init__(self, weights_dict=None, name="ICIR加权"):
        self.name = name
        self.weights = weights_dict or ICIR_NORMALIZED

    def score(self, factor_values, active_factors):
        scores = {}
        for code, fvals in factor_values.items():
            weighted = 0
            for fname in active_factors:
                w = self.weights.get(fname, 0.01)
                weighted += w * fvals.get(fname, 0)
            scores[code] = weighted
        return _cross_sectional_normalize(scores)


def _cross_sectional_normalize(raw_scores):
    """截面标准化"""
    if not raw_scores:
        return raw_scores
    codes = list(raw_scores.keys())
    vals = np.array([raw_scores[c] for c in codes])
    mu, sigma = np.mean(vals), max(0.001, np.std(vals))
    z = (vals - mu) / sigma
    return {c: float(z[i]) for i, c in enumerate(codes)}


# ================================================================
# 单方案回测
# ================================================================

def run_scheme_backtest(scheme, codes, klines_all, extra_all, forward_returns, windows):
    """运行单个方案的 Walk-Forward 回测"""
    factor_trackers = {name: FactorICTracker(name) for name in FACTOR_NAMES}
    graveyard = FactorGraveyard(icir_threshold=0.05, inactive_windows=3)
    simulator = TradeSimulator(top_pct=0.15, max_positions=20)
    all_trades = []

    for w in windows:
        # ---- Train: 因子计算 + IC ----
        train_factors = {}
        for date in w["train"]:
            snapshot = _snapshot(date, klines_all)
            if len(snapshot) < 30: continue
            fvals = compute_all_factors(snapshot, extra_all, today_str=date)
            if not fvals: continue
            train_factors[date] = fvals

        # IC 追踪
        for date in w["train"]:
            fvals = train_factors.get(date, {})
            fwd = forward_returns.get(date, {})
            for h in [1, 5, 10]:
                ics = compute_factor_ic(fvals, fwd, horizon=h)
                for fname, ic in ics.items():
                    if fname in factor_trackers:
                        factor_trackers[fname].record_ic(h, ic)

        graveyard.evaluate(factor_trackers)
        active = graveyard.get_active_names()

        # ---- Train GLM (仅 GLM 方案) ----
        if isinstance(scheme, GLMEnsembleScheme):
            scheme.train(train_factors, forward_returns, w["test"][0], active)

        # ---- Test: 评分 + 交易 ----
        test_scores = {}
        test_prices = {}

        for date in w["test"]:
            snapshot = _snapshot(date, klines_all)
            if len(snapshot) < 30: continue
            fvals = compute_all_factors(snapshot, extra_all, today_str=date)
            if not fvals: continue

            raw_scores = scheme.score(fvals, active)
            # 转成 trader 需要的格式 {code: {percentile, ensemble_z}}
            codes_list = list(raw_scores.keys())
            vals = np.array([raw_scores[c] for c in codes_list])
            sorted_idx = np.argsort(vals)[::-1]
            n = len(sorted_idx)
            test_scores[date] = {
                codes_list[si]: {
                    "percentile": rank / n,
                    "ensemble_z": float(vals[si]),
                }
                for rank, si in enumerate(sorted_idx)
            }
            test_prices[date] = {
                c: snapshot[c][-1]["close"] for c in snapshot
            }

        trades, _ = simulator.run(test_scores, test_prices)
        all_trades.extend(trades)

    # 汇总
    returns = [t.return_pct for t in all_trades]
    wins = [r for r in returns if r > 0]
    return {
        "scheme": scheme.name,
        "n_trades": len(all_trades),
        "win_rate": round(len(wins) / max(1, len(all_trades)), 3),
        "avg_return": round(np.mean(returns), 2) if returns else 0,
        "median_return": round(np.median(returns), 2) if returns else 0,
        "max_return": round(max(returns), 2) if returns else 0,
        "min_return": round(min(returns), 2) if returns else 0,
        "active_factors": len(active),
        "graveyard": graveyard.status_report()["graveyard"],
        "factor_icir": {name: t.get_weighted_icir() for name, t in factor_trackers.items()},
        "n_trades_per_window": [],  # filled below
    }


# ================================================================
# 辅助函数
# ================================================================

def _snapshot(date, klines_all):
    snap = {}
    for code, kls in klines_all.items():
        filtered = [k for k in kls if k["date"] <= date]
        if filtered: snap[code] = filtered
    return snap


# ================================================================
# 主流程
# ================================================================

def main():
    print("=" * 60)
    print("V11 多方案对比回测")
    print("=" * 60)

    # ====== 1. 加载数据 ======
    print("\n[1] 加载数据...")
    db = StockDB()
    codes_file = os.path.join(os.path.dirname(__file__), "..", "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip()]

    klines_all = db.get_klines(codes, days=300)
    extra_all = db.get_extra_info(codes)

    all_dates = sorted(set(k["date"] for kl in klines_all.values() for k in kl))
    print(f"  股票池: {len(codes)}, 日期: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")

    # ====== 2. 前向收益 ======
    print("\n[2] 前向收益...")
    forward_returns = compute_forward_returns(klines_all, codes)

    # ====== 3. 窗口（用更大的训练窗口） ======
    print("\n[3] WFO 窗口...")
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
    print(f"  配置: train={train_size}, purge={purge}, test={test_size} → {len(windows)}个窗口")
    for i, w in enumerate(windows):
        print(f"    W{i}: train={w['train'][0]}~{w['train'][-1]}, test={w['test'][0]}~{w['test'][-1]}")

    # ====== 4. 逐个方案跑 ======
    schemes = [
        GLMEnsembleScheme(),
        EqualWeightScheme(),
        ICIRWeightedScheme(ICIR_NORMALIZED_V3, "ICIR-v3(原版)"),
        ICIRWeightedScheme(ICIR_NORMALIZED_V4, "ICIR-v4(mfi反)"),
    ]

    results = []
    for scheme in schemes:
        print(f"\n[4] 方案: {scheme.name}")
        t0 = time.time()
        result = run_scheme_backtest(scheme, codes, klines_all, extra_all, forward_returns, windows)
        elapsed = time.time() - t0
        print(f"  交易: {result['n_trades']}笔, 胜率{result['win_rate']:.1%}, 平均{result['avg_return']:+.1f}%")
        print(f"  活跃因子: {result['active_factors']}/33")
        if result["graveyard"]:
            print(f"  墓地: {result['graveyard'][:5]}{'...' if len(result['graveyard'])>5 else ''}")
        print(f"  耗时: {elapsed:.0f}s")
        results.append(result)

    # ====== 5. 对比输出 ======
    print("\n" + "=" * 60)
    print("方案对比")
    print("=" * 60)

    print(f"{'方案':<12} {'交易数':>6} {'胜率':>7} {'均收益':>8} {'中位':>7} {'最大':>7} {'最小':>7} {'活跃因子':>8}")
    print("-" * 65)
    for r in results:
        print(f"{r['scheme']:<12} {r['n_trades']:>6} {r['win_rate']:>6.1%} "
              f"{r['avg_return']:>+7.1f}% {r['median_return']:>+6.1f}% "
              f"{r['max_return']:>+6.1f}% {r['min_return']:>+6.1f}% "
              f"{r['active_factors']:>8}")

    # 最佳方案
    best = max(results, key=lambda r: r["win_rate"] * 0.5 + r["avg_return"] * 0.01)
    print(f"\n🏆 最优方案: {best['scheme']} (胜率{best['win_rate']:.1%}, 均收益{best['avg_return']:+.1f}%)")

    # ====== 6. 保存 ======
    output_dir = os.path.join(os.path.dirname(__file__), "..", "output", "v11")
    os.makedirs(output_dir, exist_ok=True)

    output = {
        "config": {"train_size": train_size, "test_size": test_size, "purge": purge,
                   "n_windows": len(windows)},
        "results": results,
        "best": best["scheme"],
    }
    json_path = os.path.join(output_dir, "scheme_comparison.json")
    with open(json_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {json_path}")


if __name__ == "__main__":
    main()
