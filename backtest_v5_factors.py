#!/usr/bin/env python3
"""
SS-Enhanced V5 因子敏感度回测 — 含大盘/板块环境
==================================================
新增 V5: 大盘环境（每日池内涨跌比） + 板块相对强度（板块内均值vs全市场）
"""

import json, math, os, sys
from datetime import datetime
from collections import defaultdict, Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from scoring_engine import (
    fetch_kline_batch, fetch_extra_info, score_ss_enhanced, get_theme,
    fetch_sector_context
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


def score_bucket(s):
    return (s // 10) * 10


def main():
    today_str = datetime.now().strftime("%Y-%m-%d")

    with open(os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")) as f:
        codes = [l.strip() for l in f if l.strip()]

    print(f"╔{'═'*55}╗")
    print(f"║  V5 因子敏感度回测 — 120天 + 大盘/板块  ║")
    print(f"╚{'═'*55}╝")

    # ========== 1. 数据 ==========
    print("\n[1/4] 拉取 K线 (252天)...")
    klines_all = fetch_kline_batch(codes, days=252)
    print(f"  K线: {len(klines_all)} 只")
    extra_all = fetch_extra_info(codes)

    # 预计算每只股票的主题板块
    for code in extra_all:
        extra_all[code]["_sector"] = get_theme(code)

    # ========== 2. 滚动评分（使用当日板块数据近似）==========
    print("\n[2/4] 逐日评分...")
    all_dates = set()
    for code, kl in klines_all.items():
        for k in kl:
            all_dates.add(k['date'])
    all_dates = sorted(all_dates)
    recent_dates = all_dates[-120:]

    # 计算每只股票每天的前向收益和当日涨跌
    # {date: {code: {close, ret_today, ...}}}
    daily_snapshot = defaultdict(dict)
    for code in codes:
        kl = klines_all.get(code)
        if not kl: continue
        for i, k in enumerate(kl):
            if i < 1: continue
            yest_close = kl[i-1]['close']
            ret_today = (k['close'] / yest_close - 1) * 100 if yest_close > 0 else 0
            daily_snapshot[k['date']][code] = {
                "close": k['close'], "ret_today": ret_today
            }

    # 对每个交易日计算市场状态和板块状态
    market_states = {}
    sector_states = {}
    for date_str in recent_dates:
        snaps = daily_snapshot.get(date_str, {})
        if len(snaps) < 30: continue
        # 市场强弱：涨跌比
        up = sum(1 for v in snaps.values() if v["ret_today"] > 0)
        down = sum(1 for v in snaps.values() if v["ret_today"] < 0)
        ratio = up / max(down, 1)
        if ratio > 2:
            market_states[date_str] = {"regime": "bullish", "market_score_delta": 3}
        elif ratio < 0.5:
            market_states[date_str] = {"regime": "bearish", "market_score_delta": -3}
        else:
            market_states[date_str] = {"regime": "neutral", "market_score_delta": 0}

        # 板块强度：每个板块的当日平均涨跌幅 vs 全市场均值
        by_sector = defaultdict(list)
        for code, snap in snaps.items():
            sec = get_theme(code)
            by_sector[sec].append(snap["ret_today"])
        market_avg = sum(v["ret_today"] for v in snaps.values()) / len(snaps)
        ss = {}
        for sec, rets in by_sector.items():
            if len(rets) < 3: continue
            sec_avg = sum(rets) / len(rets)
            diff = sec_avg - market_avg
            if diff > 2:
                ss[sec] = 5
            elif diff > 1:
                ss[sec] = 3
            elif diff > 0:
                ss[sec] = 1
            elif diff > -1:
                ss[sec] = 0
            elif diff > -2:
                ss[sec] = -3
            else:
                ss[sec] = -5
        sector_states[date_str] = ss

    # 滚动评分
    records = []
    processed = 0
    for date_str in recent_dates:
        processed += 1
        if processed % 20 == 0:
            print(f"  评分: {processed}/{len(recent_dates)}")

        mr = market_states.get(date_str, {"regime": "neutral", "market_score_delta": 0})
        ss = sector_states.get(date_str, {})

        for code in codes:
            kl = klines_all.get(code)
            if not kl: continue
            idx = None
            for i, k in enumerate(kl):
                if k['date'] == date_str:
                    idx = i
                    break
            if idx is None or idx < 60: continue

            ex = extra_all.get(code, {})
            s = score_ss_enhanced(kl, idx, event_list=None, today_str=None, extra=ex,
                                   market_regime=mr, sector_strength=ss)
            if s is None: continue

            fwd_rets = {}
            close_today = kl[idx]['close']
            for fwd_days in [5, 10, 15, 20]:
                fwd_idx = idx + fwd_days
                if fwd_idx < len(kl):
                    fwd_rets[f"fwd_{fwd_days}"] = round((kl[fwd_idx]['close'] / close_today - 1) * 100, 4)
                else:
                    fwd_rets[f"fwd_{fwd_days}"] = None

            factor_triggers = {}
            for f in s.get('factors', []):
                key = f"{f.get('dim','')}_{f.get('name','')}"
                factor_triggers[key] = {"delta": f.get('delta', 0), "detail": f.get('detail', '')}

            records.append({
                "date": date_str, "code": code,
                "score": s["score"], "tech": s["tech"],
                "capital": s["capital"], "info": s["info"],
                "market_regime": mr.get("regime"),
                "factor_triggers": factor_triggers,
                "fwd_rets": fwd_rets,
            })

    print(f"  总样本: {len(records)} 条")

    # ========== 3. 因子敏感度 ==========
    print("\n[3/4] 因子敏感度分析...")
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
            same_bucket_excesses = [e["excess"] for e in bucket_excess if e["fwd"] == fwd]
            w_excess = round(sum(same_bucket_excesses) / len(same_bucket_excesses), 3) if same_bucket_excesses else None
            summary[f"fwd_{fwd}"] = {
                "triggered_n": ts["n"], "triggered_mean": ts["mean"],
                "triggered_win": ts["win_rate"], "baseline_mean": bs["mean"],
                "baseline_win": bs["win_rate"],
                "bucketed_excess": w_excess, "triggered_t_stat": ts["t_stat"],
            }
        factor_analysis[fk] = {"total_triggered": all_factors[fk], "summary": summary}

    # ========== 4. 市场状态分层分析 ==========
    print("\n[4/4] 市场状态分层...")
    regime_analysis = {}
    for regime in ["bullish", "neutral", "bearish"]:
        reg_records = [r for r in records if r.get("market_regime") == regime]
        if len(reg_records) < 100: continue
        by_bucket = defaultdict(list)
        for r in reg_records:
            by_bucket[score_bucket(r["score"])].append(r)
        reg_stats = {}
        for fwd in [5, 10, 15, 20]:
            all_rets = []
            for r in reg_records:
                v = r["fwd_rets"].get(f"fwd_{fwd}")
                if v is not None: all_rets.append(v)
            reg_stats[f"fwd_{fwd}"] = calc_stats(all_rets)
        regime_analysis[regime] = {
            "n": len(reg_records), "days": len(set(r["date"] for r in reg_records)),
            "stats": reg_stats
        }

    # ========== 5. 排名输出 ==========
    ranked = []
    for fk, fa in factor_analysis.items():
        fwd10 = fa["summary"].get("fwd_10", {})
        excess = fwd10.get("bucketed_excess")
        n = fwd10.get("triggered_n", 0)
        if excess is not None and n >= 10:
            ranked.append((fk, excess, n, fa["summary"]))

    ranked.sort(key=lambda x: x[1], reverse=True)

    print("\n" + "=" * 90)
    print("V5 因子敏感度排名（10日 控制分档超额，含大盘/板块信号）")
    print("=" * 90)
    print(f"{'排名':<4} {'因子':<34} {'触发':>6} {'10日超额%':>10} {'5日超额%':>9} {'15日超额%':>9} {'20日超额%':>9} {'T值':>6}")
    print("-" * 90)

    for rank, (fk, excess, n, sm) in enumerate(ranked[:30], 1):
        ex5 = sm.get("fwd_5", {}).get("bucketed_excess")
        ex10 = sm.get("fwd_10", {}).get("bucketed_excess")
        ex15 = sm.get("fwd_15", {}).get("bucketed_excess")
        ex20 = sm.get("fwd_20", {}).get("bucketed_excess")
        t10 = sm.get("fwd_10", {}).get("triggered_t_stat")
        e5 = f"{ex5:+.3f}" if ex5 is not None else "-"
        e10 = f"{ex10:+.3f}" if ex10 is not None else "-"
        e15 = f"{ex15:+.3f}" if ex15 is not None else "-"
        e20 = f"{ex20:+.3f}" if ex20 is not None else "-"
        t = f"{t10:.2f}" if t10 is not None else "-"
        print(f"  {rank:<2}  {fk[:32]:<32} {n:>6} {e10:>10} {e5:>9} {e15:>9} {e20:>9} {t:>6}")

    # ========== 6. 市场状态效果 ==========
    print("\n" + "=" * 70)
    print("市场状态分层：不同环境下评分系统的绝对收益")
    print("=" * 70)
    print(f"{'环境':<12} {'天数':>6} {'样本':>8} {'5日均%':>9} {'10日均%':>9} {'15日均%':>9} {'20日均%':>9} {'10日胜率%':>10}")
    print("-" * 70)
    for regime in ["bullish", "neutral", "bearish"]:
        ra = regime_analysis.get(regime)
        if not ra: continue
        s5 = ra["stats"].get("fwd_5", {})
        s10 = ra["stats"].get("fwd_10", {})
        s15 = ra["stats"].get("fwd_15", {})
        s20 = ra["stats"].get("fwd_20", {})
        m5 = f"{s5.get('mean',0):+.2f}" if s5.get('mean') is not None else "-"
        m10 = f"{s10.get('mean',0):+.2f}" if s10.get('mean') is not None else "-"
        m15 = f"{s15.get('mean',0):+.2f}" if s15.get('mean') is not None else "-"
        m20 = f"{s20.get('mean',0):+.2f}" if s20.get('mean') is not None else "-"
        w10 = f"{s10.get('win_rate',0):.0f}" if s10.get('win_rate') is not None else "-"
        print(f"  {regime:<10} {ra['days']:>6} {ra['n']:>8} {m5:>9} {m10:>9} {m15:>9} {m20:>9} {w10:>10}")

    # ========== 7. V5 新增因子（大盘+板块）效果 ==========
    print("\n=== V5 大盘/板块因子表现 ===")
    v5_factors = ['大盘_大盘强势', '大盘_大盘弱势', '板块_板块领涨', '板块_板块拖累']
    for fk, excess, n, sm in ranked:
        if fk in v5_factors or fk.split("_")[0] in ("大盘", "板块"):
            ex5 = sm.get("fwd_5", {}).get("bucketed_excess", "-")
            ex10 = sm.get("fwd_10", {}).get("bucketed_excess", "-")
            ex15 = sm.get("fwd_15", {}).get("bucketed_excess", "-")
            print(f"  {fk:<35} n={n:>5} 5d={ex5} 10d={ex10} 15d={ex15}")

    # Save
    result = {
        "backtest_period": f"{recent_dates[0]} ~ {recent_dates[-1]}",
        "total_records": len(records),
        "ranked_factors": [{"factor": fk, "fwd10_excess": ex, "n": n, "summary": sm}
                          for fk, ex, n, sm in ranked[:40]],
        "regime_analysis": regime_analysis,
    }
    json_path = os.path.join(OUTPUT_DIR, f"V5因子敏感度回测_{today_str}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n完整结果: {json_path}")

    # Conclusions
    print(f"\n{'═'*55}")
    if ranked:
        print(f"最强 5 因子: {', '.join(fk for fk,_,_,_ in ranked[:5])}")
        neg = [fk for fk, ex, n, sm in ranked if ex is not None and ex < -1.0]
        print(f"最强负因子: {', '.join(neg[:5])}")
    print(f"{'═'*55}")


if __name__ == "__main__":
    main()
