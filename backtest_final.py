#!/usr/bin/env python3
"""
终极对比回测：6种方案 × Walk-Forward × T+5/7/10
================================================================
A. V8 固定(40/40/20)    B. V9 固定(35/55/5/5)
C. 线性 GLM             D. 多项式 GLM(²+交互)
E. 时间加权 GLM          F. 集成 GLM

一步到位，选出最优方案并集成到每日评分管线。
"""
import json, math, os, sys, numpy as np
from datetime import datetime, timedelta
sys.path.insert(0, '/Users/bytedance/WorkBuddy/2026-06-24-18-57-20/stock-scoring')
from scoring_engine import fetch_kline_batch, fetch_extra_info, get_theme, score_ss_enhanced, calc_ma, calc_rsi, calc_ema

# ============================================================
def ols_fit(X, y, ridge=1.0, weights=None):
    """加权 Ridge 回归"""
    n, p = X.shape
    if weights is not None:
        W = np.diag(np.sqrt(weights))
        Xw, yw = W @ X, W @ y
    else: Xw, yw = X, y
    XtX = Xw.T @ Xw + ridge * np.eye(p)
    try: return np.linalg.solve(XtX, Xw.T @ yw)
    except: return np.linalg.lstsq(Xw, yw, rcond=None)[0]

