#!/usr/bin/env python3
"""
盘中建议 T+1 回测 — 聚焦次日收益
=====================================
目标：找出哪些因子对「当日14:30买入 → 次日收盘卖出」有预测力。

方法：
1. 取过去 252 天 K 线
2. 对最近 180 个交易日，逐只股票模拟 2:30 PM 数据
3. 计算 fwd_1（次日收盘收益）和 fwd_2（两日后收益）
4. 分析每个因子触发时的 T+1 超额收益（控制评分分档）
5. 输出 T+1 因子排名 + 可操作性结论
"""

import json, math, os, sys
from datetime import datetime, timedelta
from collections import defaultdict, Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from scoring_engine import (
    fetch_kline_batch, fetch_extra_info, score_intraday, get_suggestion_intraday, get_theme
)

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def calc_stats(values):
    if not values or len(values) < 5:
        return {"n": 0, "mean": None, "win_rate": None, "median": None, "t_stat": None}
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean)**2 for v in values) / (n - 1)) if n > 1 else 0
    win_rate = sum(1 for v in values if v > 0) / n * 100
    sv = sorted(values)
    median = sv[n // 2]
    t = round(mean / (std / math.sqrt(n)), 3) if std > 0 and n > 1 else None
    return {"n": n, "mean": round(mean, 3), "std": round(std, 3),
            "win_rate": round(win_rate, 1), "median": round(median, 3), "t_stat": t}


def sim_realtime(kl, idx):
    """从完整K线模拟 2:30 PM 实时数据"""
    k = kl[idx]
    close = k['close']
    opn = k['open']
    high = k['high']
    low = k['low']
    vol = k['volume']

    if high > low:
        close_pos = (close - low) / (high - low)
    else:
        close_pos = 0.5

    sim_price = close * 0.92 + opn * 0.08
    if close_pos > 0.7:
        sim_price = close * 0.97 + high * 0.03
    elif close_pos < 0.3:
        sim_price = close * 0.97 + low * 0.03

    sim_vol = vol * 0.85

    if len(kl) >= 6:
        avg_vol = sum(kl[j]['volume'] for j in range(idx-5, idx)) / 5
        vol_ratio = (sim_vol / avg_vol) if avg_vol > 0 else 1
    else:
        vol_ratio = 1

    if idx >= 1:
        yest_close = kl[idx-1]['close']
        change_pct = (sim_price / yest_close - 1) * 100 if yest_close > 0 else 0
    else:
        change_pct = 0

    return {
        "price": sim_price, "open": opn, "high": high, "low": low,
        "volume": sim_vol, "amount": sim_vol * sim_price,
        "change_pct": change_pct, "turnover": 0, "vol_ratio": vol_ratio,
    }


def score_bucket(s):
    return (s // 10) * 10


def main():
    TODAY = datetime.now()
    today_str = TODAY.strftime("%Y-%m-%d")

    with open(os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")) as f:
        codes = [l.strip() for l in f if l.strip()]

    print(f"╔{'═'*60}╗")
    print(f"║     盘中建议 T+1 回测 — 聚焦次日收益     ║")
    print(f"║  核心问题：2:30评分能否预测明天涨跌？    ║")
    print(f"╚{'═'*60}╝")
    print(f"股票池: {len(codes)} 只 | 回测窗口: 180天 | 目标: T+1收益")

    # ========== 1. 数据 ==========
    print("\n[1/6] 拉取 K线 (252天)...")
    klines_all = fetch_kline_batch(codes, days=252)
    print(f"  K线: {len(klines_all)} 只")

    extra_all = fetch_extra_info(codes)
    for code in extra_all:
        extra_all[code]["_sector"] = get_theme(code)

    # ========== 2. 滚动评分 ==========
    print("\n[2/6] 逐日模拟2:30PM评分 + 计算次日收益...")
    all_dates = set()
    for code, kl in klines_all.items():
        for k in kl:
            all_dates.add(k['date'])
    all_dates = sorted(all_dates)
    recent_dates = all_dates[-180:]

    records = []
    processed = 0
    for date_str in recent_dates:
        processed += 1
        if processed % 30 == 0:
            print(f"  评分: {processed}/{len(recent_dates)} ({date_str})")

        for code in codes:
            kl = klines_all.get(code)
            if not kl: continue
            idx = None
            for i, k in enumerate(kl):
                if k['date'] == date_str:
                    idx = i
                    break
            if idx is None or idx < 60: continue

            rt = sim_realtime(kl, idx)
            ex = extra_all.get(code, {})

            s = score_intraday(kl, idx, rt, event_list=None, today_str=None, extra=ex)
            if s is None: continue

            # 前向收益 — 重点是 fwd_1（次日）
            fwd_rets = {}
            close_today = kl[idx]['close']
            for fwd_days in [1, 2, 5, 10]:
                fwd_idx = idx + fwd_days
                if fwd_idx < len(kl):
                    fwd_rets[f"fwd_{fwd_days}"] = round((kl[fwd_idx]['close'] / close_today - 1) * 100, 4)
                else:
                    fwd_rets[f"fwd_{fwd_days}"] = None

            # 因子触发
            factor_triggers = {}
            for f in s.get('factors', []):
                key = f"{f.get('dim','')}_{f.get('name','')}"
                factor_triggers[key] = {"delta": f.get('delta', 0), "detail": f.get('detail', '')}

            records.append({
                "date": date_str, "code": code,
                "score": s["score"],
                "tech": s["tech"], "capital": s["capital"],
                "info": s["info"], "momentum": s["momentum"],
                "suggestion": get_suggestion_intraday(s["score"])[1],
                "factor_triggers": factor_triggers,
                "fwd_rets": fwd_rets,
            })

    print(f"  总样本: {len(records)} 条")

    # ========== 全市场基线 ==========
    all_fwd1 = [r["fwd_rets"]["fwd_1"] for r in records if r["fwd_rets"].get("fwd_1") is not None]
    baseline_fwd1 = calc_stats(all_fwd1)
    print(f"\n  全市场次日基线: 均值 {baseline_fwd1['mean']:+.3f}%, 胜率 {baseline_fwd1['win_rate']:.1f}%")

    # ========== 3. 评分等级 vs T+1 ==========
    print(f"\n{'='*85}")
    print(f"[3/6] 各评分等级的 T+1 表现")
    print(f"{'='*85}")

    suggestion_stats = defaultdict(lambda: {"count": 0, "fwd_1": [], "fwd_2": [], "fwd_5": []})
    for r in records:
        sug = r["suggestion"]
        suggestion_stats[sug]["count"] += 1
        for fwd in [1, 2, 5]:
            val = r["fwd_rets"].get(f"fwd_{fwd}")
            if val is not None:
                suggestion_stats[sug][f"fwd_{fwd}"].append(val)

    sug_order = ["strong_buy", "buy", "hold", "watch", "avoid"]
    names = {"strong_buy": "🔥强烈买入", "buy": "🟢逢低买入", "hold": "🟡持有",
             "watch": "⚪观望", "avoid": "🔴回避"}

    print(f"\n{'推荐':<14} {'数量':>7} {'T+1均值%':>10} {'T+1胜率':>9} {'T+1超额':>10} {'T+2均值%':>10} {'T+5均值%':>10}")
    print("-" * 85)
    for sug in sug_order:
        if sug not in suggestion_stats: continue
        st = suggestion_stats[sug]
        s1 = calc_stats(st["fwd_1"])
        s2 = calc_stats(st["fwd_2"])
        s5 = calc_stats(st["fwd_5"])
        m1 = f"{s1['mean']:+.3f}" if s1['mean'] is not None else "-"
        w1 = f"{s1['win_rate']:.1f}%" if s1['win_rate'] is not None else "-"
        ex1 = f"{(s1['mean'] or 0) - (baseline_fwd1['mean'] or 0):+.3f}%" if s1['mean'] is not None else "-"
        m2 = f"{s2['mean']:+.3f}" if s2['mean'] is not None else "-"
        m5 = f"{s5['mean']:+.3f}" if s5['mean'] is not None else "-"
        print(f"  {names.get(sug,sug):<12} {st['count']:>7} {m1:>10} {w1:>9} {ex1:>10} {m2:>10} {m5:>10}")

    # ========== 4. 因子 T+1 敏感度 ==========
    print(f"\n{'='*95}")
    print(f"[4/6] 每个因子触发时的 T+1 超额收益（控制评分分档）")
    print(f"{'='*95}")

    all_factors = Counter()
    for r in records:
        for fk in r['factor_triggers']:
            all_factors[fk] += 1
    top_factors = [fk for fk, cnt in all_factors.most_common(40)]

    factor_analysis = {}
    for fk in top_factors:
        triggered_rets = {fwd: [] for fwd in [1, 2, 5, 10]}
        by_bucket = defaultdict(lambda: {"triggered": defaultdict(list), "baseline": defaultdict(list)})

        for r in records:
            bucket = score_bucket(r["score"])
            is_trig = fk in r["factor_triggers"]
            for fwd in [1, 2, 5, 10]:
                key = f"fwd_{fwd}"
                val = r["fwd_rets"].get(key)
                if val is None: continue
                if is_trig:
                    by_bucket[bucket]["triggered"][fwd].append(val)
                    triggered_rets[fwd].append(val)
                else:
                    by_bucket[bucket]["baseline"][fwd].append(val)

        # 控制分档后的超额
        bucket_excess = []
        for bucket in sorted(by_bucket.keys()):
            bd = by_bucket[bucket]
            for fwd in [1, 2, 5, 10]:
                ts = calc_stats(bd["triggered"][fwd])
                bs = calc_stats(bd["baseline"][fwd])
                if ts["n"] >= 5 and bs["n"] >= 5 and ts["mean"] is not None and bs["mean"] is not None:
                    bucket_excess.append({"bucket": bucket, "fwd": fwd, "excess": round(ts["mean"] - bs["mean"], 3)})

        summary = {}
        for fwd in [1, 2, 5, 10]:
            ts = calc_stats(triggered_rets[fwd])
            same_excesses = [e["excess"] for e in bucket_excess if e["fwd"] == fwd]
            w_excess = round(sum(same_excesses) / len(same_excesses), 3) if same_excesses else None
            summary[f"fwd_{fwd}"] = {
                "triggered_n": ts["n"], "triggered_mean": ts["mean"],
                "triggered_win": ts["win_rate"],
                "bucketed_excess": w_excess, "triggered_t_stat": ts["t_stat"],
            }
        factor_analysis[fk] = {"total_triggered": all_factors[fk], "summary": summary}

    # 按 T+1 超额排序
    ranked_t1 = []
    ranked_t10 = []
    for fk, fa in factor_analysis.items():
        fwd1 = fa["summary"].get("fwd_1", {})
        ex1 = fwd1.get("bucketed_excess")
        n1 = fwd1.get("triggered_n", 0)
        if ex1 is not None and n1 >= 10:
            ranked_t1.append((fk, ex1, n1, fa["summary"]))
        fwd10 = fa["summary"].get("fwd_10", {})
        ex10 = fwd10.get("bucketed_excess")
        n10 = fwd10.get("triggered_n", 0)
        if ex10 is not None and n10 >= 10:
            ranked_t10.append((fk, ex10, n10, fa["summary"]))

    ranked_t1.sort(key=lambda x: x[1], reverse=True)
    ranked_t10.sort(key=lambda x: x[1], reverse=True)

    print(f"\n{'排名':<4} {'因子':<36} {'触发':>6} {'T+1超额':>10} {'T+1胜率':>9} {'T+2超额':>10} {'T+10超额':>10} {'对比10日':>12}")
    print("-" * 100)
    for rank, (fk, ex1, n, sm) in enumerate(ranked_t1[:30], 1):
        s1 = sm.get("fwd_1", {})
        s2 = sm.get("fwd_2", {})
        s10 = sm.get("fwd_10", {})
        ex2 = s2.get("bucketed_excess")
        ex10 = s10.get("bucketed_excess")
        w1 = f"{s1.get('triggered_win',0):.1f}%" if s1.get('triggered_win') else "-"
        e1 = f"{ex1:+.3f}" if ex1 is not None else "-"
        e2 = f"{ex2:+.3f}" if ex2 is not None else "-"
        e10 = f"{ex10:+.3f}" if ex10 is not None else "-"

        # 找这个因子在10日排名中的位置做对比
        t10_rank = ""
        for i, (fk10, _, n10, _) in enumerate(ranked_t10, 1):
            if fk10 == fk:
                t10_rank = f"第{i}名" if i <= 30 else f"第{i}名↓"
                break

        print(f"  {rank:<2}  {fk[:34]:<34} {n:>6} {e1:>10} {w1:>9} {e2:>10} {e10:>10} {t10_rank:>12}")

    # ========== 5. 因子在 T+1 vs T+10 的排名对比（找出方向反转的因子）==========
    print(f"\n{'='*95}")
    print(f"[5/6] 方向反转检测：T+1 vs T+10 排名差异最大的因子")
    print(f"{'='*95}")

    t1_rank_map = {fk: i+1 for i, (fk, _, _, _) in enumerate(ranked_t1)}
    t10_rank_map = {fk: i+1 for i, (fk, _, _, _) in enumerate(ranked_t10)}

    # 找出排名差异大的
    reversals = []
    for fk in t1_rank_map:
        if fk in t10_rank_map:
            diff = abs(t1_rank_map[fk] - t10_rank_map[fk])
            if diff >= 8:
                fwd1_ex = ranked_t1[t1_rank_map[fk]-1][1]
                fwd10_ex = ranked_t10[t10_rank_map[fk]-1][1]
                reversals.append((fk, t1_rank_map[fk], t10_rank_map[fk], diff, fwd1_ex, fwd10_ex))
    reversals.sort(key=lambda x: x[3], reverse=True)

    if reversals:
        print(f"\n{'因子':<36} {'T+1排名':>8} {'10日排名':>8} {'差异':>6} {'T+1超额':>10} {'10日超额':>10}")
        print("-" * 85)
        for fk, r1, r10, diff, ex1, ex10 in reversals[:15]:
            print(f"  {fk[:34]:<34} {r1:>8} {r10:>8} {diff:>6} {ex1:>+10.3f} {ex10:>+10.3f}")

    # ========== 6. 综合评分面 T+1 表现 ==========
    print(f"\n{'='*82}")
    print(f"[6/6] 各维度高分(>60)时的 T+1 表现")
    print(f"{'='*82}")

    dim_excess = defaultdict(list)
    for r in records:
        val = r["fwd_rets"].get("fwd_1")
        if val is None: continue
        for dim in ["tech", "capital", "info", "momentum"]:
            if r[dim] > 60:
                dim_excess[f"{dim}"].append(val)

    print(f"\n{'维度':<15} {'样本':>7} {'T+1均值%':>10} {'T+1胜率':>9} {'T+1超额':>10} {'T值':>7}")
    print("-" * 65)
    for dim in ["tech", "capital", "info", "momentum"]:
        vals = dim_excess.get(dim, [])
        ss = calc_stats(vals)
        m = f"{ss['mean']:+.3f}" if ss['mean'] is not None else "-"
        w = f"{ss['win_rate']:.1f}%" if ss['win_rate'] is not None else "-"
        ex = f"{(ss['mean'] or 0) - (baseline_fwd1['mean'] or 0):+.3f}%" if ss['mean'] is not None else "-"
        t = f"{ss['t_stat']:.2f}" if ss['t_stat'] is not None else "-"
        names = {"tech": "技术面(25%)", "capital": "资金面(35%)", "info": "信息面(15%)", "momentum": "盘中动量(25%)"}
        print(f"  {names[dim]:<13} {ss['n']:>7} {m:>10} {w:>9} {ex:>10} {t:>7}")

    # ========== 保存结果 ==========
    result = {
        "backtest_period": f"{recent_dates[0]} ~ {recent_dates[-1]}",
        "total_records": len(records),
        "total_codes": len(codes),
        "baseline": {
            "fwd_1_mean": baseline_fwd1["mean"], "fwd_1_win": baseline_fwd1["win_rate"],
        },
        "suggestion_stats": {sug: {"count": st["count"],
            "fwd_1_mean": calc_stats(st["fwd_1"]).get("mean"),
            "fwd_1_win": calc_stats(st["fwd_1"]).get("win_rate"),
            "fwd_1_excess": round((calc_stats(st["fwd_1"]).get("mean") or 0) - (baseline_fwd1["mean"] or 0), 3),
        } for sug, st in suggestion_stats.items()},
        "ranked_factors_t1": [{"factor": fk, "fwd1_excess": ex, "n": n, "summary": sm}
                             for fk, ex, n, sm in ranked_t1[:40]],
        "top_positive_t1": [fk for fk, ex, n, sm in ranked_t1 if ex > 0.1][:10],
        "top_negative_t1": [fk for fk, ex, n, sm in ranked_t1 if ex < -0.1][:10],
        "reversals": [{"factor": fk, "t1_rank": r1, "t10_rank": r10, "fwd1_excess": ex1, "fwd10_excess": ex10}
                      for fk, r1, r10, diff, ex1, ex10 in reversals[:15]],
    }

    json_path = os.path.join(OUTPUT_DIR, f"T1回测_{today_str}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n完整结果: {json_path}")

    # ========== 结论 ==========
    print(f"\n{'═'*65}")
    print("T+1 回测结论")
    print(f"{'═'*65}")

    # 最佳等级
    for sug in ["buy", "hold"]:
        if sug in suggestion_stats:
            st = suggestion_stats[sug]
            s1 = calc_stats(st["fwd_1"])
            if s1["mean"] is not None:
                ex = s1["mean"] - (baseline_fwd1["mean"] or 0)
                print(f"  {names[sug]}: {st['count']}次, T+1均值{s1['mean']:+.3f}%, 胜率{s1['win_rate']:.1f}%, 超额{ex:+.3f}%")

    # 最强/最弱 T+1 因子
    pos_t1 = [fk for fk, ex, n, sm in ranked_t1 if ex > 0.2][:8]
    neg_t1 = [fk for fk, ex, n, sm in ranked_t1 if ex < -0.1][:8]
    print(f"\n  ✅ 对T+1正向有效(+超额>0.2%):")
    for fk in pos_t1:
        ex = next(ex for f, ex, n, sm in ranked_t1 if f == fk)
        print(f"      {fk}  (超额 {ex:+.3f}%)")
    print(f"\n  ❌ 对T+1负向拖累(-超额<0.1%):")
    for fk in neg_t1:
        ex = next(ex for f, ex, n, sm in ranked_t1 if f == fk)
        print(f"      {fk}  (超额 {ex:+.3f}%)")

    # 维度排名
    dim_perf = [(dim, calc_stats(dim_excess.get(dim, []))) for dim in ["tech", "capital", "info", "momentum"]]
    dim_perf.sort(key=lambda x: (x[1]["mean"] or -99), reverse=True)
    print(f"\n  维度T+1预测力排名: {' > '.join(d[0] for d in dim_perf)}")

    print(f"{'═'*65}")

    return result


if __name__ == "__main__":
    main()
