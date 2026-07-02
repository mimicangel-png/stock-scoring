#!/usr/bin/env python3
"""
日报评分改进回测 — 位置因子能否治追高
======================================
核心问题：当前高分股是"已涨完的票"，加位置因子（高位减分）后，
高分组(>=70)的未来5日收益是否提升、回撤是否降低。

方法：
1. 拉取252天K线，逐日对每只股票跑 score_ss_enhanced（纯K线场景）
2. 记录基准评分 + 20日涨幅 + 未来5日收益
3. 构造改进评分：对20日涨幅过大的票，tech封顶+减分
4. 对比 基准版 vs 改进版 在 >=70 / >=65 分组的前瞻收益
5. 用数据决定阈值
"""
import json, math, os, sys
from datetime import datetime
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from scoring_engine import fetch_kline_batch, fetch_extra_info, score_ss_enhanced, get_theme

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
    return {"n": n, "mean": round(mean, 3), "std": round(std, 3),
            "win_rate": round(win_rate, 1), "median": round(sv[n // 2], 3)}


def apply_position_factor(score, tech, capital, info, ret_20d, ret_5d):
    """
    位置因子改进：对短期涨幅过大的票，tech封顶+减分，重算总分。
    测试三档阈值，返回多个改进版本。
    """
    versions = {}

    # V-A: 20日涨幅>40% 时 tech封顶60再-8
    tech_a = tech
    if ret_20d > 40:
        tech_a = min(tech, 60) - 8
    versions["V-A(r20>40封60-8)"] = round(tech_a * 0.4 + capital * 0.4 + info * 0.2)

    # V-B: 20日涨幅>40% 封55-12; >60% 封50-18 (更激进)
    tech_b = tech
    if ret_20d > 60:
        tech_b = min(tech, 50) - 18
    elif ret_20d > 40:
        tech_b = min(tech, 55) - 12
    versions["V-B(分级40/60)"] = round(tech_b * 0.4 + capital * 0.4 + info * 0.2)

    # V-C: 5日>20% 或 20日>40% 触发，tech封顶58-10
    tech_c = tech
    if ret_5d > 20 or ret_20d > 40:
        tech_c = min(tech, 58) - 10
    versions["V-C(5d20或20d40)"] = round(tech_c * 0.4 + capital * 0.4 + info * 0.2)

    return versions


def main():
    with open(os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")) as f:
        codes = [l.strip() for l in f if l.strip()]

    print("=" * 64)
    print("  日报评分改进回测 — 位置因子能否治追高")
    print("  纯K线场景 | 120天 | 目标: fwd_5 超额")
    print("=" * 64)
    print(f"股票池: {len(codes)} 只")

    # 1. 数据
    print("\n[1/4] 拉取K线 (252天)...")
    klines_all = fetch_kline_batch(codes, days=252)
    print(f"  K线: {len(klines_all)} 只")

    extra_all = fetch_extra_info(codes)
    for code in extra_all:
        extra_all[code]["_sector"] = get_theme(code)

    # 2. 逐日评分
    print("\n[2/4] 逐日评分 (纯K线, 120天)...")
    all_dates = set()
    for kl in klines_all.values():
        for k in kl:
            all_dates.add(k['date'])
    all_dates = sorted(all_dates)
    recent_dates = all_dates[-120:]

    records = []
    for di, date_str in enumerate(recent_dates):
        if (di + 1) % 30 == 0:
            print(f"  评分: {di+1}/{len(recent_dates)} ({date_str})")
        for code in codes:
            kl = klines_all.get(code)
            if not kl:
                continue
            idx = None
            for i, k in enumerate(kl):
                if k['date'] == date_str:
                    idx = i
                    break
            if idx is None or idx < 60:
                continue

            ex = extra_all.get(code, {})
            # 纯K线场景：不传 fund_flow/risk/news/market
            s = score_ss_enhanced(kl, idx, event_list=None, today_str=date_str, extra=ex)
            if s is None:
                continue

            c = [k['close'] for k in kl[:idx+1]]
            ret_5d = (c[-1] / c[-6] - 1) * 100 if len(c) >= 6 else 0
            ret_20d = (c[-1] / c[-21] - 1) * 100 if len(c) >= 21 else 0

            # 前向5日收益
            fwd5 = None
            if idx + 5 < len(kl):
                fwd5 = round((kl[idx + 5]['close'] / kl[idx]['close'] - 1) * 100, 4)

            records.append({
                "date": date_str, "code": code,
                "score": s["score"], "tech": s["tech"],
                "capital": s["capital"], "info": s["info"],
                "ret_5d": round(ret_5d, 2), "ret_20d": round(ret_20d, 2),
                "fwd_5": fwd5,
                "versions": apply_position_factor(
                    s["score"], s["tech"], s["capital"], s["info"], ret_20d, ret_5d),
            })

    valid = [r for r in records if r["fwd_5"] is not None]
    baseline = calc_stats([r["fwd_5"] for r in valid])
    print(f"\n  总样本: {len(records)} 条 (有效fwd_5: {len(valid)})")
    print(f"  全市场5日基线: 均值 {baseline['mean']:+.3f}%, 胜率 {baseline['win_rate']:.1f}%")

    # 3. 对比：基准 vs 各改进版本
    print("\n" + "=" * 78)
    print("[3/4] 高分组(>=70) 前瞻5日收益对比 — 位置因子是否治追高")
    print("-" * 78)
    print(f"{'版本':<22} {'>=70数量':>8} {'5日均值':>9} {'5日胜率':>8} {'5日超额':>9} {'20日已涨':>9}")
    print("-" * 78)

    def show_version(label, scores):
        grp = [r for r in valid if scores(r) >= 70]
        if not grp:
            print(f"{label:<22} {'0':>8} {'-':>9} {'-':>8} {'-':>9} {'-':>9}")
            return
        s5 = calc_stats([r["fwd_5"] for r in grp])
        ex = (s5["mean"] or 0) - (baseline["mean"] or 0)
        avg_r20 = sum(r["ret_20d"] for r in grp) / len(grp)
        print(f"{label:<22} {s5['n']:>8} {s5['mean']:>+8.3f}% {s5['win_rate']:>7.1f}% {ex:>+8.3f}% {avg_r20:>+8.1f}%")

    show_version("基准(当前)", lambda r: r["score"])
    for vname in ["V-A(r20>40封60-8)", "V-B(分级40/60)", "V-C(5d20或20d40)"]:
        show_version(vname, lambda r, vn=vname: r["versions"][vn])

    # 4. 更细的分档对比 + 追高股专项
    print("\n" + "=" * 78)
    print("[4/4] 追高股专项 — 20日涨幅>40% 的票，改进后收益是否变好")
    print("-" * 78)
    chase = [r for r in valid if r["ret_20d"] > 40]
    if chase:
        print(f"  追高股样本: {len(chase)} 条")
        print(f"\n  {'版本':<22} {'>=70占比':>9} {'5日均值':>9} {'5日胜率':>8} {'5日超额':>9}")
        # 基准
        grp = [r for r in chase if r["score"] >= 70]
        s = calc_stats([r["fwd_5"] for r in grp])
        ratio = len(grp) / len(chase) * 100
        ex = (s["mean"] or 0) - (baseline["mean"] or 0)
        print(f"  {'基准(当前)':<22} {ratio:>8.1f}% {s['mean']:>+8.3f}% {s['win_rate']:>7.1f}% {ex:>+8.3f}%")
        for vname in ["V-A(r20>40封60-8)", "V-B(分级40/60)", "V-C(5d20或20d40)"]:
            grp = [r for r in chase if r["versions"][vname] >= 70]
            s = calc_stats([r["fwd_5"] for r in grp])
            ratio = len(grp) / len(chase) * 100
            ex = (s["mean"] or 0) - (baseline["mean"] or 0)
            m = f"{s['mean']:>+8.3f}%" if s["mean"] is not None else "      n/a"
            w = f"{s['win_rate']:>7.1f}%" if s["win_rate"] is not None else "    n/a"
            print(f"  {vname:<22} {ratio:>8.1f}% {m} {w} {ex:>+8.3f}%")

        # 追高股里被改进版"踢出>=70"的，它们的实际收益（验证踢得对不对）
        print(f"\n  被V-B踢出>=70的追高股，其实际5日收益（踢对了=收益低/负）:")
        kicked = [r for r in chase if r["score"] >= 70 and r["versions"]["V-B(分级40/60)"] < 70]
        if kicked:
            ks = calc_stats([r["fwd_5"] for r in kicked])
            print(f"    样本{ks['n']} | 5日均值 {ks['mean'] or 0:+.3f}% | 胜率 {ks['win_rate'] or 0:.1f}% | 超额 {(ks['mean'] or 0)-(baseline['mean'] or 0):+.3f}%")
        else:
            print(f"    无（基准版就没把追高股放进>=70）")

    # 全局对比：所有分档
    print("\n" + "=" * 78)
    print("全分档对比 — 基准 vs V-B(分级40/60)")
    print("-" * 78)
    print(f"{'分档':<12} {'基准n':>6} {'基准均值':>9} {'V-B n':>7} {'V-B均值':>9} {'差异':>8}")
    for lo, hi, label in [(70, 200, ">=70"), (65, 70, "65-70"), (60, 65, "60-65"), (50, 60, "50-60"), (0, 50, "<50")]:
        g0 = [r for r in valid if lo <= r["score"] < hi]
        g1 = [r for r in valid if lo <= r["versions"]["V-B(分级40/60)"] < hi]
        s0 = calc_stats([r["fwd_5"] for r in g0])
        s1 = calc_stats([r["fwd_5"] for r in g1])
        d = (s1["mean"] or 0) - (s0["mean"] or 0)
        print(f"{label:<12} {s0['n']:>6} {s0['mean']:>+8.3f}% {s1['n']:>7} {s1['mean']:>+8.3f}% {d:>+7.3f}%")

    # 保存
    out = os.path.join(OUTPUT_DIR, f"位置因子回测_{datetime.now().strftime('%Y-%m-%d')}.json")
    summary = {
        "baseline_5d": baseline,
        "sample_size": len(valid),
        "window_days": 120,
        "note": "纯K线场景(无fund_flow/risk/news)，测试位置因子对追高的抑制效果",
    }
    with open(out, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已存: {out}")
    print("\n  结论看 >=70 行：改进版超额是否高于基准、20日已涨是否下降。")
    print("  若 V-B 的 >=70 超额 > 基准超额，且追高股被踢出后实际收益确实低，则位置因子有效，可上线。")


if __name__ == "__main__":
    main()
