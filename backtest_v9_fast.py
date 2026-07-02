#!/usr/bin/env python3
"""
V9 Fast — 超越 GLM 的方案对比（跳过评分，纯特征+GLM）
========================================================
- 固定分数：从已生成的 SS增强版 JSON 加载
- 特征提取：纯 numpy，无需 scoring_engine 打分
- Walk-Forward：线性/多项式/时间加权三种方案
- 总耗时 < 60 秒（271只股票）
"""
import json, math, os, sys
from datetime import datetime
from collections import defaultdict
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from scoring_engine import (
    fetch_kline_batch, fetch_extra_info, get_theme,
    calc_ma, calc_rsi, calc_ema
)

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ================================================================
# OLS / 指标
# ================================================================
def ols_fit(X, y, ridge=0.1, sample_weights=None):
    n, p = X.shape
    if sample_weights is not None:
        W = np.diag(np.sqrt(sample_weights))
        Xw, yw = W @ X, W @ y
    else:
        Xw, yw = X, y
    XtX = Xw.T @ Xw + ridge * np.eye(p)
    try: return np.linalg.solve(XtX, Xw.T @ yw)
    except: return np.linalg.lstsq(Xw, yw, rcond=None)[0]

def spearmanr(x, y):
    n = len(x)
    if n < 3: return 0
    xr = np.argsort(np.argsort(x)).astype(float)+1
    yr = np.argsort(np.argsort(y)).astype(float)+1
    return round(float(1-6*np.sum((xr-yr)**2)/(n*(n**2-1))), 6)

