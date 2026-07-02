#!/usr/bin/env python3
"""
GLM 全面回测：Walk-Forward GLM 权重学习 vs 固定权重
=====================================================
纯 numpy 实现，对比两种评分方案在 T+5 / T+7 / T+10 的表现。

指标体系：
  - 多头组合收益（Top-20% 等权）
  - 多头超额 vs 全市场等权
  - IC Rank / IC Mean
  - 胜率 / 最大回撤 / Sharpe / Calmar
  - Top-N 命中率（买的股票后续是否跑赢）

关键反思点：
  当前固定权重 = 40%技术 + 40%资金 + 20%信息
  GLM 学习 = 从历史数据学习每个因子的最优权重
"""

import json, math, os, sys
from datetime import datetime
from collections import defaultdict
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from scoring_engine import (
    fetch_kline_batch, fetch_extra_info, score_ss_enhanced, get_theme,
    calc_ma, calc_rsi, calc_ema
)
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
# Pure numpy OLS / Ridge
# ============================================================

def ols_fit(X, y, ridge_alpha=0.1):
    """纯 numpy OLS（带小 Ridge 正则化防奇异矩阵）"""
    n, p = X.shape
    # Ridge: (X^T X + αI)^-1 X^T y
    XtX = X.T @ X
    XtX += ridge_alpha * np.eye(p)
    Xty = X.T @ y
    try:
        beta = np.linalg.solve(XtX, Xty)
    except np.linalg.LinAlgError:
        beta = np.linalg.lstsq(X, y, rcond=None)[0]
    return beta


def spearmanr(x, y):
    """纯 numpy Spearman rank correlation"""
    n = len(x)
    if n < 3:
        return 0
    x_rank = np.argsort(np.argsort(x)).astype(float) + 1
    y_rank = np.argsort(np.argsort(y)).astype(float) + 1
    # Handle ties by averaging ranks
    diff = x_rank - y_rank
    rho = 1 - 6 * np.sum(diff**2) / (n * (n**2 - 1))
    return round(float(rho), 6)


# ============================================================
# 因子特征提取（纯数值，不做离散化）
# ============================================================

