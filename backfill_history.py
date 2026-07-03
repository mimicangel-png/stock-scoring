#!/usr/bin/env python3
"""
SS-ICIR 历史回补脚本
====================
基于已缓存的 K线数据，回补最近 N 个交易日的 ICIR 排名 和 SS 评分历史。

用法:
  python3 backfill_history.py 30    # 回补最近30个交易日
  python3 backfill_history.py       # 默认60个交易日
"""

import sys, os, json
from datetime import datetime, timedelta
from collections import defaultdict

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

def _load_env():
    env_path = os.path.join(PROJECT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
_load_env()

from stock_db import StockDB
_db = StockDB()
from v11.factor_engine import compute_all_factors, FACTOR_NAMES, calc_ma, calc_rsi, calc_ema
from scoring_engine import get_theme

# ICIR weights (same as scoring_engine_icir.py)
ICIR_W = {
    "turnover_z": 0.451, "log_mcap": 0.162, "mfi": 0.153, "pct_52w": 0.091,
    "pe_percentile": 0.078, "pb_percentile": 0.078, "gap_open": 0.072,
    "max_dd_20d": 0.052, "ma_trend": 0.030, "rsi_signal": 0.028,
    "macd_signal": 0.026, "cmf": 0.025, "vwap_premium": 0.022,
    "event_score": 0.020, "event_count": 0.018, "sector_rsi": 0.015,
    "sector_momentum": 0.012, "inflow_rate": 0.010, "main_flow_5d": 0.008,
    "main_flow_20d": 0.006, "amplitude_z": 0.005, "ret_20d": 0.004,
    "volatility_20d": 0.003, "ma_bull": 0.029, "vol_price": 0.025,
    "dev_ma20": 0.024, "vol_ratio_5d": 0.023, "ret_5d": 0.021,
    "streak": 0.020, "roe_rank": 0.001, "gross_margin_rank": 0.001,
    "ocf_ratio_rank": 0.001,
}
_total = sum(ICIR_W.values())
ICIR = {k: v/_total for k, v in ICIR_W.items()}

RANK_FILE = os.path.join(PROJECT_DIR, "output", "icir_rank_history.json")
SS_HISTORY_FILE = os.path.join(PROJECT_DIR, "output", "icir_ss_history.json")
EXTRA_FILE = os.path.join(PROJECT_DIR, "output", "icir_extra_snapshot.json")

def load_json(path):
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return {}

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f: json.dump(data, f, ensure_ascii=False)


def compute_ss_score(klines, idx):
    """简化版 SS 评分（技术面+资金面）"""
    if idx < 60: return {"tech": 50, "capital": 50, "info": 50, "total": 50}
    w = klines[:idx+1]
    c = [k['close'] for k in w]
    v = [k['volume'] for k in w]
    h = [k['high'] for k in w]
    l = [k['low'] for k in w]
    tech = 50; capital = 50
    ma5, ma10, ma20 = calc_ma(c, 5), calc_ma(c, 10), calc_ma(c, 20)
    rsi = calc_rsi(c)
    dif, dea = calc_ema(c, 12), calc_ema(c, 26)
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20: tech += 15
        elif ma5 < ma10 < ma20: tech -= 10
    if dif and dea and dif > dea and dif > 0: tech += 5
    if 40 <= rsi <= 55: tech -= 3
    if rsi > 80: tech += 12
    elif rsi > 75: tech += 10
    if len(c) >= 20:
        mf_mult = [(c[i] - l[i] - (h[i] - c[i])) / (h[i] - l[i]) if h[i] != l[i] else 0.0
                    for i in range(-20, 0)]
        mf_vol = [m * v[i] for m, i in zip(mf_mult, range(-20, 0))]
        cmf = sum(mf_vol) / sum(v[-20:]) if sum(v[-20:]) > 0 else 0
        if cmf > 0.1: capital += 8
        elif cmf > 0: capital += 3
        elif cmf < -0.1: capital -= 8
    tech = max(5, min(95, tech))
    capital = max(5, min(95, capital))
    total = tech * 0.35 + capital * 0.55 + 50 * 0.10
    return {"tech": tech, "capital": capital, "info": 50, "total": round(total)}


def get_proxy_extra(code, kline_date, today_extra):
    """
    用今天快照 + K线数据估算历史日期的 extra_info。
    name/sector/pe_ttm/pb/mcap 变化缓慢，用今天的近似。
    price/change_pct 从 K线精确推导。
    """
    ex = today_extra.get(code, {})
    name = ex.get("name", "")
    sector = ex.get("_sector", get_theme(code))
    pe = ex.get("pe_ttm", 0)
    pb = ex.get("pb", 0)
    mcap = ex.get("mcap", 0)
    turnover = ex.get("turnover", 0)

    # price 和 change_pct 会在后面的主循环中用K线覆盖
    return {
        "name": name, "_sector": sector,
        "pe_ttm": pe, "pb": pb, "mcap": mcap,
        "turnover": turnover,
        "price": 0, "change_pct": 0,
    }


def backfill(days=60):
    """回补最近 days 个交易日的排名和SS历史"""
    codes_file = os.path.join(PROJECT_DIR, "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    # 加载所有K线
    klines = _db.get_klines(codes, days=400)  # 取足够天数
    today_extra = _db.get_extra_info(codes)
    for c in today_extra:
        today_extra[c]["_sector"] = get_theme(c)

    # 收集所有K线日期
    all_dates = set()
    for code, kl in klines.items():
        for k in kl:
            all_dates.add(k["date"])
    all_dates = sorted(all_dates)
    print(f"K线日期范围: {all_dates[0]} ~ {all_dates[-1]}, 共 {len(all_dates)} 个交易日")

    # 取最近 N 个日期
    target_dates = all_dates[-days:]
    print(f"回补最近 {len(target_dates)} 个交易日: {target_dates[0]} ~ {target_dates[-1]}")

    # 创建日期索引
    date_index = {d: i for i, d in enumerate(all_dates)}

    # 加载现有历史（增量模式）
    old_rank = load_json(RANK_FILE)
    old_ss = load_json(SS_HISTORY_FILE)

    rank_history = {}  # {code: [rank1, rank2, ...]}
    ss_history = {}    # {code: [ss1, ss2, ...]}

    # 初始化：从已有历史开始
    for code in codes:
        prev = old_rank.get(code, {})
        rank_history[code] = prev.get("history", [])
        prev_s = old_ss.get(code, {})
        ss_history[code] = prev_s.get("history", [])

    # 逐日计算（全股票池一起算，保证截面标准化有效）
    total_targets = len(target_dates)
    for di, target_date in enumerate(target_dates):
        target_idx = date_index.get(target_date)
        if target_idx is None:
            continue

        # 构建当日全量数据
        pool_klines = {}   # {code: klines sliced to target_date}
        pool_extra = {}    # {code: extra dict with price/change_pct from K-line}

        for code in codes:
            kl = klines.get(code)
            if not kl or len(kl) < 60:
                continue

            kl_idx = None
            for i, k in enumerate(kl):
                if k["date"] == target_date:
                    kl_idx = i
                    break
            if kl_idx is None or kl_idx < 60:
                continue

            proxy_ex = get_proxy_extra(code, target_date, today_extra)
            k = kl[kl_idx]
            proxy_ex["price"] = k["close"]
            proxy_ex["change_pct"] = ((k["close"] / kl[kl_idx-1]["close"] - 1) * 100) if kl_idx > 0 else 0

            pool_klines[code] = kl[:kl_idx+1]
            pool_extra[code] = proxy_ex

        if len(pool_klines) < 100:
            print(f"  [{di+1}/{total_targets}] {target_date} 数据不足({len(pool_klines)}只), 跳过")
            continue

        # 全量计算因子
        all_factors = compute_all_factors(pool_klines, pool_extra, today_str=target_date)

        # 计算 ICIR 和 SS
        raw = {}; results = {}
        for code in pool_klines:
            fv = all_factors.get(code, {})
            if not fv: continue
            icir_score = sum(ICIR.get(fn, 0.01) * fv.get(fn, 0) for fn in FACTOR_NAMES)
            kl_slice = pool_klines[code]
            ss = compute_ss_score(kl_slice, len(kl_slice)-1)
            ex = pool_extra[code]
            results[code] = {
                "code": code, "name": ex.get("name",""),
                "price": ex["price"], "change_pct": ex["change_pct"],
                "sector": ex.get("_sector",""), "icir_raw": icir_score, "ss_score": ss,
            }
            raw[code] = icir_score

        if not results: continue

        # 排名
        sorted_keys = sorted(raw.keys(), key=lambda k: -raw[k])
        for rank, code in enumerate(sorted_keys):
            results[code]["rank"] = rank + 1
            results[code]["rank_pct"] = rank / len(sorted_keys)

        # 记录历史
        for code in codes:
            r = results.get(code)
            if r:
                rank_history[code].append(r["rank"])
                ss_history[code].append(r["ss_score"].get("total", 50))

        print(f"  [{di+1}/{total_targets}] {target_date} 完成, {len(results)}只")

    # 裁剪到最近5天
    for code in codes:
        if len(rank_history[code]) > 5:
            rank_history[code] = rank_history[code][-5:]
        if len(ss_history[code]) > 5:
            ss_history[code] = ss_history[code][-5:]

    # 保存
    today_str = target_dates[-1]
    save_json(RANK_FILE, {c: {
        "rank_pct": old_rank.get(c, {}).get("rank_pct", 0),
        "rank": old_rank.get(c, {}).get("rank", 0),
        "history": rank_history[c],
        "date": today_str
    } for c in codes if old_rank.get(c) or rank_history[c]})

    save_json(SS_HISTORY_FILE, {c: {
        "history": ss_history[c],
        "date": today_str
    } for c in codes if ss_history[c]})

    # 统计
    stocks_with_history = sum(1 for c in codes if len(ss_history[c]) >= 2)
    print(f"\n✅ 回补完成: {stocks_with_history}/{len(codes)} 只股票有≥2天历史")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("days", nargs="?", type=int, default=60)
    args = p.parse_args()
    backfill(args.days)
