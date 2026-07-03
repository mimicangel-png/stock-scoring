#!/usr/bin/env python3
"""
V11 快速回测脚本 — 适配当前数据量
资源有限（~200交易日），用小窗口跑 WFO，验证因子+GLM的有效性。
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np
from stock_db import StockDB
from v11.factor_engine import (
    compute_all_factors, compute_factor_ic,
    FactorICTracker, FactorGraveyard, FACTOR_NAMES, FACTOR_REGISTRY,
)
from v11.glm_model import MultiPeriodGLM, GLM_CONFIG
from v11.trade_sim import TradeSimulator, Trade
from v11.data_builder import compute_forward_returns


def main():
    print("=" * 60)
    print("V11 快速回测")
    print("=" * 60)

    # ====== 1. 加载数据 ======
    print("\n[1] 加载数据...")
    db = StockDB()
    codes_file = os.path.join(os.path.dirname(__file__), "..", "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip()]

    print(f"  股票池: {len(codes)} 只")

    klines_all = db.get_klines(codes, days=300)
    extra_all = db.get_extra_info(codes)

    n_with_data = len(klines_all)
    all_dates = sorted(set(k["date"] for kl in klines_all.values() for k in kl))
    print(f"  有K线: {n_with_data}/{len(codes)} 只")
    print(f"  日期: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)}天)")

    # ====== 2. 预计算前向收益 ======
    print("\n[2] 预计算前向收益...")
    forward_returns = compute_forward_returns(klines_all, codes)
    print(f"  前向收益: {len(forward_returns)}天")

    # ====== 3. 小窗口 WFO（数据量有限） ======
    print("\n[3] 运行 WFO...")
    train_size, test_size, purge = 100, 50, 5
    print(f"  配置: train={train_size}, purge={purge}, test={test_size}")

    # 生成窗口
    windows = []
    start = 0
    min_train = 60
    while start + train_size + purge + test_size <= len(all_dates):
        windows.append({
            "train": all_dates[start:start+train_size],
            "purge": all_dates[start+train_size:start+train_size+purge],
            "test": all_dates[start+train_size+purge:start+train_size+purge+test_size],
            "idx": len(windows),
        })
        start += test_size

    print(f"  窗口数: {len(windows)}")
    for w in windows:
        print(f"    W{w['idx']}: train={w['train'][0]}~{w['train'][-1]}, test={w['test'][0]}~{w['test'][-1]}")

    # ====== 逐窗口执行 ======
    factor_trackers = {name: FactorICTracker(name) for name in FACTOR_NAMES}
    graveyard = FactorGraveyard(icir_threshold=0.05, inactive_windows=3)
    glm = MultiPeriodGLM()
    simulator = TradeSimulator(top_pct=0.15, max_positions=20)
    all_trades = []
    all_nav = []

    for w in windows:
        print(f"\n--- W{w['idx']} ---")
        print(f"  Train: {w['train'][0]} ~ {w['train'][-1]}")

        # ---- 3a. Train窗口：计算因子 + 记录IC ----
        train_factors = {}
        for i, date in enumerate(w["train"]):
            snapshot = _build_snapshot(date, klines_all)
            if len(snapshot) < 30: continue
            fvals = compute_all_factors(snapshot, extra_all, today_str=date)
            if not fvals: continue
            train_factors[date] = fvals

            if i % 20 == 0:
                print(f"    [factor] {i}/{len(w['train'])} ({date})")

        # 记录因子IC
        for date in w["train"]:
            fvals = train_factors.get(date, {})
            fwd = forward_returns.get(date, {})
            for h in [1, 5, 10]:
                ics = compute_factor_ic(fvals, fwd, horizon=h)
                for fname, ic in ics.items():
                    if fname in factor_trackers:
                        factor_trackers[fname].record_ic(h, ic)

        # 因子墓地
        removed = graveyard.evaluate(factor_trackers)
        if removed: print(f"  ⚠️ 移除: {removed}")
        active = graveyard.get_active_names()
        print(f"  活跃: {len(active)}/{len(FACTOR_NAMES)}")

        # ---- 3b. 训练 GLM ----
        print(f"  [GLM] 训练...")
        glm = MultiPeriodGLM()
        glm_results = glm.train(train_factors, forward_returns, w["test"][0], active)
        for p, r in glm_results.items():
            if r["train_n"] > 0:
                print(f"    {p}: n={r['train_n']}, IC={r['train_ic']:.4f}")

        # ---- 3c. Test窗口：评分 + 模拟交易 ----
        print(f"  [Trade] 模拟交易...")
        test_scores = {}
        test_prices = {}

        for date in w["test"]:
            snapshot = _build_snapshot(date, klines_all)
            if len(snapshot) < 30: continue
            fvals = compute_all_factors(snapshot, extra_all, today_str=date)
            if not fvals: continue
            scores = glm.predict(fvals)
            test_scores[date] = scores
            test_prices[date] = {c: snapshot[c][-1]["close"] for c in snapshot}

        trades, nav = simulator.run(test_scores, test_prices)
        all_trades.extend(trades)
        all_nav.extend(nav)

        wr = len([t for t in trades if t.return_pct>0]) / max(1, len(trades))
        ar = np.mean([t.return_pct for t in trades]) if trades else 0
        print(f"    结果: {len(trades)}笔, 胜率{wr:.1%}, 平均{ar:+.1f}%")

    # ====== 4. 汇总 ======
    print("\n" + "=" * 60)
    print("回测结果")
    print("=" * 60)

    returns = [t.return_pct for t in all_trades]
    wins = [r for r in returns if r > 0]

    print(f"\n总交易: {len(all_trades)} 笔")
    print(f"胜率:   {len(wins)/max(1,len(all_trades)):.1%}")
    print(f"平均收益: {np.mean(returns):+.2f}%" if returns else "N/A")
    print(f"中位收益: {np.median(returns):+.2f}%" if returns else "N/A")

    if returns:
        sharpe = np.mean(returns) / max(0.01, np.std(returns)) * np.sqrt(252 / 10)
        print(f"Sharpe (ann. approx): {sharpe:.2f}")

    # 因子排名
    print(f"\n--- 因子 ICIR 排名 ---")
    icirs = [(n, t.get_weighted_icir()) for n, t in factor_trackers.items()]
    icirs.sort(key=lambda x: -x[1])
    for name, icir in icirs[:15]:
        flag = "💀" if name not in active else "🟢"
        print(f"  {flag} {name:20s}: ICIR={icir:.3f}")

    # 分数标定
    print(f"\n--- 分数标定 (按买入排名分桶) ---")
    buckets = {}
    for t in all_trades:
        b = min(int(t.entry_percentile * 10), 9)
        if b not in buckets: buckets[b] = []
        buckets[b].append(t.return_pct)

    for b in sorted(buckets.keys()):
        rets = buckets[b]
        wr = len([r for r in rets if r > 0]) / len(rets)
        print(f"  top {b*10}-{(b+1)*10}%: {len(rets)}笔, 胜率{wr:.0%}, 平均{np.mean(rets):+.1f}%")
        if b == 0 and wr >= 0.55:
            print(f"    ✅ top 10% 信号有效!")
        elif b == 0 and wr < 0.50:
            print(f"    ❌ top 10% 信号不可靠，需要改进")

    # 退出原因统计
    print(f"\n--- 退出原因分析 ---")
    exit_stats = {}
    for t in all_trades:
        r = t.exit_reason
        if r not in exit_stats: exit_stats[r] = []
        exit_stats[r].append(t.return_pct)
    for reason, rets in exit_stats.items():
        avg = np.mean(rets)
        median = np.median(rets)
        print(f"  {reason:15s}: {len(rets)}笔, 平均{avg:+.1f}%, 中位{median:+.1f}%")

    # 保存结果
    result = {
        "n_total": len(all_trades),
        "win_rate": round(len(wins)/max(1,len(all_trades)), 3),
        "avg_return": round(float(np.mean(returns)), 2) if returns else 0,
        "sharpe": round(float(sharpe), 2) if returns else 0,
        "factor_icir": {n: round(icir,3) for n, icir in icirs},
        "graveyard": graveyard.status_report(),
        "glm_mid_weights": glm.get_factor_weights("mid"),
        "trades": [{
            "code": t.code, "entry_date": t.entry_date,
            "return_pct": t.return_pct, "exit_reason": t.exit_reason,
            "entry_percentile": t.entry_percentile,
        } for t in all_trades],
    }

    output_dir = os.path.join(os.path.dirname(__file__), "..", "output", "v11")
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "quick_backtest.json")
    with open(json_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {json_path}")


def _build_snapshot(date, klines_all):
    """构建截至某日的快照"""
    snap = {}
    for code, kls in klines_all.items():
        filtered = [k for k in kls if k["date"] <= date]
        if filtered: snap[code] = filtered
    return snap


if __name__ == "__main__":
    main()