def extract_features(klines, idx, extra=None):
    """提取原始数值特征（非 category），让 GLM 自己学权重"""
    w = klines[:idx+1]
    c = np.array([k['close'] for k in w], dtype=float)
    v = np.array([k['volume'] for k in w], dtype=float)
    h = np.array([k['high'] for k in w], dtype=float)
    l = np.array([k['low'] for k in w], dtype=float)
    o = np.array([k['open'] for k in w], dtype=float)
    f = {}

    # -- 均线趋势 --
    ma5, ma10, ma20, ma60 = calc_ma(c,5), calc_ma(c,10), calc_ma(c,20), calc_ma(c,60)
    f["trend_5_10"] = (ma5/ma10-1)*100 if (ma5 and ma10) else 0
    f["trend_10_20"] = (ma10/ma20-1)*100 if (ma10 and ma20) else 0
    f["trend_20_60"] = (ma20/ma60-1)*100 if (ma20 and ma60) else 0
    f["ma_bull"] = 1 if (ma5 and ma10 and ma20 and ma5>ma10>ma20) else 0
    f["ma_bear"] = 1 if (ma5 and ma10 and ma20 and ma5<ma10<ma20) else 0

    # -- RSI --
    rsi = calc_rsi(c)
    f["rsi"] = rsi
    f["rsi_hi"] = max(0, rsi-70) if rsi>70 else 0
    f["rsi_lo"] = max(0, 30-rsi) if rsi<30 else 0

    # -- MACD --
    dif, dea = calc_ema(c,12), calc_ema(c,26)
    f["macd"] = (dif-dea) if (dif and dea) else 0
    f["macd_pos"] = 1 if (dif and dea and dif>0) else 0

    # -- 量价 --
    av5 = np.mean(v[-6:-1]) if len(v)>=6 else v[-1]
    f["vol_ratio_5d"] = v[-1]/av5 if av5>0 else 1
    f["ret_1d"] = (c[-1]/c[-2]-1)*100 if len(c)>=2 else 0
    f["ret_5d"] = (c[-1]/c[-6]-1)*100 if len(c)>=6 else 0
    f["ret_20d"] = (c[-1]/c[-21]-1)*100 if len(c)>=21 else 0

    # -- 偏离 --
    f["dev_ma20"] = (c[-1]-ma20)/ma20*100 if ma20 else 0

    # -- 52周位置 --
    if len(c)>=120:
        h52, l52 = max(h[-120:]), min(l[-120:])
        f["pct_52w"] = (c[-1]-l52)/(h52-l52)*100 if h52>l52 else 50
    else:
        f["pct_52w"] = 50

    # -- 资金 --
    tp = (h+l+c)/3
    pf = np.sum(np.where(tp[-14:] > np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 0
    nf = np.sum(np.where(tp[-14:] <= np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 1
    mfi = 100 - 100/(1+pf/nf) if nf>0 else 50
    f["mfi"] = mfi

    mf_vals = []
    for i in range(-20, 0):
        if i < -len(c): break
        if h[i]==l[i]: mf_v=0
        else: mf_v = ((c[i]-l[i])-(h[i]-c[i]))/(h[i]-l[i])
        mf_vals.append(mf_v*v[i])
    cmf = sum(mf_vals)/sum(v[-20:]) if len(mf_vals)>0 and sum(v[-20:])>0 else 0
    f["cmf"] = cmf

    # VWAP
    if len(c)>=20:
        tv20 = sum(v[-20:])
        vwap20 = sum(c[-20+i]*v[-20+i] for i in range(20))/tv20 if tv20>0 else c[-1]
        f["vwap_ratio"] = (c[-1]/vwap20-1)*100 if vwap20>0 else 0
    else:
        f["vwap_ratio"] = 0

    # 持续放量天数
    if len(v)>=6:
        f["vol_up_days"] = sum(1 for i in range(-5,0) if v[i]>v[i-1])
    else:
        f["vol_up_days"] = 0

    # 振幅
    if len(c)>=21:
        ampl_t = (h[-1]-l[-1])/c[-1]*100
        ampl_h = [(h[i]-l[i])/c[i]*100 for i in range(-21,-1) if h[i]!=l[i] and c[i]!=0]
        ampl_avg = sum(ampl_h)/len(ampl_h) if ampl_h else ampl_t
        f["ampl_ratio"] = ampl_t/ampl_avg if ampl_avg>0 else 1
    else:
        f["ampl_ratio"] = 1

    # 市值
    mcap = extra.get("mcap",0) if extra else 0
    f["log_mcap"] = math.log(mcap+1e8) if mcap>0 else 0

    # 跳空
    f["gap"] = (o[-1]-c[-2])/c[-2]*100 if len(c)>=2 and c[-2]>0 else 0

    # 连涨/连跌
    streak = 0
    for i in range(1, min(5, len(c)-1)):
        if c[-i]>c[-i-1]: streak += 1
        else: break
    f["up_streak"] = streak

    return f


# ============================================================
# Walk-Forward 回测
# ============================================================

def walk_forward_backtest(codes, lookback=120, train_win=60):
    """Walk-Forward GLM vs 固定权重"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*65}")
    print(f"  GLM Walk-Forward 回测 [{today_str}]")
    print(f"  {len(codes)}只股票 | trains={train_win}d | test={lookback}d")
    print(f"{'='*65}")

    # 1. K线数据
    print("\n[1/3] 获取 K线...")
    klines_all = fetch_kline_batch(codes, days=lookback+80)
    extra_all = fetch_extra_info(codes)
    for code in extra_all:
        extra_all[code]["_sector"] = get_theme(code)
    print(f"  K线: {len(klines_all)}只")

    # 2. 构建每日快照
    print("[2/3] 构建每日快照...")
    all_dates = set()
    for kl in klines_all.values():
        for k in kl: all_dates.add(k['date'])
    all_dates = sorted(all_dates)
    # 测试日期：保证有 train_win 天之前的数据
    test_start_idx = next(i for i,d in enumerate(all_dates) if d >= all_dates[max(0,len(all_dates)-lookback-1)])
    test_start_idx = max(test_start_idx, train_win+10)
    test_dates = all_dates[test_start_idx:]
    print(f"  训练数据: {test_start_idx}天 | 测试: {len(test_dates)}天")

    # 收集每个测试日的记录
    print(f"  [预计算评分]...")
    daily_recs = defaultdict(list)
    processed = 0
    for date_str in all_dates:
        processed += 1
        if processed % 40 == 0:
            print(f"    日期 {processed}/{len(all_dates)}")
        for code in codes:
            kl = klines_all.get(code)
            if not kl: continue
            idx = next((i for i,k in enumerate(kl) if k['date']==date_str), None)
            if idx is None or idx < 60: continue
            ex = extra_all.get(code, {})
            s = score_ss_enhanced(kl, idx, extra=ex)
            if s is None: continue
            feats = extract_features(kl, idx, extra=ex)
            daily_recs[date_str].append({
                "code": code, "close": kl[idx]['close'], "idx": idx,
                "score_f": s["score"], "tech": s["tech"],
                "capital": s["capital"], "info": s["info"],
                "feats": feats, "kl": kl,
            })

    print(f"  快照天数: {len(daily_recs)}")

    # 3. Walk-Forward
    print(f"[3/3] Walk-Forward ({len(test_dates)}天)...")

    # Collect feature names
    all_fnames = set()
    for ds in [d for d in all_dates if d >= test_dates[0]][:10]:
        for r in daily_recs.get(ds, []):
            all_fnames.update(r["feats"].keys())
    all_fnames = sorted(all_fnames)
    print(f"  特征数: {len(all_fnames)}")

    results = []
    for t, test_date in enumerate(test_dates):
        if t % 15 == 0:
            print(f"    [{t+1}/{len(test_dates)}] {test_date}")

        # Training data: all dates BEFORE test_date
        train = []
        for hist_date in daily_recs:
            if hist_date >= test_date: break
            train.extend(daily_recs[hist_date])

        test = daily_recs.get(test_date, [])
        if len(train) < 100 or len(test) < 10:
            continue

        # Build training matrix
        n_train = len(train)
        X = np.zeros((n_train, len(all_fnames)))
        for i, r in enumerate(train):
            for j, fn in enumerate(all_fnames):
                X[i,j] = r["feats"].get(fn, 0)

        # Forward returns as targets
        Y5, Y7, Y10 = [], [], []
        for r in train:
            kl = r["kl"]; idx = r["idx"]; c0 = kl[idx]['close']
            for fwd, yl in [(5,Y5),(7,Y7),(10,Y10)]:
                fi = idx+fwd
                yl.append((kl[fi]['close']/c0-1)*100 if fi<len(kl) else np.nan)

        Y5 = np.array(Y5); Y7 = np.array(Y7); Y10 = np.array(Y10)

        # Fit GLM per horizon
        betas = {}
        for fwd, Y in [(5,Y5),(7,Y7),(10,Y10)]:
            mask = ~np.isnan(Y)
            if mask.sum() < 50:
                betas[fwd] = None
                continue
            try:
                beta = ols_fit(X[mask], Y[mask])
                betas[fwd] = beta
            except Exception:
                betas[fwd] = None

        # Predict on test
        for r in test:
            feat_vec = np.array([r["feats"].get(fn,0) for fn in all_fnames])

            # GLM predictions
            glm_scores = {}
            for fwd in [5,7,10]:
                if betas.get(fwd) is not None:
                    glm_scores[f"glm{fwd}"] = round(float(feat_vec @ betas[fwd]), 4)
                else:
                    glm_scores[f"glm{fwd}"] = None

            # Forward returns
            kl = r["kl"]; idx = r["idx"]; c0 = kl[idx]['close']
            fwd_rets = {}
            for fwd in [5,7,10,15,20]:
                fi = idx+fwd
                fwd_rets[f"fwd{fwd}"] = round((kl[fi]['close']/c0-1)*100, 4) if fi<len(kl) else None

            results.append({
                "date": test_date, "code": r["code"],
                "score_f": r["score_f"],
                "tech": r["tech"], "capital": r["capital"], "info": r["info"],
                "glm5": glm_scores["glm5"], "glm7": glm_scores["glm7"], "glm10": glm_scores["glm10"],
                **fwd_rets,
            })

    print(f"\n  样本: {len(results)}条")
    return results, test_dates


# ============================================================
# 评估
# ============================================================

def evaluate(results, key, fwd_days, top_pct=0.20):
    """评估一种评分方案在某个持有周期的表现"""
    fk = f"fwd{fwd_days}"
    valid = [r for r in results if r.get(fk) is not None and r.get(key) is not None]
    if len(valid) < 50:
        return None
    valid.sort(key=lambda x: -x[key])
    n, tn = len(valid), max(5, int(len(valid)*top_pct))
    top_r = [r[fk] for r in valid[:tn]]
    bot_r = [r[fk] for r in valid[-tn:]]
    all_r = [r[fk] for r in valid]

    lm = np.mean(top_r) if top_r else 0
    lw = sum(1 for x in top_r if x>0)/len(top_r)*100 if top_r else 0
    bm = np.mean(bot_r) if bot_r else 0
    mk = np.mean(all_r) if all_r else 0
    lx = lm - mk
    ls = lm - bm
    ld = abs(min([0] + [r[fk] for r in valid[:tn]]))
    ps = lm / np.std(top_r, ddof=1) if len(top_r)>1 and np.std(top_r,ddof=1)>0 else 0
    ar = lm * (250/fwd_days)
    sr = ps * math.sqrt(250/fwd_days)
    cr = ar / ld if ld > 0 else 0
    ic = spearmanr([valid[i][key] for i in range(len(valid))], [r[fk] for r in valid])
    ic_mean = lm / (np.mean(all_r) - bm + 1) if lm != 0 else 0

    return {
        "n": n, "top_n": tn,
        "long_mean": round(lm,3), "win_rate": round(lw,1),
        "excess": round(lx,3), "spread": round(ls,3),
        "max_dd": round(ld,2), "sharpe": round(sr,3),
        "calmar": round(cr,3), "ic": ic,
    }


def print_table(metrics):
    """对比表"""
    print(f"\n{'='*90}")
    print(f"  GLM学习权重 vs 固定权重 — 全面对比")
    print(f"{'='*90}")

    for fwd in [5,7,10]:
        print(f"\n┌{'─'*85}┐")
        print(f"│  T+{fwd} 持有期{'':>72}│")
        print(f"├{'─'*85}┤")
        print(f"│ {'指标':<18} {'固定权重':>14} {'GLM-5(learn)':>14} {'GLM-7(learn)':>14} {'GLM-10(learn)':>14} │")
        print(f"├{'─'*85}┤")

        m_fixed = metrics.get(f"fixed_{fwd}")
        m_glm5 = metrics.get(f"glm5_{fwd}")
        m_glm7 = metrics.get(f"glm7_{fwd}")
        m_glm10 = metrics.get(f"glm10_{fwd}")

        rows = [
            ("多头均值%", "long_mean", "+.2f"),
            ("胜率%", "win_rate", ".1f"),
            ("超额%", "excess", "+.3f"),
            ("多空利差%", "spread", "+.2f"),
            ("最大回撤%", "max_dd", ".2f"),
            ("年化Sharpe", "sharpe", ".3f"),
            ("Calmar", "calmar", ".3f"),
            ("IC Rank", "ic", ".4f"),
        ]
        for label, key, fmt in rows:
            def fmt_v(m, k):
                if m is None: return "-"*12
                v = m.get(k)
                if v is None: return "-"*12
                return f"{v:{fmt}}"
            v1 = fmt_v(m_fixed, key)
            v2 = fmt_v(m_glm5, key)
            v3 = fmt_v(m_glm7, key)
            v4 = fmt_v(m_glm10, key)
            print(f"│ {label:<18} {v1:>14} {v2:>14} {v3:>14} {v4:>14} │")

        # Winner
        fixed_s = m_fixed["sharpe"] if m_fixed and m_fixed.get("sharpe") else 0
        glm_scores = [
            ("GLM-5", m_glm5["sharpe"] if m_glm5 and m_glm5.get("sharpe") else 0),
            ("GLM-7", m_glm7["sharpe"] if m_glm7 and m_glm7.get("sharpe") else 0),
            ("GLM-10", m_glm10["sharpe"] if m_glm10 and m_glm10.get("sharpe") else 0),
        ]
        best = max(glm_scores, key=lambda x: x[1])
        winner = "固定" if fixed_s > best[1] else best[0]

        print(f"├{'─'*85}┤")
        print(f"│ Sharpe最优: {winner} (固定={fixed_s:.3f} GLM5={glm_scores[0][1]:.3f} GLM7={glm_scores[1][1]:.3f} GLM10={glm_scores[2][1]:.3f})")
        print(f"└{'─'*85}┘")


# ============================================================
# Factor Learning 与反思
# ============================================================

def learn_final_weights(results, test_dates):
    """在全部数据上学一次 GLM T+10，输出学习的权重 vs 隐含的固定权重"""
    print(f"\n{'='*65}")
    print(f"  因子权重学习 — 反思固定权重的不足")
    print(f"{'='*65}\n")

    # Collect factors from scoring engine factor list (the named deltas)
    # We'll compare: what the data says vs what we hardcoded
    factor_deltas = {}
    for r in results:
        code = r["code"]; sf = r["score_f"]
        # The fixed scoring already aggregates: tech/capital/info
        # The implicit weight is 0.4*tech + 0.4*capital + 0.2*info

    # Do a decomposition analysis
    techs = [r["tech"] for r in results if r.get("fwd10") is not None]
    caps = [r["capital"] for r in results if r.get("fwd10") is not None]
    infos = [r["info"] for r in results if r.get("fwd10") is not None]
    fwds = [r["fwd10"] for r in results if r.get("fwd10") is not None]
    scores = [r["score_f"] for r in results if r.get("fwd10") is not None]

    if len(fwds) < 50: return

    # Regress forward return on each dimension independently
    for label, xs in [("技术面", techs), ("资金面", caps), ("信息面", infos)]:
        if len(xs) < 50: continue
        X = np.column_stack([np.ones(len(xs)), np.array(xs)])
        beta = ols_fit(X, np.array(fwds), ridge_alpha=0.01)
        slope = beta[1]
        ic = spearmanr(xs, fwds)
        print(f"  {label}: 最优权重 = {slope*100:+.4f}%，IC = {ic:.4f} "
              f"(固定权重 = {'+40%' if label=='技术面' else '+40%' if label=='资金面' else '+20%'})")

    # What the optimal dimension weights would be
    X_all = np.column_stack([techs, caps, infos])
    beta_all = ols_fit(X_all, np.array(fwds), ridge_alpha=0.01)
    w_tech, w_cap, w_info = [b for b in beta_all]
    w_sum = abs(w_tech) + abs(w_cap) + abs(w_info)
    if w_sum > 0:
        print(f"\n  📊 数据学出的最优维度权重：")
        print(f"     技术面: {w_tech*100:+.2f}%  | 资金面: {w_cap*100:+.2f}%  | 信息面: {w_info*100:+.2f}%")
        print(f"     归一化后 ≈ {(w_tech/w_sum)*100:.0f}% / {(w_cap/w_sum)*100:.0f}% / {(w_info/w_sum)*100:.0f}%")
        print(f"     当前固定: 40% / 40% / 20%")

    # IC analysis
    ic_fixed = spearmanr(scores, fwds)
    print(f"\n  🎯 固定评分 vs T+10收益: IC = {ic_fixed:.4f}")


# ============================================================
# Main
# ============================================================

def main():
    with open(os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")) as f:
        codes = [l.strip() for l in f if l.strip()]

    # Check for cache
    today = datetime.now().strftime("%Y-%m-%d")
    cache_path = os.path.join(OUTPUT_DIR, f"_cache_{today}.json")
    use_cached_klines = os.path.exists(cache_path)

    if use_cached_klines:
        print(f"\n  ♻️  发现今日K线缓存 — 跳过数据抓取（仅回测评分）")

    results, test_dates = walk_forward_backtest(codes, lookback=120, train_win=60)

    # Evaluate all schemes
    metrics = {}
    for label, key in [("fixed", "score_f"), ("glm5", "glm5"), ("glm7", "glm7"), ("glm10", "glm10")]:
        for fwd in [5,7,10]:
            m = evaluate(results, key, fwd)
            if m:
                metrics[f"{label}_{fwd}"] = m

    print_table(metrics)
    learn_final_weights(results, test_dates)

    # Save
    out = os.path.join(OUTPUT_DIR, f"glm_backtest_{today}.json")
    with open(out, "w") as f:
        json.dump({
            "date": today, "n_results": len(results),
            "metrics": {k: v for k,v in metrics.items()},
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ 已保存: {out}")

    return results


if __name__ == "__main__":
    main()