# ============================================================
def extract_features(klines, idx, extra=None):
    """快速特征提取"""
    w = klines[:idx+1]
    c = np.array([k['close'] for k in w], float)
    v = np.array([k['volume'] for k in w], float)
    h = np.array([k['high'] for k in w], float)
    l = np.array([k['low'] for k in w], float)
    o = np.array([k['open'] for k in w], float)
    f = []
    ma5, ma10, ma20 = calc_ma(c,5), calc_ma(c,10), calc_ma(c,20)
    f.append((ma5/ma10-1)*100 if (ma5 and ma10) else 0)        # 0: trend
    f.append(1 if (ma5 and ma10 and ma20 and ma5>ma10>ma20) else 0)  # 1: ma_bull
    rsi = calc_rsi(c)
    f.append(rsi)                                              # 2: rsi
    f.append(rsi**2)                                           # 3: rsi²
    dif, dea = calc_ema(c,12), calc_ema(c,26)
    f.append((dif-dea)/c[-1]*1000 if (dif and dea and c[-1]>0) else 0)  # 4: macd
    av5 = np.mean(v[-6:-1]) if len(v)>=6 else v[-1]
    f.append(v[-1]/av5 if av5>0 else 1)                        # 5: vol_r5
    f.append((c[-1]/c[-6]-1)*100 if len(c)>=6 else 0)          # 6: ret5d
    f.append((c[-1]-ma20)/ma20*100 if ma20 else 0)             # 7: dev_ma20
    if len(c)>=120:
        h52,l52 = max(h[-120:]),min(l[-120:])
        f.append((c[-1]-l52)/(h52-l52)*100 if h52>l52 else 50) # 8: pct52
    else: f.append(50)
    # Capital features
    tp = (h+l+c)/3
    pf = np.sum(np.where(tp[-14:]>np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 0
    nf = np.sum(np.where(tp[-14:]<=np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 1
    f.append(100-100/(1+pf/nf) if nf>0 else 50)               # 9: mfi
    mf_v = [(c[i]-l[i]-(h[i]-c[i]))/(h[i]-l[i])*v[i] if h[i]!=l[i] else 0 for i in range(-20,0) if i>-len(c)]
    f.append(sum(mf_v)/sum(v[-20:]) if mf_v and sum(v[-20:])>0 else 0)  # 10: cmf
    f.append(sum(1 for i in range(-5,0) if v[i]>v[i-1]) if len(v)>=6 else 0)  # 11: vol_up
    mcap = extra.get("mcap",0) if extra else 0
    f.append(math.log(mcap+1e8) if mcap>0 else 0)              # 12: log_mcap
    f.append((o[-1]-c[-2])/c[-2]*100 if len(c)>=2 and c[-2]>0 else 0)  # 13: gap
    streak = 0
    for i in range(1, min(5,len(c)-1)):
        if c[-i]>c[-i-1]: streak+=1
        else: break
    f.append(float(streak))                                    # 14: streak
    return np.array(f, dtype=float)

def expand_poly(X):
    """添加二次项和关键交互项"""
    extra = []
    for i in range(X.shape[1]): extra.append(X[:,i]**2)  # 平方项
    # 关键交互
    pairs = [(2,5),(0,13),(10,6),(8,7),(5,6),(9,1),(11,6),(13,6)]
    for a,b in pairs:
        if a<X.shape[1] and b<X.shape[1]:
            extra.append(X[:,a]*X[:,b])
    return np.column_stack([X] + extra)

# ============================================================
def main():
    t0 = datetime.now()
    print(f"终极回测 {t0.strftime('%H:%M')}")

    with open('uploaded-stock-codes.txt') as f:
        codes = [l.strip() for l in f if l.strip()]

    print("[1] 数据..." )
    extra = fetch_extra_info(codes)
    for c in extra: extra[c]["_sector"] = get_theme(c)
    kall = fetch_kline_batch(codes, days=130)
    all_dates = sorted(set(k['date'] for kl in kall.values() for k in kl))
    test_start = max(60, len(all_dates)-110)
    test_dates = all_dates[test_start:]
    print(f"  K线{len(kall)}只 测试{len(test_dates)}天 {datetime.now()-t0}")

    # [2] 预计算特征和分数（一次性）
    print("[2] 特征..." )
    daily = {}  # {date: (X_matrix, codes_list, closes_list, kline_indices, techs, caps, infos, events)}
    for di, date_str in enumerate(all_dates):
        if di % 50 == 0: print(f"  {di}/{len(all_dates)}")
        feats, cd, cl, idxs, ts, cs, ins, evs = [],[],[],[],[],[],[],[]
        for code in codes:
            kl = kall.get(code)
            if not kl: continue
            idx = next((i for i,k in enumerate(kl) if k['date']==date_str), None)
            if idx is None or idx < 30: continue
            s = score_ss_enhanced(kl, idx, extra=extra.get(code,{}))
            if s is None: continue
            fv = extract_features(kl, idx, extra.get(code,{}))
            ev = sum(v[0] for v in s.get('event_summary',{}).values() if isinstance(v,list) and len(v)>0)
            feats.append(fv); cd.append(code); cl.append(kl[idx]['close'])
            idxs.append(idx); ts.append(s['tech']); cs.append(s['capital'])
            ins.append(s['info']); evs.append(ev)
        if feats:
            daily[date_str] = (np.array(feats), cd, cl, idxs, ts, cs, ins, evs)
    print(f"  特征日:{len(daily)} {datetime.now()-t0}")

    # [3] Walk-Forward 对比 6 种方案
    print(f"[3] Walk-Forward ({len(test_dates)}天)..." )
    results = []  # (date, code, v8, v9, glmC, glmD, glmE, fwd5, fwd7, fwd10, fwd15)

    for ti, test_date in enumerate(test_dates):
        train_X, train_Y10, train_days = [], [], []
        for hist_date in all_dates:
            if hist_date >= test_date: break
            d = daily.get(hist_date)
            if d is None: continue
            Xd, cd, cl, idxs = d[0], d[1], d[2], d[3]
            for j in range(len(cd)):
                kl = kall[cd[j]]
                idx = idxs[j]
                if idx+10 < len(kl):
                    train_X.append(Xd[j])
                    train_Y10.append((kl[idx+10]['close']/cl[j]-1)*100)
                    train_days.append(len(train_days))

        test_data = daily.get(test_date)
        if test_data is None or len(train_X) < 200: continue
        Xt, cdt, clt, idxst, tst, cst, inst, evst = test_data
        n_test = len(cdt)

        # Train T+10 GLM (will use for prediction)
        X_tr = np.array(train_X); Y_tr = np.array(train_Y10)
        beta_linear = ols_fit(X_tr, Y_tr, 1.0)

        Xp_tr = expand_poly(X_tr)
        beta_poly = ols_fit(Xp_tr, Y_tr, 2.0)

        # Time-weighted poly
        n_tr = len(train_days)
        tw = np.exp(-0.02 * (n_tr - 1 - np.array(train_days)))
        beta_tw = ols_fit(Xp_tr, Y_tr, 2.0, tw)

        # Predict
        Xp_t = expand_poly(Xt)
        pred_linear = Xt @ beta_linear
        pred_poly = Xp_t @ beta_poly
        pred_tw = Xp_t @ beta_tw
        pred_ensemble = (pred_linear + pred_poly + pred_tw) / 3  # Simple average

        for j in range(n_test):
            code = cdt[j]; kl = kall[code]; idx = idxst[j]; c0 = clt[j]
            ev = evst[j]; evn = 25 + max(-25, min(25, ev))
            v8 = max(5,min(95,round(tst[j]*0.4 + cst[j]*0.4 + (inst[j]+ev)*0.2)))
            v9 = max(5,min(95,round(tst[j]*0.35 + cst[j]*0.55 + inst[j]*0.05 + evn*0.05)))

            def fwd_get(k, fw):
                return round((k[idx+fw]['close']/c0-1)*100,4) if idx+fw<len(kl) and kl[idx+fw]['close']>0 else None

            results.append((
                test_date, code, v8, v9,
                float(pred_linear[j]), float(pred_poly[j]),
                float(pred_tw[j]), float(pred_ensemble[j]),
                fwd_get(kl,5), fwd_get(kl,7), fwd_get(kl,10), fwd_get(kl,15)
            ))

        if ti % 15 == 0:
            print(f"  [{ti+1}/{len(test_dates)}] {test_date} samples:{len(results)}")

    print(f"  样本:{len(results)} 耗时:{str(datetime.now()-t0).split('.')[0]}")

    # [4] 评估
    schemes = [
        (2, "A.V8固定"),
        (3, "B.V9固定"),
        (4, "C.线性GLM"),
        (5, "D.多项式GLM"),
        (6, "E.时间加权"),
        (7, "F.集成GLM"),
    ]
    fwd_map = {8:5, 9:7, 10:10, 11:15}

    def evalr(recs, si, fi, pct=0.15):
        v = sorted([r for r in recs if r[fi] is not None], key=lambda x:-x[si])
        if len(v) < 20: return [0,0,0,0]
        tn = max(5, int(len(v)*pct))
        t = [r[fi] for r in v[:tn]]; a = [r[fi] for r in v]
        lm=np.mean(t); lw=sum(1 for x in t if x>0)/len(t)*100
        ex=lm-np.mean(a)
        s = lm/np.std(t,ddof=1)*math.sqrt(250/fwd_map[fi]) if np.std(t,ddof=1)>0 else 0
        ic = np.corrcoef([r[si] for r in v], [r[fi] for r in v])[0,1]
        return [round(lm,3), round(lw,1), round(ex,3), round(s,3), round(ic or 0,4)]

    print("\n" + "="*90)
    print(f"  终极对比：{len(schemes)}种方案 × 3周期 × Walk-Forward")
    print("="*90)
    for lbl, fi in [("T+5",8),("T+7",9),("T+10",10)]:
        print(f"\n  {lbl}:")
        print(f"  {'方案':<14} {'Sharpe':>8} {'均值%':>8} {'胜率%':>7} {'超额%':>8} {'IC':>8}")
        best_s, best_n = -99, ""
        for si, name in schemes:
            m = evalr(results, si, fi)
            s = m[3]; ic = m[4]
            print(f"  {name:<14} {s:>8.3f} {m[0]:>+8.2f} {m[1]:>7.1f} {m[2]:>+8.2f} {ic:>8.4f}")
            if s > best_s: best_s, best_n = s, name
        print(f"  {'─'*70}")
        print(f"  Sharpe 最优: {best_n} ({best_s:.3f})")

    # Overall winner
    wins = {}
    for si, name in schemes: wins[name] = 0
    for fi in [8,9,10]:
        best_s = -99; best_n = ""
        for si, name in schemes:
            s = evalr(results, si, fi)[3]
            if s > best_s: best_s, best_n = s, name
        wins[best_n] += 1
    print(f"\n  {'='*90}")
    print(f"  总胜场: " + " | ".join(f"{n}:{w}" for n,w in wins.items()))
    winner = max(wins, key=wins.get)
    print(f"\n  🏆 冠军方案: {winner} ({wins[winner]}/3)")
    print(f"  完成: {datetime.now().strftime('%H:%M')}")

if __name__ == "__main__":
    main()

# ============================================================
# 新增方案 G/H/I
# ============================================================
def backtest_advanced(results_ref, all_dates_ref, test_dates_ref, daily_ref, kall_ref, schemes_extra):
    """在已有的 walk-forward 循环中增加新方案的预测"""
    # results_ref[-1] is last existing result tuple
    # We extend with new columns for G, H, I
    new_results = []
    for r in list(results_ref[-1]):  # last result tuple
        new_r = list(r)
        new_r.extend([0.0, 0.0, 0.0])  # Add placeholder for G, H, I
        new_results.append(tuple(new_r))
    results_ref[-1] = tuple(new_results)
    # ... this is getting complex. Let me just rewrite the backtest more cleanly.
