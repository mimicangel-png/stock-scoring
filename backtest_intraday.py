#!/usr/bin/env python3
"""
盘中建议回测 — 模拟 2:30 PM 数据 + 120天滚动
=============================================
独立产品线，与每日评分(SS-Enhanced V7)并行。
权重: 技术25% + 资金35% + 信息15% + 盘中动量25%

方法：
1. 取过去 252 天 K 线
2. 对最近 120 个交易日，逐只股票模拟 2:30 PM 数据
3. 模拟逻辑：2:30 价格≈(open+close)/2 加权，量≈日量×0.85
4. 计算前向超额收益（vs 同日同分档基线）
5. 输出各维度预测能力 + 推荐准确性
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

    # 模拟 2:30 价格：基于开盘+收盘以及高低位置推断
    # 如果收阳（close>open），2:30价格在中间偏上位置
    # 如果收阴，2:30价格在中间偏下位置
    if high > low:
        close_pos = (close - low) / (high - low)  # 0~1，收盘在日内的位置
    else:
        close_pos = 0.5

    # 2:30价格≈收盘，但往开盘方向微调（因为收盘前30分钟可能有变化）
    sim_price = close * 0.92 + opn * 0.08
    # 如果收盘在日内高位，2:30应该也在高位
    if close_pos > 0.7:
        sim_price = close * 0.97 + high * 0.03
    elif close_pos < 0.3:
        sim_price = close * 0.97 + low * 0.03

    # 2:30 成交量约为日成交量的 85%
    sim_vol = vol * 0.85

    # 量比：当日量/5日均量
    if len(kl) >= 6:
        avg_vol = sum(kl[j]['volume'] for j in range(idx-5, idx)) / 5
        vol_ratio = (sim_vol / avg_vol) if avg_vol > 0 else 1
    else:
        vol_ratio = 1

    # 涨跌幅（vs 昨日收盘）
    if idx >= 1:
        yest_close = kl[idx-1]['close']
        change_pct = (sim_price / yest_close - 1) * 100 if yest_close > 0 else 0
    else:
        change_pct = 0

    return {
        "price": sim_price,
        "open": opn,
        "high": high,
        "low": low,
        "volume": sim_vol,
        "amount": sim_vol * sim_price,
        "change_pct": change_pct,
        "turnover": 0,
        "vol_ratio": vol_ratio,
    }


def score_bucket(s):
    return (s // 10) * 10


def main():
    TODAY = datetime.now()
    today_str = TODAY.strftime("%Y-%m-%d")

    with open(os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")) as f:
        codes = [l.strip() for l in f if l.strip()]

    print(f"╔{'═'*55}╗")
    print(f"║  盘中建议回测 — 模拟2:30PM + 120天滚动  ║")
    print(f"╚{'═'*55}╝")
    print(f"股票池: {len(codes)} 只 | 回测窗口: 120天")

    # ========== 1. 数据 ==========
    print("\n[1/5] 拉取 K线 (252天)...")
    klines_all = fetch_kline_batch(codes, days=252)
    print(f"  K线: {len(klines_all)} 只")

    extra_all = fetch_extra_info(codes)
    for code in extra_all:
        extra_all[code]["_sector"] = get_theme(code)

    # ========== 2. 滚动评分 ==========
    print("\n[2/5] 逐日模拟2:30PM评分...")
    all_dates = set()
    for code, kl in klines_all.items():
        for k in kl:
            all_dates.add(k['date'])
    all_dates = sorted(all_dates)
    recent_dates = all_dates[-120:]

    records = []
    processed = 0
    for date_str in recent_dates:
        processed += 1
        if processed % 20 == 0:
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

            # 模拟 2:30 PM 数据
            rt = sim_realtime(kl, idx)
            ex = extra_all.get(code, {})

            s = score_intraday(kl, idx, rt, event_list=None, today_str=None, extra=ex)
            if s is None: continue

            # 前向收益
            fwd_rets = {}
            close_today = kl[idx]['close']
            for fwd_days in [5, 10, 15, 20]:
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

    # ========== 3. 推荐准确性 ==========
    print("\n[3/5] 推荐准确性分析...")
    suggestion_stats = defaultdict(lambda: {
        "count": 0, "fwd_5": [], "fwd_10": [], "fwd_15": [], "fwd_20": []
    })

    for r in records:
        sug = r["suggestion"]
        suggestion_stats[sug]["count"] += 1
        for fwd in [5, 10, 15, 20]:
            val = r["fwd_rets"].get(f"fwd_{fwd}")
            if val is not None:
                suggestion_stats[sug][f"fwd_{fwd}"].append(val)

    sug_order = ["strong_buy", "buy", "hold", "watch", "avoid"]
    print(f"\n{'='*85}")
    print(f"盘中评分推荐准确性（按等级）")
    print(f"{'='*85}")
    print(f"{'推荐':<14} {'数量':>6} {'5日均%':>9} {'5日胜率':>8} {'10日均%':>9} {'10日胜率':>8} {'20日均%':>9}")
    print("-" * 85)

    for sug in sug_order:
        if sug not in suggestion_stats: continue
        st = suggestion_stats[sug]
        s5 = calc_stats(st["fwd_5"])
        s10 = calc_stats(st["fwd_10"])
        s20 = calc_stats(st["fwd_20"])
        m5 = f"{s5['mean']:+.2f}" if s5['mean'] is not None else "-"
        w5 = f"{s5['win_rate']:.0f}%" if s5['win_rate'] is not None else "-"
        m10 = f"{s10['mean']:+.2f}" if s10['mean'] is not None else "-"
        w10 = f"{s10['win_rate']:.0f}%" if s10['win_rate'] is not None else "-"
        m20 = f"{s20['mean']:+.2f}" if s20['mean'] is not None else "-"
        names = {"strong_buy":"🔥强烈买入","buy":"🟢逢低买入","hold":"🟡持有",
                 "watch":"⚪观望","avoid":"🔴回避"}
        print(f"  {names.get(sug,sug):<12} {st['count']:>6} {m5:>9} {w5:>8} {m10:>9} {w10:>8} {m20:>9}")

    # ========== 4. 因子敏感度 ==========
    print("\n[4/5] 因子敏感度分析...")
    all_factors = Counter()
    for r in records:
        for fk in r['factor_triggers']:
            all_factors[fk] += 1
    top_factors = [fk for fk, cnt in all_factors.most_common(35)]

    factor_analysis = {}
    for fk in top_factors:
        triggered_rets = {fwd: [] for fwd in [5, 10, 15, 20]}
        baseline_rets = {fwd: [] for fwd in [5, 10, 15, 20]}
        by_bucket = defaultdict(lambda: {"triggered": defaultdict(list), "baseline": defaultdict(list)})

        for r in records:
            bucket = score_bucket(r["score"])
            is_trig = fk in r["factor_triggers"]
            for fwd in [5, 10, 15, 20]:
                key = f"fwd_{fwd}"
                val = r["fwd_rets"].get(key)
                if val is None: continue
                if is_trig:
                    by_bucket[bucket]["triggered"][fwd].append(val)
                    triggered_rets[fwd].append(val)
                else:
                    by_bucket[bucket]["baseline"][fwd].append(val)
                    baseline_rets[fwd].append(val)

        # 控制分档后的超额
        bucket_excess = []
        for bucket in sorted(by_bucket.keys()):
            bd = by_bucket[bucket]
            for fwd in [5, 10, 15, 20]:
                ts = calc_stats(bd["triggered"][fwd])
                bs = calc_stats(bd["baseline"][fwd])
                if ts["n"] >= 5 and bs["n"] >= 5 and ts["mean"] is not None and bs["mean"] is not None:
                    bucket_excess.append({"bucket": bucket, "fwd": fwd, "excess": round(ts["mean"] - bs["mean"], 3)})

        summary = {}
        for fwd in [5, 10, 15, 20]:
            ts = calc_stats(triggered_rets[fwd])
            bs = calc_stats(baseline_rets[fwd])
            same_excesses = [e["excess"] for e in bucket_excess if e["fwd"] == fwd]
            w_excess = round(sum(same_excesses) / len(same_excesses), 3) if same_excesses else None
            summary[f"fwd_{fwd}"] = {
                "triggered_n": ts["n"], "triggered_mean": ts["mean"],
                "triggered_win": ts["win_rate"],
                "bucketed_excess": w_excess, "triggered_t_stat": ts["t_stat"],
            }
        factor_analysis[fk] = {"total_triggered": all_factors[fk], "summary": summary}

    ranked = []
    for fk, fa in factor_analysis.items():
        fwd10 = fa["summary"].get("fwd_10", {})
        excess = fwd10.get("bucketed_excess")
        n = fwd10.get("triggered_n", 0)
        if excess is not None and n >= 10:
            ranked.append((fk, excess, n, fa["summary"]))

    ranked.sort(key=lambda x: x[1], reverse=True)

    print(f"\n{'='*95}")
    print(f"因子敏感度排名（10日 控制分档超额收益）")
    print(f"{'='*95}")
    print(f"{'排名':<4} {'因子':<36} {'触发':>6} {'10日超额':>10} {'5日超额':>9} {'20日超额':>9} {'T值':>6}")
    print("-" * 95)

    for rank, (fk, excess, n, sm) in enumerate(ranked[:25], 1):
        ex5 = sm.get("fwd_5", {}).get("bucketed_excess")
        ex10 = sm.get("fwd_10", {}).get("bucketed_excess")
        ex20 = sm.get("fwd_20", {}).get("bucketed_excess")
        t10 = sm.get("fwd_10", {}).get("triggered_t_stat")
        e5 = f"{ex5:+.3f}" if ex5 is not None else "-"
        e10 = f"{ex10:+.3f}" if ex10 is not None else "-"
        e20 = f"{ex20:+.3f}" if ex20 is not None else "-"
        tt = f"{t10:.2f}" if t10 is not None else "-"
        print(f"  {rank:<2}  {fk[:34]:<34} {n:>6} {e10:>10} {e5:>9} {e20:>9} {tt:>6}")

    # ========== 5. 维度权重贡献 ==========
    print("\n[5/5] 维度贡献分析...")
    dim_excess = defaultdict(list)
    for r in records:
        for fwd in [5, 10, 15, 20]:
            val = r["fwd_rets"].get(f"fwd_{fwd}")
            if val is None: continue
            # 高分维度的贡献
            for dim, key in [("tech","tech"), ("capital","capital"), ("info","info"), ("momentum","momentum")]:
                if r[dim] > 60:  # 该维度得分高于60
                    dim_excess[f"{dim}_fwd{fwd}"].append(val)

    print(f"\n{'='*60}")
    print(f"各维度高分(>60)时的前向收益")
    print(f"{'='*60}")
    print(f"{'维度':<15} {'5日均%':>9} {'5日胜率':>8} {'10日均%':>9} {'10日胜率':>8}")
    print("-" * 60)
    for dim in ["tech", "capital", "info", "momentum"]:
        s5 = calc_stats(dim_excess.get(f"{dim}_fwd5", []))
        s10 = calc_stats(dim_excess.get(f"{dim}_fwd10", []))
        m5 = f"{s5['mean']:+.2f}" if s5['mean'] is not None else "-"
        w5 = f"{s5['win_rate']:.0f}%" if s5['win_rate'] is not None else "-"
        m10 = f"{s10['mean']:+.2f}" if s10['mean'] is not None else "-"
        w10 = f"{s10['win_rate']:.0f}%" if s10['win_rate'] is not None else "-"
        names = {"tech":"技术面(25%)","capital":"资金面(35%)","info":"信息面(15%)","momentum":"盘中动量(25%)"}
        print(f"  {names[dim]:<13} {m5:>9} {w5:>8} {m10:>9} {w10:>8}")

    # ========== 保存结果 ==========
    result = {
        "backtest_period": f"{recent_dates[0]} ~ {recent_dates[-1]}",
        "total_records": len(records),
        "total_codes": len(codes),
        "suggestion_stats": {sug: {"count": st["count"],
            "fwd_5_mean": calc_stats(st["fwd_5"]).get("mean"),
            "fwd_10_mean": calc_stats(st["fwd_10"]).get("mean"),
            "fwd_10_win": calc_stats(st["fwd_10"]).get("win_rate"),
        } for sug, st in suggestion_stats.items()},
        "ranked_factors": [{"factor": fk, "fwd10_excess": ex, "n": n, "summary": sm}
                          for fk, ex, n, sm in ranked[:40]],
        "top_positive": [fk for fk, ex, n, sm in ranked if ex > 0.3][:10],
        "top_negative": [fk for fk, ex, n, sm in ranked if ex < -0.3][:10],
    }

    json_path = os.path.join(OUTPUT_DIR, f"盘中建议回测_{today_str}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n完整结果: {json_path}")

    # 结论
    print(f"\n{'═'*60}")
    print("回测结论:")
    if suggestion_stats.get("strong_buy", {}).get("count", 0) > 0:
        sb = calc_stats(suggestion_stats["strong_buy"]["fwd_10"])
        print(f"  强烈买入推荐: {suggestion_stats['strong_buy']['count']}次, 10日均{sb['mean']:+.2f}%, 胜率{sb['win_rate']:.0f}%")
    if suggestion_stats.get("buy", {}).get("count", 0) > 0:
        b = calc_stats(suggestion_stats["buy"]["fwd_10"])
        print(f"  逢低买入推荐: {suggestion_stats['buy']['count']}次, 10日均{b['mean']:+.2f}%, 胜率{b['win_rate']:.0f}%")
    if ranked:
        print(f"  最强5因子: {', '.join(fk for fk,_,_,_ in ranked[:5])}")
        neg = [fk for fk, ex, n, sm in ranked if ex is not None and ex < -1.5]
        if neg:
            print(f"  最弱因子: {', '.join(neg[:5])}")
    print(f"{'═'*60}")

    return result


if __name__ == "__main__":
    main()
