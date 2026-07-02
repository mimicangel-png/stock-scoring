#!/usr/bin/env python3
"""
SS-Enhanced V9 — 超越 GLM 的方案对比回测
=========================================
在 V8 GLM 的基础上，实现并对比 5 种改进方案：

  A. 线性 GLM（基准，V8）        — OLS on raw features
  B. 多项式 GLM                   — 二次项 + 交互项，捕获非线性
  C. 时间加权 GLM                 — 指数衰减训练权重
  D. 组合评分（集成）              — 多周期 GLM 预测的组合
  E. 混合评分                     — B + C 的结合

每个方案都做 Walk-Forward，指标对比 T+5/7/10。
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

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ================================================================
# 工具函数
# ================================================================

def ols_fit(X, y, ridge=0.1, sample_weights=None):
    """加权 Ridge 回归"""
    n, p = X.shape
    if sample_weights is not None:
        W = np.diag(np.sqrt(sample_weights))
        Xw = W @ X
        yw = W @ y
    else:
        Xw, yw = X, y
    XtX = Xw.T @ Xw + ridge * np.eye(p)
    Xty = Xw.T @ yw
    try:
        return np.linalg.solve(XtX, Xty)
    except np.linalg.LinAlgError:
        return np.linalg.lstsq(Xw, yw, rcond=None)[0]

def spearmanr(x, y):
    n = len(x); 
    if n < 3: return 0
    xr = np.argsort(np.argsort(x)).astype(float) + 1
    yr = np.argsort(np.argsort(y)).astype(float) + 1
    d = xr - yr; return round(float(1 - 6*np.sum(d**2)/(n*(n**2-1))), 6)

# ================================================================
# 特征工程
# ================================================================

def extract_features(klines, idx, extra=None):
    """提取原始连续特征（同 V8）"""
    w = klines[:idx+1]
    c = np.array([k['close'] for k in w], dtype=float)
    v = np.array([k['volume'] for k in w], dtype=float)
    h = np.array([k['high'] for k in w], dtype=float)
    l = np.array([k['low'] for k in w], dtype=float)
    o = np.array([k['open'] for k in w], dtype=float)
    f = {}

    ma5, ma10, ma20, ma60 = calc_ma(c,5), calc_ma(c,10), calc_ma(c,20), calc_ma(c,60)
    f["trend_5_10"] = (ma5/ma10-1)*100 if (ma5 and ma10) else 0
    f["trend_10_20"] = (ma10/ma20-1)*100 if (ma10 and ma20) else 0
    f["ma_bull"] = 1 if (ma5 and ma10 and ma20 and ma5>ma10>ma20) else 0
    f["ma_bear"] = 1 if (ma5 and ma10 and ma20 and ma5<ma10<ma20) else 0

    rsi = calc_rsi(c)
    f["rsi"] = rsi
    f["rsi_dev"] = abs(rsi - 50)  # deviation from neutral (U-shape proxy)

    dif, dea = calc_ema(c,12), calc_ema(c,26)
    f["macd"] = (dif-dea)/c[-1]*1000 if (dif and dea and c[-1]>0) else 0

    av5 = np.mean(v[-6:-1]) if len(v)>=6 else v[-1]
    f["vol_ratio_5d"] = v[-1]/av5 if av5>0 else 1
    f["ret_5d"] = (c[-1]/c[-6]-1)*100 if len(c)>=6 else 0
    f["ret_20d"] = (c[-1]/c[-21]-1)*100 if len(c)>=21 else 0

    f["dev_ma20"] = (c[-1]-ma20)/ma20*100 if ma20 else 0

    if len(c)>=120:
        h52, l52 = max(h[-120:]), min(l[-120:])
        f["pct_52w"] = (c[-1]-l52)/(h52-l52)*100 if h52>l52 else 50
    else:
        f["pct_52w"] = 50

    # 资金
    tp = (h+l+c)/3
    pf = np.sum(np.where(tp[-14:] > np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 0
    nf = np.sum(np.where(tp[-14:] <= np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 1
    f["mfi"] = 100 - 100/(1+pf/nf) if nf>0 else 50

    mf_vals = []
    for i in range(-20, 0):
        if i < -len(c): break
        if h[i]==l[i]: mf_v=0
        else: mf_v = ((c[i]-l[i])-(h[i]-c[i]))/(h[i]-l[i])
        mf_vals.append(mf_v*v[i])
    cmf = sum(mf_vals)/sum(v[-20:]) if len(mf_vals)>0 and sum(v[-20:])>0 else 0
    f["cmf"] = cmf

    if len(c)>=20:
        tv20 = sum(v[-20:])
        vwap20 = sum(c[-20+i]*v[-20+i] for i in range(20))/tv20 if tv20>0 else c[-1]
        f["vwap_ratio"] = (c[-1]/vwap20-1)*100 if vwap20>0 else 0
    else:
        f["vwap_ratio"] = 0

    if len(v)>=6:
        f["vol_up_days"] = sum(1 for i in range(-5,0) if v[i]>v[i-1])
    else:
        f["vol_up_days"] = 0

    if len(c)>=21:
        ampl_t = (h[-1]-l[-1])/c[-1]*100
        ampl_h = [(h[i]-l[i])/c[i]*100 for i in range(-21,-1) if h[i]!=l[i] and c[i]!=0]
        ampl_avg = sum(ampl_h)/len(ampl_h) if ampl_h else ampl_t
        f["ampl_ratio"] = ampl_t/ampl_avg if ampl_avg>0 else 1
    else:
        f["ampl_ratio"] = 1

    mcap = extra.get("mcap",0) if extra else 0
    f["log_mcap"] = math.log(mcap+1e8) if mcap>0 else 0

    f["gap"] = (o[-1]-c[-2])/c[-2]*100 if len(c)>=2 and c[-2]>0 else 0

    streak = 0
    for i in range(1, min(5, len(c)-1)):
        if c[-i]>c[-i-1]: streak += 1
        else: break
    f["up_streak"] = streak

    return f


def expand_polynomial(X_raw, fnames):
    """对原始特征矩阵添加二次项和关键交互项"""
    n, p = X_raw.shape
    new_cols = []
    new_names = []

    # 二次项（对连续特征）
    continuous_features = [
        "trend_5_10", "trend_10_20", "rsi", "rsi_dev", "macd",
        "vol_ratio_5d", "ret_5d", "ret_20d", "dev_ma20", "pct_52w",
        "mfi", "cmf", "vwap_ratio", "vol_up_days", "ampl_ratio",
        "log_mcap", "gap"
    ]

    for fn in continuous_features:
        if fn in fnames:
            j = fnames.index(fn)
            x = X_raw[:, j]
            new_cols.append(x**2)
            new_names.append(f"{fn}^2")
            # 也加上 sign(x)*x² 来区分方向
            new_cols.append(np.sign(x) * x**2)
            new_names.append(f"sgn({fn})*{fn}^2")

    # 关键交互项
    interactions = [
        ("rsi", "vol_ratio_5d"),
        ("trend_5_10", "gap"),
        ("cmf", "ret_5d"),
        ("pct_52w", "dev_ma20"),
        ("ampl_ratio", "vol_ratio_5d"),
        ("mfi", "rsi"),
        ("vol_up_days", "ret_5d"),
        ("gap", "ret_20d"),
        ("cmf", "ma_bull"),
    ]

    for a_name, b_name in interactions:
        if a_name in fnames and b_name in fnames:
            a = X_raw[:, fnames.index(a_name)]
            b = X_raw[:, fnames.index(b_name)]
            new_cols.append(a * b)
            new_names.append(f"{a_name}×{b_name}")

    if new_cols:
        return np.column_stack([X_raw] + new_cols), fnames + new_names
    return X_raw, fnames


# ================================================================
# 方案 A: 线性 GLM
# ================================================================

class LinearGLM:
    def fit(self, X, y, sample_weights=None):
        self.beta = ols_fit(X, y, ridge=0.1, sample_weights=sample_weights)
    def predict(self, X):
        return X @ self.beta


# ================================================================
# 方案 B: 多项式 GLM
# ================================================================

class PolyGLM:
    def fit(self, X_raw, y, fnames, sample_weights=None):
        X_poly, self.poly_fnames = expand_polynomial(X_raw, fnames)
        self.beta = ols_fit(X_poly, y, ridge=0.5, sample_weights=sample_weights)
    def predict(self, X_raw, fnames):
        X_poly, _ = expand_polynomial(X_raw, fnames)
        return X_poly @ self.beta


# ================================================================
# 方案 E: 混合评分（多项式 + 时间加权）
# ================================================================

class HybridGLM(PolyGLM):
    def fit(self, X_raw, y, fnames, train_day_indices):
        """带时间加权的多项式 GLM"""
        n = len(train_day_indices)
        max_idx = max(train_day_indices)
        # 指数衰减: 最近样本权重=1, 60天前权重≈0.37
        weights = np.exp(-0.02 * (max_idx - np.array(train_day_indices)))
        super().fit(X_raw, y, fnames, sample_weights=weights)


# ================================================================
# Walk-Forward 对比回测
# ================================================================

def walk_forward_compare(codes, lookback=120, train_win=60):
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*70}")
    print(f"  SS-Enhanced V9 — 5种方案对比回测 [{today_str}]")
    print(f"  {len(codes)}只股票 | trains={train_win}d | test={lookback}d")
    print(f"{'='*70}")

    # 1. K线
    print("\n[1/3] 获取 K线...")
    klines_all = fetch_kline_batch(codes, days=lookback+80)
    extra_all = fetch_extra_info(codes)
    for code in extra_all: extra_all[code]["_sector"] = get_theme(code)
    print(f"  K线: {len(klines_all)}只")

    # 2. 构建每日快照
    print("[2/3] 构建每日快照...")
    all_dates = set()
    for kl in klines_all.values():
        for k in kl: all_dates.add(k['date'])
    all_dates = sorted(all_dates)
    test_start = next(i for i,d in enumerate(all_dates) if d>=all_dates[max(0,len(all_dates)-lookback-1)])
    test_start = max(test_start, train_win+10)
    test_dates = all_dates[test_start:]
    print(f"  测试: {len(test_dates)}天")

    daily_recs = defaultdict(list)
    processed = 0
    for date_str in all_dates:
        processed += 1
        if processed % 50 == 0: print(f"    日期 {processed}/{len(all_dates)}")
        for code in codes:
            kl = klines_all.get(code); 
            if not kl: continue
            idx = next((i for i,k in enumerate(kl) if k['date']==date_str), None)
            if idx is None or idx < 60: continue
            ex = extra_all.get(code, {})
            s = score_ss_enhanced(kl, idx, extra=ex)
            if s is None: continue
            feats = extract_features(kl, idx, extra=ex)
            daily_recs[date_str].append({
                "code":code, "close":kl[idx]['close'], "idx":idx,
                "score_f":s["score"], "tech":s["tech"],
                "capital":s["capital"], "info":s["info"],
                "feats":feats, "kl":kl,
            })

    # 3. Walk-Forward with all 5 schemes
    print(f"[3/3] Walk-Forward ({len(test_dates)}天)...")

    all_fnames = set()
    for ds in [d for d in all_dates if d>=test_dates[0]][:5]:
        for r in daily_recs.get(ds, []): all_fnames.update(r["feats"].keys())
    all_fnames = sorted(all_fnames)
    print(f"  特征数: {len(all_fnames)}")

    schemes = {
        "A_linear": "线性 GLM",
        "B_poly": "多项式 GLM",
        "E_hybrid": "混合 GLM",
    }

    results = []
    for t, test_date in enumerate(test_dates):
        if t % 20 == 0:
            print(f"    [{t+1}/{len(test_dates)}] {test_date}")

        train = []; train_day_idx = []
        for hi, hist_date in enumerate(all_dates):
            if hist_date >= test_date: break
            for rec in daily_recs.get(hist_date, []):
                train.append(rec)
                train_day_idx.append(hi)
                
        test = daily_recs.get(test_date, [])
        if len(train) < 100 or len(test) < 10: continue

        # Build training data
        n_train = len(train)
        X_raw = np.zeros((n_train, len(all_fnames)))
        for i, r in enumerate(train):
            for j, fn in enumerate(all_fnames):
                X_raw[i,j] = r["feats"].get(fn, 0)

        # Forward returns
        Y10 = np.array([(r["kl"][r["idx"]+10]['close']/r["kl"][r["idx"]]['close']-1)*100
                        if r["idx"]+10 < len(r["kl"]) else np.nan for r in train])

        mask = ~np.isnan(Y10)
        if mask.sum() < 50: continue
        Xm, Ym = X_raw[mask], Y10[mask]
        day_idx_m = [train_day_idx[i] for i in range(n_train) if mask[i]]

        # Fit all schemes
        models = {}
        # A: Linear
        ma = LinearGLM(); ma.fit(Xm, Ym)
        models["A_linear"] = ma

        # B: Poly
        mb = PolyGLM(); mb.fit(Xm, Ym, all_fnames)
        models["B_poly"] = mb

        # E: Hybrid (poly + time-weighted)
        me = HybridGLM(); me.fit(Xm, Ym, all_fnames, day_idx_m)
        models["E_hybrid"] = me

        # Predict on test
        X_test = np.zeros((len(test), len(all_fnames)))
        for i, r in enumerate(test):
            for j, fn in enumerate(all_fnames):
                X_test[i,j] = r["feats"].get(fn, 0)

        preds = {}
        for name, model in models.items():
            if isinstance(model, (PolyGLM, HybridGLM)):
                preds[name] = model.predict(X_test, all_fnames)
            else:
                preds[name] = model.predict(X_test)

        for i, r in enumerate(test):
            kl = r["kl"]; idx = r["idx"]; c0 = kl[idx]['close']
            fwd_rets = {}
            for fwd in [5,7,10,15]:
                fi = idx+fwd
                fwd_rets[f"fwd{fwd}"] = round((kl[fi]['close']/c0-1)*100,4) if fi<len(kl) else None

            rec = {
                "date": test_date, "code": r["code"],
                "score_f": r["score_f"],
            }
            for name in schemes:
                rec[f"score_{name}"] = float(preds[name][i])
            rec.update(fwd_rets)
            results.append(rec)

    print(f"\n  有效样本: {len(results)} 条")
    return results


# ================================================================
# 评估与对比
# ================================================================

def evaluate(results, key, fwd_days, top_pct=0.20):
    fk = f"fwd{fwd_days}"
    valid = [r for r in results if r.get(fk) is not None and r.get(key) is not None]
    if len(valid) < 50: return None
    valid.sort(key=lambda x: -x[key])
    n, tn = len(valid), max(5, int(len(valid)*top_pct))
    top_r = [r[fk] for r in valid[:tn]]
    bot_r = [r[fk] for r in valid[-tn:]]
    all_r = [r[fk] for r in valid]
    lm=np.mean(top_r); lw=sum(1 for x in top_r if x>0)/len(top_r)*100
    bm=np.mean(bot_r); mk=np.mean(all_r)
    lx=lm-mk; ls=lm-bm; ld=abs(min([0]+[r[fk] for r in valid[:tn]]))
    ps = lm/np.std(top_r,ddof=1) if len(top_r)>1 and np.std(top_r,ddof=1)>0 else 0
    return {
        "n":n,"top_n":tn,"long_mean":round(lm,3),"win_rate":round(lw,1),
        "excess":round(lx,3),"spread":round(ls,3),"max_dd":round(ld,2),
        "sharpe":round(ps*math.sqrt(250/fwd_days),3),
        "calmar":round(lm*(250/fwd_days)/ld,3) if ld>0 else 0,
        "ic":spearmanr([r[key] for r in valid],[r[fk] for r in valid]),
    }


def print_final_table(results):
    print(f"\n{'='*95}")
    print(f"  5种方案 vs 固定权重 — 最终对比")
    print(f"{'='*95}")

    names = [
        ("score_f", "固定权重(40/40/20)"),
        ("score_A_linear", "A. 线性GLM"),
        ("score_B_poly", "B. 多项式GLM"),
        ("score_E_hybrid", "E. 混合GLM(时间加权+多项式)"),
    ]

    for fwd in [5,7,10]:
        winner_name, winner_sharpe = "", 0
        print(f"\n┌{'─'*90}┐")
        print(f"│{'T+'+str(fwd)+' 持有期':>91}│")
        print(f"├{'─'*90}┤")

        header = "│" + "指标".ljust(16)
        for _, label in names:
            header += label.center(18)
        header += "│"
        print(header)
        print(f"├{'─'*90}┤")

        for metric_key, metric_label in [
            ("long_mean","多头均值%"),("win_rate","胜率%"),("excess","超额%"),
            ("spread","多空利差%"),("max_dd","最大回撤%"),("sharpe","Sharpe"),("ic","IC Rank"),
        ]:
            row = "│" + metric_label.ljust(16)
            for key, _ in names:
                m = evaluate(results, key, fwd)
                if m and m.get(metric_key) is not None:
                    v = m[metric_key]
                    if isinstance(v, float):
                        row += f"{v:+8.3f}".rjust(18) if v != 0 else "        -".rjust(18)
                    else:
                        row += str(v).rjust(18)
                else:
                    row += "        -".rjust(18)
            row += "│"
            print(row)

        # Winner
        for key, label in names:
            m = evaluate(results, key, fwd)
            if m and m.get("sharpe",0) > winner_sharpe:
                winner_sharpe = m["sharpe"]
                winner_name = label

        print(f"├{'─'*90}┤")
        print(f"│ Sharpe最优: {winner_name} ({winner_sharpe:.3f}){'':>40}│")
        print(f"└{'─'*90}┘")


# ================================================================
# Main
# ================================================================

def main():
    with open(os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")) as f:
        codes = [l.strip() for l in f if l.strip()]

    results = walk_forward_compare(codes, lookback=120, train_win=60)
    print_final_table(results)

    today = datetime.now().strftime("%Y-%m-%d")
    out = os.path.join(OUTPUT_DIR, f"v9_backtest_{today}.json")
    # Save compact
    with open(out, "w") as f:
        json.dump({"date":today,"n":len(results)}, f)
    print(f"\n  ✅ 已保存: {out}")

if __name__ == "__main__":
    main()
