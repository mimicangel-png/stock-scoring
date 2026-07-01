#!/usr/bin/env python3
"""
SS-Enhanced V4 因子敏感度回测 — 120天滚动
============================================
目标：逐个因子评估其对前向收益的预测能力
方法：
  1. 对过去120个交易日的每一天，逐只股票评分并记录触发了哪些因子
  2. 对每个因子，对比「触发日」vs「未触发日（同分位）」的前向5/10/15/20天超额收益
  3. 控制变量：按总分10分档分组，消除高分股自然偏向
"""

import json, math, os, sys
from datetime import datetime, timedelta
from collections import defaultdict, Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from scoring_engine import (
    fetch_kline_batch, fetch_extra_info, score_ss_enhanced
)

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def calc_stats(values):
    if not values or len(values) < 5:
        return {"n": 0, "mean": None, "win_rate": None, "median": None}
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean)**2 for v in values) / (n - 1)) if n > 1 else 0
    win_rate = sum(1 for v in values if v > 0) / n * 100
    sv = sorted(values)
    median = sv[n // 2]
    return {"n": n, "mean": round(mean, 3), "std": round(std, 3),
            "win_rate": round(win_rate, 1), "median": round(median, 3),
            "t_stat": round(mean / (std / math.sqrt(n)), 3) if std > 0 and n > 1 else None}


def main():
    TODAY = datetime.now()
    today_str = TODAY.strftime("%Y-%m-%d")

    # 加载股票池
    with open(os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")) as f:
        codes = [l.strip() for l in f if l.strip()]

    print(f"╔{'═'*50}╗")
    print(f"║  V4 因子敏感度回测 — 120天  ║")
    print(f"╚{'═'*50}╝")
    print(f"股票池: {len(codes)} 只 | 回测: 120 日 | 预估: ~{len(codes)*120} 次日×股样本")

    # ========== 1. 获取 252 天 K 线（120 回测 + 60 最小滚动 + 额外缓冲）==========
    print("\n[1/3] 拉取 K 线 (252天)...")
    klines_all = fetch_kline_batch(codes, days=252)
    print(f"  K线获取成功: {len(klines_all)} 只")

    # 获取行情（用于市值分层等 extra 信息）
    extra_all = fetch_extra_info(codes)

    # ========== 2. 滚动评分 ==========
    print("\n[2/3] 逐日评分（无事件数据，纯 V4 技术面+资金面+信息面）...")
    all_dates = set()
    for code, kl in klines_all.items():
        for k in kl:
            all_dates.add(k['date'])

    all_dates = sorted(all_dates)
    # 取最近 120 个交易日
    recent_dates = all_dates[-120:]
    print(f"  交易日范围: {recent_dates[0]} ~ {recent_dates[-1]} ({len(recent_dates)}天)")

    # 存储结果: [{date, code, score, tech, capital, info, factors, fwd_rets}]
    records = []
    processed_days = 0

    for date_str in recent_dates:
        processed_days += 1
        if processed_days % 20 == 0:
            print(f"  进度: {processed_days}/{len(recent_dates)} ({date_str})")

        for code in codes:
            kl = klines_all.get(code)
            if not kl:
                continue
            # 找到当日的 K 线索引
            idx = None
            for i, k in enumerate(kl):
                if k['date'] == date_str:
                    idx = i
                    break
            if idx is None or idx < 60:
                continue

            ex = extra_all.get(code, {})
            s = score_ss_enhanced(kl, idx, event_list=None, today_str=None, extra=ex)
            if s is None:
                continue

            # 计算前向收益（需要未来数据在 K 线中）
            fwd_rets = {}
            close_today = kl[idx]['close']
            for fwd_days in [5, 10, 15, 20]:
                fwd_idx = idx + fwd_days
                if fwd_idx < len(kl):
                    fwd_rets[f"fwd_{fwd_days}"] = round((kl[fwd_idx]['close'] / close_today - 1) * 100, 4)
                else:
                    fwd_rets[f"fwd_{fwd_days}"] = None

            # 记录每个因子的触发情况
            factor_triggers = {}
            for f in s.get('factors', []):
                fname = f.get('name', '')
                fdim = f.get('dim', '')
                fdelta = f.get('delta', 0)
                # 标准化因子名
                key = f"{fdim}_{fname}"
                factor_triggers[key] = {"delta": fdelta, "detail": f.get('detail', '')}

            records.append({
                "date": date_str, "code": code,
                "score": s["score"], "tech": s["tech"],
                "capital": s["capital"], "info": s["info"],
                "factor_triggers": factor_triggers,
                "fwd_rets": fwd_rets,
            })

    print(f"  总样本: {len(records)} 条 (日×股)")

    # ========== 3. 因子敏感度分析 ==========
    print("\n[3/3] 因子敏感度分析...")

    # 收集所有因子
    all_factors = Counter()
    for r in records:
        for fk in r['factor_triggers']:
            all_factors[fk] += 1

    # 按触发次数排序，只分析前 30 个最常见的因子
    top_factors = [fk for fk, cnt in all_factors.most_common(30)]

    # 按总分10分档分组（控制变量）
    def score_bucket(s):
        return (s // 10) * 10

    # 对每个因子进行分析
    factor_analysis = {}
    for fk in top_factors:
        # 按分档分组：触发 vs 未触发
        triggered_returns = {fwd: [] for fwd in [5, 10, 15, 20]}
        baseline_returns = {fwd: [] for fwd in [5, 10, 15, 20]}

        # 按得分档分组
        by_bucket = defaultdict(lambda: {"triggered": defaultdict(list), "baseline": defaultdict(list)})
        for r in records:
            bucket = score_bucket(r["score"])
            is_triggered = fk in r["factor_triggers"]

            for fwd in [5, 10, 15, 20]:
                key = f"fwd_{fwd}"
                val = r["fwd_rets"].get(key)
                if val is None:
                    continue
                if is_triggered:
                    by_bucket[bucket]["triggered"][fwd].append(val)
                    triggered_returns[fwd].append(val)
                else:
                    by_bucket[bucket]["baseline"][fwd].append(val)
                    baseline_returns[fwd].append(val)

        # 计算控制分档后的超额收益（加权平均各分档的超额）
        bucket_excess = []
        for bucket in sorted(by_bucket.keys()):
            bdata = by_bucket[bucket]
            for fwd in [5, 10, 15, 20]:
                ts = calc_stats(bdata["triggered"][fwd])
                bs = calc_stats(bdata["baseline"][fwd])
                if ts["n"] >= 5 and bs["n"] >= 5 and ts["mean"] is not None and bs["mean"] is not None:
                    bucket_excess.append({
                        "bucket": bucket, "fwd": fwd,
                        "triggered_n": ts["n"], "baseline_n": bs["n"],
                        "excess": round(ts["mean"] - bs["mean"], 3),
                        "triggered_win": ts["win_rate"],
                        "baseline_win": bs["win_rate"],
                    })

        # 汇总统计
        summary = {}
        for fwd in [5, 10, 15, 20]:
            ts = calc_stats(triggered_returns[fwd])
            bs = calc_stats(baseline_returns[fwd])
            excess = round(ts["mean"] - bs["mean"], 3) if ts["mean"] is not None and bs["mean"] is not None else None

            # 加权超额（按分档）
            same_bucket_excesses = [e["excess"] for e in bucket_excess if e["fwd"] == fwd]
            n_buckets = len(same_bucket_excesses)
            weight_excess = round(sum(same_bucket_excesses) / n_buckets, 3) if n_buckets > 0 else None

            summary[f"fwd_{fwd}"] = {
                "triggered_n": ts["n"],
                "triggered_mean": ts["mean"],
                "triggered_win": ts["win_rate"],
                "baseline_mean": bs["mean"],
                "baseline_win": bs["win_rate"],
                "raw_excess": excess,
                "bucketed_excess": weight_excess,
                "triggered_t_stat": ts["t_stat"],
            }

        factor_analysis[fk] = {
            "total_triggered": all_factors[fk],
            "summary": summary,
        }

    # ========== 4. 按因子维度和方向排序 ==========
    # 简单指标：fwd_10 的 bucketed_excess
    ranked = []
    for fk, fa in factor_analysis.items():
        sm = fa["summary"]
        fwd10 = sm.get("fwd_10", {})
        excess = fwd10.get("bucketed_excess")
        n = fwd10.get("triggered_n", 0)
        if excess is not None and n >= 10:
            ranked.append((fk, excess, n, sm))

    ranked.sort(key=lambda x: x[1], reverse=True)

    # ========== 5. 输出报告 ==========
    print("\n" + "=" * 80)
    print("因子敏感度排名（按 10日 控制分档超额收益）")
    print("=" * 80)
    print(f"{'排名':<4} {'因子':<30} {'触发':>6} {'10日超额%':>10} {'5日超额%':>9} {'15日超额%':>9} {'20日超额%':>9} {'触发胜率%':>9}")
    print("-" * 80)

    for rank, (fk, excess, n, sm) in enumerate(ranked[:25], 1):
        fwd5_ex = sm.get("fwd_5", {}).get("bucketed_excess")
        fwd10_ex = sm.get("fwd_10", {}).get("bucketed_excess")
        fwd15_ex = sm.get("fwd_15", {}).get("bucketed_excess")
        fwd20_ex = sm.get("fwd_20", {}).get("bucketed_excess")
        win10 = sm.get("fwd_10", {}).get("triggered_win", 0)
        ex5 = f"{fwd5_ex:+.3f}" if fwd5_ex is not None else "-"
        ex10 = f"{fwd10_ex:+.3f}" if fwd10_ex is not None else "-"
        ex15 = f"{fwd15_ex:+.3f}" if fwd15_ex is not None else "-"
        ex20 = f"{fwd20_ex:+.3f}" if fwd20_ex is not None else "-"
        win = f"{win10:.0f}" if win10 else "-"
        short_name = fk[:28]
        print(f"  {rank:<2}  {short_name:<28} {n:>6} {ex10:>10} {ex5:>9} {ex15:>9} {ex20:>9} {win:>9}")

    # ========== 6. 保存详细 JSON ==========
    result = {
        "backtest_period": f"{recent_dates[0]} ~ {recent_dates[-1]}",
        "total_records": len(records),
        "total_codes": len(codes),
        "factor_analysis": factor_analysis,
        "ranked_factors": [{"factor": fk, "fwd10_excess": ex, "n": n, "summary": sm}
                          for fk, ex, n, sm in ranked[:40]],
        "top_positive": [fk for fk, ex, n, sm in ranked if ex > 0.2][:10],
        "top_negative": [fk for fk, ex, n, sm in reversed(ranked) if ex < -0.3][:10],
    }

    json_path = os.path.join(OUTPUT_DIR, f"V4因子敏感度回测_{today_str}.json")
    with open(json_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n完整结果已保存: {json_path}")
    print(f"\n{'═'*50}")
    print("结论:")
    if ranked:
        print(f"  最强正向因子 (10日超额>0.2%): {', '.join(result['top_positive'][:5])}")
        print(f"  最强负向因子 (10日超额<-0.3%): {', '.join(result['top_negative'][:5])}")
    print(f"{'═'*50}")

    return result


if __name__ == "__main__":
    main()