# ================================================================
# 快速特征提取（纯 numpy，无循环中的 score_ss_enhanced）
# ================================================================
def extract_features(klines, idx, extra=None):
    w = klines[:idx+1]
    c = np.array([k['close'] for k in w], dtype=float)
    v = np.array([k['volume'] for k in w], dtype=float)
    h = np.array([k['high'] for k in w], dtype=float)
    l = np.array([k['low'] for k in w], dtype=float)
    o = np.array([k['open'] for k in w], dtype=float)
    f = {}
    ma5, ma10, ma20 = calc_ma(c,5), calc_ma(c,10), calc_ma(c,20)
    f["trend"] = (ma5/ma10-1)*100 if (ma5 and ma10) else 0
    f["ma_bull"] = 1 if (ma5 and ma10 and ma20 and ma5>ma10>ma20) else 0
    rsi = calc_rsi(c); f["rsi"] = rsi; f["rsi_sq"] = rsi**2
    dif, dea = calc_ema(c,12), calc_ema(c,26)
    f["macd"] = (dif-dea)/c[-1]*1000 if (dif and dea and c[-1]>0) else 0
    av5 = np.mean(v[-6:-1]) if len(v)>=6 else v[-1]
    f["vol_r5"] = v[-1]/av5 if av5>0 else 1
    f["ret5d"] = (c[-1]/c[-6]-1)*100 if len(c)>=6 else 0
    f["dev_ma20"] = (c[-1]-ma20)/ma20*100 if ma20 else 0
    if len(c)>=120:
        h52,l52 = max(h[-120:]),min(l[-120:])
        f["pct52"] = (c[-1]-l52)/(h52-l52)*100 if h52>l52 else 50
    else: f["pct52"] = 50
    tp = (h+l+c)/3
    pf = np.sum(np.where(tp[-14:]>np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 0
    nf = np.sum(np.where(tp[-14:]<=np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 1
    f["mfi"] = 100-100/(1+pf/nf) if nf>0 else 50
    mf_v = []
    for i in range(-20,0):
        if i<-len(c): break
        mf_v.append(((c[i]-l[i])-(h[i]-c[i]))/(h[i]-l[i])*v[i] if h[i]!=l[i] else 0)
    f["cmf"] = sum(mf_v)/sum(v[-20:]) if mf_v and sum(v[-20:])>0 else 0
    if len(v)>=6: f["vol_up"] = sum(1 for i in range(-5,0) if v[i]>v[i-1])
    else: f["vol_up"] = 0
    mcap = extra.get("mcap",0) if extra else 0
    f["log_mcap"] = math.log(mcap+1e8) if mcap>0 else 0
    f["gap"] = (o[-1]-c[-2])/c[-2]*100 if len(c)>=2 and c[-2]>0 else 0
    streak = 0
    for i in range(1, min(5,len(c)-1)):
        if c[-i]>c[-i-1]: streak+=1
        else: break
    f["streak"] = streak
    return f

# ================================================================
# 对比回测
# ================================================================
def main():
    print(f"\n{'='*60}")
    print(f"  V9 Fast — GLM 多项式 vs 线性 vs 时间加权")
    print(f"{'='*60}")

    with open(os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")) as f:
        codes = [l.strip() for l in f if l.strip()]

    # 1. 加载固定分数（从已生成的 JSON）
    today = datetime.now().strftime("%Y-%m-%d")
    fixed_file = os.path.join(OUTPUT_DIR, f"SS增强版评分_{today}.json")
    fixed_scores = {}
    if os.path.exists(fixed_file):
        with open(fixed_file) as f:
            d = json.load(f)
        for r in d.get("results", []):
            fixed_scores[r["code"]] = r["score"]
        print(f"  加载固定分数: {len(fixed_scores)} 只")
    else:
        print("  ⚠️ 无固定分数文件，跳过固定权重对比")
        fixed_scores = {}

    # 2. K线
    print("[1] K线 (DB缓存)...")
    klines_all = fetch_kline_batch(codes, days=200)
    extra_all = fetch_extra_info(codes)
    print(f"  {len(klines_all)}只")

    # 3. 构建每日快照（只提取特征，不打分）
    print("[2] 每日快照...")
    all_dates = sorted(set(k['date'] for kl in klines_all.values() for k in kl))
    train_win, lookback = 60, 120
    test_start = max(train_win+10, len(all_dates)-lookback-1)

    daily_recs = defaultdict(list)
    t0 = datetime.now()
    for di, date_str in enumerate(all_dates):
        if di % 30 == 0: print(f"  日期 {di}/{len(all_dates)} ({datetime.now()-t0})")
        for code in codes:
            kl = klines_all.get(code)
            if not kl: continue
            idx = next((i for i,k in enumerate(kl) if k['date']==date_str), None)
            if idx is None or idx < 60: continue
            ex = extra_all.get(code, {})
            feats = extract_features(kl, idx, extra=ex)
            sf = fixed_scores.get(code)
            daily_recs[date_str].append({
                "code": code, "close": kl[idx]['close'], "idx": idx,
                "score_f": sf, "feats": feats, "kl": kl,
            })

    test_dates = all_dates[test_start:]
    print(f"  快照: {len(daily_recs)}天 | 测试: {len(test_dates)}天 | 耗时: {datetime.now()-t0}")

    # 4. Walk-Forward
    print(f"[3] Walk-Forward ({len(test_dates)}天)...")
    all_fnames = sorted(set().union(*[r["feats"].keys() for recs in list(daily_recs.values())[:20] for r in recs]))
    print(f"  特征: {len(all_fnames)}")

    results = []
    t0 = datetime.now()
    for t, test_date in enumerate(test_dates):
        if t % 10 == 0: print(f"  [{t+1}/{len(test_dates)}] {test_date} ({datetime.now()-t0})")

        train, train_di = [], []
        for hi, hist_date in enumerate(all_dates):
            if hist_date >= test_date: break
            for rec in daily_recs.get(hist_date, []):
                train.append(rec); train_di.append(hi)
        test = daily_recs.get(test_date, [])
        if len(train) < 100 or len(test) < 10: continue

        # Build X
        n_train = len(train)
        X_raw = np.zeros((n_train, len(all_fnames)))
        for i, r in enumerate(train):
            for j, fn in enumerate(all_fnames):
                X_raw[i,j] = r["feats"].get(fn, 0)

        # Y10
        Y10 = np.array([
            (r["kl"][r["idx"]+10]['close']/r["kl"][r["idx"]]['close']-1)*100
            if r["idx"]+10 < len(r["kl"]) else np.nan for r in train
        ])
        mask = ~np.isnan(Y10)
        if mask.sum() < 50: continue
        Xm, Ym = X_raw[mask], Y10[mask]
        di_m = [train_di[i] for i in range(n_train) if mask[i]]

        # A: Linear
        bA = ols_fit(Xm, Ym)

        # B: Poly (add squared + interaction terms)
        X_poly = Xm.copy()
        for i, fn_i in enumerate(all_fnames):
            X_poly = np.column_stack([X_poly, Xm[:,i]**2])
        # key interactions
        pairs = [("rsi","vol_r5"),("trend","gap"),("cmf","ret5d"),("rsi","dev_ma20"),("trend","pct52")]
        for a,b in pairs:
            if a in all_fnames and b in all_fnames:
                X_poly = np.column_stack([X_poly, Xm[:,all_fnames.index(a)]*Xm[:,all_fnames.index(b)]])
        bB = ols_fit(X_poly, Ym, ridge=1.0)

        # E: Time-weighted Poly
        max_di = max(di_m)
        weights = np.exp(-0.02 * (max_di - np.array(di_m)))
        bE = ols_fit(X_poly, Ym, ridge=1.0, sample_weights=weights)

        # Predict test
        X_test = np.zeros((len(test), len(all_fnames)))
        for i, r in enumerate(test):
            for j, fn in enumerate(all_fnames):
                X_test[i,j] = r["feats"].get(fn, 0)

        Xp_test = X_test.copy()
        for i, fn_i in enumerate(all_fnames):
            Xp_test = np.column_stack([Xp_test, X_test[:,i]**2])
        for a,b in pairs:
            if a in all_fnames and b in all_fnames:
                Xp_test = np.column_stack([Xp_test, X_test[:,all_fnames.index(a)]*X_test[:,all_fnames.index(b)]])

        pA = X_test @ bA
        pB = Xp_test @ bB
        pE = Xp_test @ bE

        for i, r in enumerate(test):
            kl=r["kl"]; idx=r["idx"]; c0=kl[idx]['close']
            fwd={}
            for fw in [5,7,10,15]:
                fi=idx+fw
                fwd[f"fwd{fw}"]=round((kl[fi]['close']/c0-1)*100,4) if fi<len(kl) else None
            results.append({
                "date":test_date,"code":r["code"],
                "score_f":r["score_f"],
                "glm_A":float(pA[i]),"glm_B":float(pB[i]),"glm_E":float(pE[i]),
                **fwd
            })

    print(f"  样本: {len(results)} | 总耗时: {datetime.now()-t0}")

    # 5. 评估
    def evaluate(results, key, fwd_days, top_pct=0.20):
        fk = f"fwd{fwd_days}"
        valid = [r for r in results if r.get(fk) is not None and r.get(key) is not None]
        if len(valid) < 50: return None
        valid.sort(key=lambda x: -x[key])
        n, tn = len(valid), max(5,int(len(valid)*top_pct))
        top_r = [r[fk] for r in valid[:tn]]
        bot_r = [r[fk] for r in valid[-tn:]]
        all_r = [r[fk] for r in valid]
        lm=np.mean(top_r); lw=sum(1 for x in top_r if x>0)/len(top_r)*100
        mk=np.mean(all_r); lx=lm-mk; ls=lm-np.mean(bot_r)
        ld=abs(min([0]+[r[fk] for r in valid[:tn]]))
        ps=lm/np.std(top_r,ddof=1) if len(top_r)>1 and np.std(top_r,ddof=1)>0 else 0
        return {
            "n":n,"top_n":tn,"long_mean":round(lm,3),"win_rate":round(lw,1),
            "excess":round(lx,3),"spread":round(ls,3),"max_dd":round(ld,2),
            "sharpe":round(ps*math.sqrt(250/fwd_days),3),
            "calmar":round(lm*(250/fwd_days)/ld,3) if ld>0 else 0,
            "ic":spearmanr([r[key] for r in valid],[r[fk] for r in valid]),
        }

    # 6. 打印对比表
    schemes = [
        ("score_f","固定权重(40/40/20)"),
        ("glm_A","A.线性GLM"),
        ("glm_B","B.多项式GLM(²+交互)"),
        ("glm_E","E.混合GLM(时间加权)"),
    ]

    for fwd in [5,7,10]:
        print(f"\n{'='*80}")
        print(f"  T+{fwd} 持有期")
        print(f"{'='*80}")
        print(f"  {'指标':<16}", end="")
        for _,label in schemes: print(f"  {label:<18}", end="")
        print()

        for mk, ml in [("long_mean","多头均值%"),("win_rate","胜率%"),("excess","超额%"),
                        ("spread","多空利差%"),("max_dd","最大回撤%"),("sharpe","年化Sharpe"),
                        ("calmar","Calmar"),("ic","IC Rank")]:
            print(f"  {ml:<16}", end="")
            best_val, best_name = -999, ""
            for key,_ in schemes:
                m = evaluate(results, key, fwd)
                v = m[mk] if m and mk in m else 0
                print(f"  {v:>+13.3f}" if isinstance(v,float) else f"  {str(v):>18}", end="")
                if mk == "sharpe" and v > best_val:
                    best_val, best_name = v, key
            print()
        winner = [l for k,l in schemes if k==best_name][0]
        print(f"  ✅ Sharpe最优: {winner} ({best_val:.3f})")

    print(f"\n  ✅ V9 Fast 完成")

if __name__ == "__main__":
    main()
