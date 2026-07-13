#!/usr/bin/env python3
"""
SS-ICIR v3 vs SS-ICIR-GLM 对比追踪器
=====================================
连续 30 个交易日追踪两套策略的 top30 买入信号，
计算未来 1/5/10/20 天实际收益，生成对比报告。

信号定义：
  v3: 原始 ICIR 权重 (mfi +0.153, pct_52w +0.091)
  glm: SS-ICIR-GLM 权重 (mfi -0.153, pct_52w -0.091)
  买入信号 = 当日 top30
  卖出信号 = 昨日在 top30 今日掉出

收益结算：
  entry_date 当日收盘价 → N 天后收盘价
  ret_Nd = (close_Nd / close_entry - 1) * 100
"""

import sys, os, json, math
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

# ====== v3 权重（原始，mfi/pct_52w 正） ======
ICIR_V3_W = {
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
_total_v3 = sum(abs(v) for v in ICIR_V3_W.values())
ICIR_V3 = {k: v/_total_v3 for k, v in ICIR_V3_W.items()}

# ====== GLM 权重（mfi/pct_52w 取反） ======
ICIR_GLM_W = dict(ICIR_V3_W)
ICIR_GLM_W["mfi"] = -0.153
ICIR_GLM_W["pct_52w"] = -0.091
_total_glm = sum(abs(v) for v in ICIR_GLM_W.values())
ICIR_GLM = {k: v/_total_glm for k, v in ICIR_GLM_W.items()}

# ====== 文件路径 ======
TRACK_FILE = os.path.join(PROJECT_DIR, "output", "v3_vs_glm_signals.json")
PERF_FILE = os.path.join(PROJECT_DIR, "output", "v3_vs_glm_performance.json")
TRADE_FILE = os.path.join(PROJECT_DIR, "output", "v3_vs_glm_trades.json")
BUY_COUNT = 30
HOLD_PERIODS = [5, 10, 20]  # 固定持仓周期（交易日）

def load_json(path):
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return {}

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f: json.dump(data, f, ensure_ascii=False)


def compute_ranking(codes, klines, extra, weights, today):
    """用指定权重计算全股票池排名"""
    pool_klines = {}; pool_extra = {}
    for code in codes:
        kl = klines.get(code)
        if not kl or len(kl) < 60: continue
        pool_klines[code] = kl
        pool_extra[code] = extra.get(code, {})

    all_factors = compute_all_factors(pool_klines, pool_extra, today_str=today)

    raw = {}
    results = {}
    for code in pool_klines:
        fv = all_factors.get(code, {})
        if not fv: continue
        score = sum(weights.get(fn, 0.01) * fv.get(fn, 0) for fn in FACTOR_NAMES)
        ex = pool_extra.get(code, {})
        raw[code] = score
        results[code] = {
            "code": code, "name": ex.get("name", ""),
            "price": ex.get("price", 0), "change_pct": ex.get("change_pct", 0),
            "sector": ex.get("_sector", get_theme(code)),
            "icir_raw": score,
        }

    sorted_keys = sorted(raw.keys(), key=lambda k: -raw[k])
    for rank, code in enumerate(sorted_keys):
        results[code]["rank"] = rank + 1
        results[code]["rank_pct"] = rank / max(1, len(sorted_keys))

    return results, sorted_keys


def record_signals(today, v3_sorted, glm_sorted, v3_results, glm_results):
    """记录今日 top30 买入信号 + 卖出信号"""
    signals = load_json(TRACK_FILE)

    v3_top30 = set(v3_sorted[:BUY_COUNT])
    glm_top30 = set(glm_sorted[:BUY_COUNT])

    # 卖出信号：昨天在 top30 今天不在
    prev_v3_top30 = set()
    prev_glm_top30 = set()
    if signals.get("v3"):
        prev_dates = sorted(signals["v3"].keys())
        if prev_dates:
            prev_v3_top30 = set(s["code"] for s in signals["v3"][prev_dates[-1]]["buy"])
    if signals.get("glm"):
        prev_dates = sorted(signals["glm"].keys())
        if prev_dates:
            prev_glm_top30 = set(s["code"] for s in signals["glm"][prev_dates[-1]]["buy"])

    v3_buys = [{"code": c, "name": v3_results[c]["name"], "price": v3_results[c]["price"],
                "rank": v3_results[c]["rank"]} for c in v3_sorted[:BUY_COUNT]]
    v3_sells = [{"code": c, "name": v3_results.get(c, {}).get("name", c),
                 "price": v3_results.get(c, {}).get("price", 0)}
                for c in prev_v3_top30 - v3_top30 if c in v3_results]

    glm_buys = [{"code": c, "name": glm_results[c]["name"], "price": glm_results[c]["price"],
                 "rank": glm_results[c]["rank"]} for c in glm_sorted[:BUY_COUNT]]
    glm_sells = [{"code": c, "name": glm_results.get(c, {}).get("name", c),
                  "price": glm_results.get(c, {}).get("price", 0)}
                 for c in prev_glm_top30 - glm_top30 if c in glm_results]

    for strategy in ("v3", "glm"):
        if strategy not in signals: signals[strategy] = {}

    signals["v3"][today] = {"buy": v3_buys, "sell": v3_sells}
    signals["glm"][today] = {"buy": glm_buys, "sell": glm_sells}

    save_json(TRACK_FILE, signals)
    return v3_buys, v3_sells, glm_buys, glm_sells


def build_trading_calendar(klines):
    """从 K 线数据构建交易日历（所有日期去重排序）"""
    all_dates = set()
    for kl in klines.values():
        for k in kl:
            all_dates.add(k["date"])
    return sorted(all_dates)


def next_trading_day(date_str, calendar):
    """找 date_str 之后的下一个交易日"""
    for d in calendar:
        if d > date_str:
            return d
    return None


def nth_trading_day(date_str, n, calendar):
    """找 date_str 之后第 n 个交易日（date_str 本身不算）"""
    idx = None
    for i, d in enumerate(calendar):
        if d > date_str:
            idx = i
            break
    if idx is None:
        return None
    target = idx + n - 1  # -1 因为 idx 已经是第 1 天
    if target < len(calendar):
        return calendar[target]
    return None


def get_ohlc(code, date_str, klines):
    """获取某只股票在某日的 OHLC 数据"""
    kl = klines.get(code, [])
    for k in kl:
        if k["date"] == date_str:
            return k
    return None


def generate_trade_ledger(signals, klines):
    """
    从信号生成实盘交易账本。
    
    规则：
    - 买入价：信号日 T 的下一个交易日开盘价（T+1 open）
    - 卖出价：买入后固定持仓 N 天的收盘价
    - 三策略：共识 / 仅v3 / 仅GLM
    
    Returns:
        {strategy: {holdN: [trade, ...]}}
    """
    calendar = build_trading_calendar(klines)
    trades = {cat: {f"hold{h}": [] for h in HOLD_PERIODS}
              for cat in ("共识", "仅v3", "仅GLM")}
    
    # 收集所有日期的信号并分类
    all_dates = sorted(set(list(signals.get("v3", {}).keys()) +
                           list(signals.get("glm", {}).keys())))
    
    for sig_date in all_dates:
        v3_day = signals.get("v3", {}).get(sig_date, {}).get("buy", [])
        glm_day = signals.get("glm", {}).get(sig_date, {}).get("buy", [])
        
        v3_codes = set(s["code"] for s in v3_day)
        glm_codes = set(s["code"] for s in glm_day)
        
        # 分类
        consensus = [(c, _lookup_name(c, v3_day, glm_day)) for c in v3_codes & glm_codes]
        v3_only = [(c, _lookup_name(c, v3_day, glm_day)) for c in v3_codes - glm_codes]
        glm_only = [(c, _lookup_name(c, v3_day, glm_day)) for c in glm_codes - v3_codes]
        
        categories = {"共识": consensus, "仅v3": v3_only, "仅GLM": glm_only}
        
        for cat_name, cat_stocks in categories.items():
            for code, name in cat_stocks:
                # 找 T+1 开盘价
                entry_date = next_trading_day(sig_date, calendar)
                if entry_date is None:
                    continue
                entry_bar = get_ohlc(code, entry_date, klines)
                if entry_bar is None or entry_bar.get("open", 0) <= 0:
                    continue  # T+1 无数据（停牌等）
                entry_price = entry_bar["open"]
                
                # 对每个持仓周期生成一笔交易
                for hold_days in HOLD_PERIODS:
                    exit_date = nth_trading_day(entry_date, hold_days, calendar)
                    if exit_date is None:
                        continue
                    exit_bar = get_ohlc(code, exit_date, klines)
                    if exit_bar is None or exit_bar.get("close", 0) <= 0:
                        continue
                    exit_price = exit_bar["close"]
                    
                    ret = round((exit_price / entry_price - 1) * 100, 2)
                    
                    trades[cat_name][f"hold{hold_days}"].append({
                        "signal_date": sig_date,
                        "code": code,
                        "name": name,
                        "entry_date": entry_date,
                        "entry_price": entry_price,
                        "entry_type": "open",
                        "exit_date": exit_date,
                        "exit_price": exit_price,
                        "exit_type": "close",
                        "holding_days": hold_days,
                        "return_pct": ret,
                    })
    
    return trades


def _lookup_name(code, v3_day, glm_day):
    """从信号列表中查股票名称"""
    for s in v3_day + glm_day:
        if s["code"] == code:
            return s.get("name", code)
    return code


def calc_trade_stats(trade_list):
    """计算一组交易的统计"""
    if not trade_list:
        return {"n": 0, "win_rate": 0, "avg_ret": 0, "total_ret": 0,
                "best": None, "worst": None}
    returns = [t["return_pct"] for t in trade_list]
    wins = sum(1 for r in returns if r > 0)
    total_ret = sum(returns)  # 简单加和，非复利
    return {
        "n": len(returns),
        "win_rate": round(wins / len(returns) * 100, 1),
        "avg_ret": round(sum(returns) / len(returns), 2),
        "total_ret": round(total_ret, 2),
        "best": max(returns),
        "worst": min(returns),
    }
    """结算历史信号的实际收益，支持增量更新远期 ret"""
    signals = load_json(TRACK_FILE)
    perf = load_json(PERF_FILE)

    for strategy in ("v3", "glm"):
        if strategy not in perf:
            perf[strategy] = {"buy": [], "sell": []}
        if strategy not in signals: continue

        for action in ("buy", "sell"):
            # 现有记录按 (entry_date, code) 索引
            existing_map = {}  # key -> index
            for idx, p in enumerate(perf[strategy][action]):
                existing_map[(p["entry_date"], p["code"])] = idx

            for entry_date, day_data in signals[strategy].items():
                for sig in day_data[action]:
                    key = (entry_date, sig["code"])
                    entry_price = sig["price"]
                    if entry_price <= 0: continue

                    kl = klines.get(sig["code"], [])
                    entry_idx = None
                    for i, k in enumerate(kl):
                        if k["date"] == entry_date:
                            entry_idx = i
                            break
                    if entry_idx is None: continue

                    # 计算所有可用远期的收益
                    rets = {}
                    for nd in [1, 5, 10, 20]:
                        target_idx = entry_idx + nd
                        if target_idx < len(kl):
                            rets[f"ret_{nd}d"] = round((kl[target_idx]["close"] / entry_price - 1) * 100, 2)
                        else:
                            rets[f"ret_{nd}d"] = None

                    if key in existing_map:
                        # 增量更新：填补之前为 None 的远期收益
                        idx = existing_map[key]
                        updated = False
                        for nd in [1, 5, 10, 20]:
                            field = f"ret_{nd}d"
                            if perf[strategy][action][idx].get(field) is None and rets.get(field) is not None:
                                perf[strategy][action][idx][field] = rets[field]
                                updated = True
                        # 不需要标记，直接改 in-place
                    else:
                        # 新信号：至少有 ret_1d 才记录
                        if rets["ret_1d"] is not None:
                            perf[strategy][action].append({
                                "entry_date": entry_date,
                                "code": sig["code"],
                                "name": sig["name"],
                                "entry_price": entry_price,
                                **rets,
                            })

    save_json(PERF_FILE, perf)
    return perf


def calc_stats(trades, ret_key="ret_5d"):
    """计算统计"""
    valid = [t[ret_key] for t in trades if t.get(ret_key) is not None]
    if not valid: return {"n": 0, "win_rate": 0, "avg": 0, "median": 0}
    wins = [r for r in valid if r > 0]
    return {
        "n": len(valid),
        "win_rate": round(len(wins) / len(valid) * 100, 1),
        "avg": round(sum(valid) / len(valid), 2),
        "median": round(sorted(valid)[len(valid)//2], 2),
    }


def categorize_trades(strategy, action, perf, signals):
    """
    分类每笔交易：共识 / 仅v3 / 仅GLM。
    根据 entry_date 当天该 code 在 v3 和 glm 的 top30 中出现情况判断。
    """
    result = {"共识": [], "仅v3": [], "仅GLM": []}
    for trade in perf.get(strategy, {}).get(action, []):
        date = trade.get("entry_date", "")
        code = trade.get("code", "")
        # 查找当天 v3/glm 的 top30
        v3_day = signals.get("v3", {}).get(date, {}).get("buy", [])
        glm_day = signals.get("glm", {}).get(date, {}).get("buy", [])
        v3_codes = set(s.get("code", "") for s in v3_day)
        glm_codes = set(s.get("code", "") for s in glm_day)
        in_v3 = code in v3_codes
        in_glm = code in glm_codes
        if in_v3 and in_glm:
            result["共识"].append(trade)
        elif in_v3:
            result["仅v3"].append(trade)
        elif in_glm:
            result["仅GLM"].append(trade)
    return result


def build_report(today, v3_buys, v3_sells, glm_buys, glm_sells, trades):
    v3_codes = set(b["code"] for b in v3_buys)
    glm_codes = set(b["code"] for b in glm_buys)
    overlap = v3_codes & glm_codes
    v3_only = v3_codes - glm_codes
    glm_only = glm_codes - v3_codes

    # 买入信号对比表
    all_buy_codes = list(v3_codes | glm_codes)
    v3_results_map = {b["code"]: b["rank"] for b in v3_buys}
    buy_rows = ""
    for code in sorted(all_buy_codes, key=lambda c: (
        0 if c in overlap else 1,
        v3_results_map.get(c, 999)
    )):
        v3_item = next((b for b in v3_buys if b["code"] == code), None)
        glm_item = next((b for b in glm_buys if b["code"] == code), None)
        name = (v3_item or glm_item or {}).get("name", code)
        in_v3 = "✅" if v3_item else "—"
        in_glm = "✅" if glm_item else "—"
        price = (v3_item or glm_item or {}).get("price", 0)
        v3_rank = v3_item["rank"] if v3_item else "-"
        glm_rank = glm_item["rank"] if glm_item else "-"
        tag = ""
        if v3_item and glm_item: tag = '<span class="tag-both">共识</span>'
        elif v3_item: tag = '<span class="tag-v3">仅v3</span>'
        else: tag = '<span class="tag-glm">仅GLM</span>'
        buy_rows += (f'<tr><td>{code}</td><td>{name}</td><td>{price:.2f}</td>'
                     f'<td>{in_v3} #{v3_rank}</td><td>{in_glm} #{glm_rank}</td>'
                     f'<td>{tag}</td></tr>\n')

    # --- 实盘交易账本（新） ---
    trade_html = _build_trade_section(trades)
    
    # --- 逐笔交易明细 ---
    detail_html = _build_trade_detail(trades)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>v3 vs GLM 对比追踪 {today}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,sans-serif;background:#eef0f5;color:#1a1a2e;font-size:13px}}
.container{{max-width:980px;margin:0 auto;padding:12px}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:16px 20px;border-radius:8px;margin-bottom:12px}}
.header h1{{font-size:17px}} .header h1 span{{color:#e94560}}
.header p{{font-size:11px;color:#94a3b8;margin-top:4px}}
.card{{background:#fff;border-radius:8px;padding:14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.card h3{{font-size:13px;margin-bottom:8px}}
.card h4{{font-size:12px;margin:10px 0 6px;color:#e94560}}
.dash{{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}}
.dcard{{flex:1;min-width:100px;background:#fff;border-radius:8px;padding:10px;text-align:center;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
.dcard .dv{{font-size:20px;font-weight:700}} .dcard .dl{{font-size:10px;color:#888}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th,td{{padding:4px 7px;text-align:center;border-bottom:1px solid #f0f0f0;white-space:nowrap}}
th{{background:#f5f6f8;font-weight:600;color:#666;font-size:10px}}
td:first-child,th:first-child{{text-align:left;font-weight:600}}
.red{{color:#d32f2f}} .green{{color:#2e7d32}} .dim{{color:#999}} .bold{{font-weight:700}}
.tag-both{{background:#dcfce7;color:#166534;padding:2px 6px;border-radius:4px;font-size:10px}}
.tag-v3{{background:#e6f1fb;color:#0c447c;padding:2px 6px;border-radius:4px;font-size:10px}}
.tag-glm{{background:#faf5ff;color:#6b21a8;padding:2px 6px;border-radius:4px;font-size:10px}}
.footer{{text-align:center;padding:10px;font-size:10px;color:#aaa}}
.stats-table th{{background:#1a1a2e;color:#fff}}
.winner{{background:#fffbeb}}

/* === 交互组件 === */
.tab-bar{{display:flex;gap:0;margin-bottom:8px;background:#f5f6f8;border-radius:8px;padding:3px}}
.tab-bar .tab{{flex:1;text-align:center;padding:8px 0;font-size:12px;font-weight:600;border-radius:6px;cursor:pointer;transition:all .15s;color:#666}}
.tab-bar .tab:hover{{background:#e8eaf0}}
.tab-bar .tab.active{{background:#fff;color:#1a1a2e;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.tab-search{{display:flex;align-items:center;padding:4px 8px}}
.tab-search input{{border:1px solid #ddd;border-radius:6px;padding:6px 10px;font-size:12px;width:140px;outline:none}}
.tab-search input:focus{{border-color:#888}}
.hold-tabs{{display:flex;gap:4px;margin-bottom:12px}}
.htab{{padding:5px 14px;font-size:11px;border:1px solid #ddd;border-radius:6px;cursor:pointer;color:#666;transition:all .15s}}
.htab:hover{{border-color:#999}}
.htab.active{{background:#1a1a2e;color:#fff;border-color:#1a1a2e}}

/* stat cards */
.stat-cards{{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}}
.scard{{flex:1;min-width:80px;background:#f8f9fb;border-radius:8px;padding:10px 8px;text-align:center}}
.scard .sv{{font-size:18px;font-weight:700;color:#1a1a2e}}
.scard .sl{{font-size:10px;color:#888;margin-top:2px}}
.c-green{{color:#2e7d32!important}} .c-red{{color:#d32f2f!important}}

/* week groups */
.trade-week{{margin-bottom:6px;border:1px solid #eee;border-radius:8px;overflow:hidden}}
.week-header{{display:flex;align-items:center;padding:10px 12px;background:#f8f9fb;cursor:pointer;user-select:none}}
.week-header:hover{{background:#f0f2f5}}
.week-label{{font-weight:600;font-size:12px;color:#333}}
.week-stats{{margin-left:12px;font-size:11px;color:#888}}
.week-toggle{{margin-left:auto;font-size:11px;color:#aaa;transition:transform .2s}}
.week-body{{overflow:auto}}
.week-body.hidden{{display:none}}
.week-body table{{font-size:10px}}
.week-body th{{background:#fafafa;position:sticky;top:0}}
.code-cell{{font-family:monospace;font-weight:600}}

/* PnL bar */
.pnl-cell{{font-weight:700;font-size:11px}}
.bar-cell{{padding:2px 4px!important;min-width:100px}}
.pnl-bar{{height:10px;border-radius:5px;min-width:4px}}
.bar-green{{background:linear-gradient(90deg,#a5d6a7,#43a047)}}
.bar-red{{background:linear-gradient(90deg,#ef9a9a,#e53935)}}
.empty-state{{text-align:center;padding:30px;color:#aaa;font-size:13px}}

/* email compatibility: no JS = show all */
@media screen and (max-width:0){{}} 
</style></head><body>
<div class="container">
<div class="header">
<h1><span>v3</span> vs <span>SS-ICIR-GLM</span> 实盘追踪</h1>
<p>{today} | 信号日收盘判断 → T+1开盘买入 → 固定持仓N天收盘卖出</p>
</div>

<div class="dash">
<div class="dcard"><div class="dv" style="color:#d32f2f">{len(v3_buys)}</div><div class="dl">v3 买入</div></div>
<div class="dcard"><div class="dv" style="color:#6b21a8">{len(glm_buys)}</div><div class="dl">GLM 买入</div></div>
<div class="dcard"><div class="dv" style="color:#166534">{len(overlap)}</div><div class="dl">共识标的</div></div>
<div class="dcard"><div class="dv" style="color:#185fa5">{len(v3_only)}</div><div class="dl">仅 v3</div></div>
<div class="dcard"><div class="dv" style="color:#854f0b">{len(glm_only)}</div><div class="dl">仅 GLM</div></div>
</div>

<div class="card">
<h3>📊 实盘交易账本 — 三策略对比</h3>
<p style="font-size:10px;color:#888;margin-bottom:4px">买入=T+1开盘价 | 卖出=持仓N天收盘价 | 同一标的可在不同时段重复交易</p>
{trade_html}
</div>

<div class="card">
<h3>📋 逐笔交易明细</h3>
<p style="font-size:10px;color:#888;margin-bottom:4px">按时间排序，展示每笔交易的完整买卖时间点和价格</p>
{detail_html}
</div>

<div class="card">
<h3>今日买入信号对比（{len(all_buy_codes)}只标的）</h3>
<div style="max-height:50vh;overflow:auto">
<table><thead><tr>
<th>代码</th><th>名称</th><th>收盘价</th><th>v3</th><th>GLM</th><th>标签</th>
</tr></thead><tbody>
{buy_rows}
</tbody></table>
</div></div>

<div class="card" style="font-size:11px;color:#666;line-height:1.6">
<b>说明</b><br>
· <b>信号日</b>: 当日收盘后根据 ICIR 排名判断 top30<br>
· <b>买入日</b>: 信号日的下一个交易日，以<b>开盘价</b>成交（实际可执行）<br>
· <b>卖出日</b>: 买入后固定持仓 N 个交易日，以<b>收盘价</b>卖出<br>
· <b>持仓周期</b>: 5日 / 10日 / 20日 三档<br>
· <b>三策略</b>: 共识(两套都推) / 仅v3 / 仅GLM<br>
· <b>收益</b>: (卖出价/买入价 - 1) × 100%
</div>

<div class="footer">v3 vs GLM Tracker | 自动生成 {datetime.now().strftime('%H:%M')}</div>
</div></body></html>"""
    return html


def _build_interactive_report(trades):
    """构建交互式报告：标签页 + 可折叠 + 搜索"""
    import json as _json
    trades_json = _json.dumps(trades, ensure_ascii=False)
    
    return f'''<div id="trade-app">
<div class="tab-bar" id="cat-tabs">
<span class="tab active" data-cat="共识">共识</span>
<span class="tab" data-cat="仅v3">仅v3</span>
<span class="tab" data-cat="仅GLM">仅GLM</span>
<span class="tab-search"><input id="stock-filter" type="text" placeholder="🔍 搜索代码/名称..." oninput="renderTrades()"></span>
</div>
<div class="hold-tabs" id="hold-tabs">
<span class="htab active" data-hold="hold5">持仓5日</span>
<span class="htab" data-hold="hold10">持仓10日</span>
<span class="htab" data-hold="hold20">持仓20日</span>
</div>
<div class="trade-summary" id="trade-summary"></div>
<div class="trade-groups" id="trade-groups"></div>
</div>

<script>
const TRADES = {trades_json};
let activeCat = '共识';
let activeHold = 'hold5';

document.getElementById('cat-tabs').addEventListener('click', function(e) {{
  if (e.target.classList.contains('tab')) {{
    document.querySelectorAll('#cat-tabs .tab').forEach(t => t.classList.remove('active'));
    e.target.classList.add('active');
    activeCat = e.target.dataset.cat;
    renderTrades();
  }}
}});

document.getElementById('hold-tabs').addEventListener('click', function(e) {{
  if (e.target.classList.contains('htab')) {{
    document.querySelectorAll('#hold-tabs .htab').forEach(t => t.classList.remove('active'));
    e.target.classList.add('active');
    activeHold = e.target.dataset.hold;
    renderTrades();
  }}
}});

function renderTrades() {{
  const tlist = TRADES[activeCat]?.[activeHold] || [];
  const filter = (document.getElementById('stock-filter').value || '').toLowerCase();
  
  // Filter
  let filtered = tlist;
  if (filter) {{
    filtered = tlist.filter(t => t.code.toLowerCase().includes(filter) || t.name.toLowerCase().includes(filter));
  }}
  
  // Stats
  const returns = filtered.map(t => t.return_pct);
  const n = returns.length;
  if (n === 0) {{
    document.getElementById('trade-summary').innerHTML = '<div class="empty-state">暂无已完成交易</div>';
    document.getElementById('trade-groups').innerHTML = '';
    return;
  }}
  const wins = returns.filter(r => r > 0).length;
  const avg = returns.reduce((a,b) => a+b, 0) / n;
  const best = Math.max(...returns);
  const worst = Math.min(...returns);
  const total = returns.reduce((a,b) => a+b, 0);
  
  const wrCls = wins/n >= 0.55 ? 'c-green' : (wins/n < 0.45 ? 'c-red' : '');
  const avgCls = avg > 0 ? 'c-green' : 'c-red';
  const totalCls = total > 0 ? 'c-green' : 'c-red';
  
  document.getElementById('trade-summary').innerHTML = 
    `<div class="stat-cards">
      <div class="scard"><div class="sv">${{n}}</div><div class="sl">已完成</div></div>
      <div class="scard"><div class="sv ${{wrCls}}">${{(wins/n*100).toFixed(0)}}%</div><div class="sl">胜率</div></div>
      <div class="scard"><div class="sv ${{avgCls}}">${{avg >= 0 ? '+' : ''}}${{avg.toFixed(1)}}%</div><div class="sl">平均收益</div></div>
      <div class="scard"><div class="sv ${{totalCls}}">${{total >= 0 ? '+' : ''}}${{total.toFixed(1)}}%</div><div class="sl">累计收益</div></div>
      <div class="scard"><div class="sv c-green">${{best >= 0 ? '+' : ''}}${{best.toFixed(1)}}%</div><div class="sl">最佳</div></div>
      <div class="scard"><div class="sv c-red">${{worst >= 0 ? '+' : ''}}${{worst.toFixed(1)}}%</div><div class="sl">最差</div></div>
    </div>`;
  
  // Group by signal week
  const groups = {{}};
  filtered.forEach(t => {{
    const d = t.signal_date;
    // Get ISO week
    const parts = d.split('-');
    const dt = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
    const mon = new Date(dt); mon.setDate(dt.getDate() - dt.getDay() + 1);
    const weekKey = mon.toISOString().slice(0,10);
    if (!groups[weekKey]) groups[weekKey] = [];
    groups[weekKey].push(t);
  }});
  
  let html = '';
  const sortedWeeks = Object.keys(groups).sort();
  sortedWeeks.forEach(wk => {{
    const weekTrades = groups[wk].sort((a,b) => a.signal_date.localeCompare(b.signal_date) || a.code.localeCompare(b.code));
    const wkReturns = weekTrades.map(t => t.return_pct);
    const wkAvg = wkReturns.reduce((a,b)=>a+b,0)/wkReturns.length;
    const wkWin = wkReturns.filter(r=>r>0).length;
    const wkId = 'wk-' + wk.replace(/-/g,'');
    
    html += `<div class="trade-week">
      <div class="week-header" onclick="document.getElementById('${{wkId}}').classList.toggle('hidden')">
        <span class="week-label">📅 ${{wk}} 起</span>
        <span class="week-stats">${{weekTrades.length}}笔 | 胜率${{(wkWin/weekTrades.length*100).toFixed(0)}}% | 均收${{wkAvg >= 0 ? '+' : ''}}${{wkAvg.toFixed(1)}}%</span>
        <span class="week-toggle">▼</span>
      </div>
      <div class="week-body" id="${{wkId}}">
        <table>
          <thead><tr>
            <th>信号日</th><th>代码</th><th>名称</th>
            <th>买入日</th><th>买入价</th>
            <th>卖出日</th><th>卖出价</th>
            <th>收益</th><th></th>
          </tr></thead><tbody>`;
    
    weekTrades.forEach(t => {{
      const retCls = t.return_pct > 0 ? 'c-green' : 'c-red';
      const barW = Math.min(Math.abs(t.return_pct) * 3, 100);
      const barCls = t.return_pct > 0 ? 'bar-green' : 'bar-red';
      html += `<tr>
        <td>${{t.signal_date.slice(5)}}</td>
        <td class="code-cell">${{t.code}}</td>
        <td>${{t.name}}</td>
        <td>${{t.entry_date.slice(5)}}</td>
        <td>${{t.entry_price.toFixed(2)}}</td>
        <td>${{t.exit_date.slice(5)}}</td>
        <td>${{t.exit_price.toFixed(2)}}</td>
        <td class="${{retCls}} pnl-cell">${{t.return_pct >= 0 ? '+' : ''}}${{t.return_pct.toFixed(1)}}%</td>
        <td class="bar-cell"><div class="pnl-bar ${{barCls}}" style="width:${{barW}}px"></div></td>
      </tr>`;
    }});
    
    html += '</tbody></table></div></div>';
  }});
  
  document.getElementById('trade-groups').innerHTML = html;
}}

renderTrades();
</script>'''


def _build_trade_section(trades):
    """构建实盘账本摘要卡片（简化版）"""
    rows = ""
    for hold_days in HOLD_PERIODS:
        hold_key = f"hold{hold_days}"
        rows += f'<h4>持仓 {hold_days} 日</h4>'
        rows += '<table class="cat-table"><thead><tr><th>策略</th><th>笔数</th><th>胜率</th><th>均收</th><th>累计</th></tr></thead><tbody>'
        
        best_avg = -999
        best_cat = None
        for cat in ("共识","仅v3","仅GLM"):
            s = calc_trade_stats(trades.get(cat, {}).get(hold_key, []))
            if s["n"] > 0 and s["avg_ret"] > best_avg:
                best_avg = s["avg_ret"]
                best_cat = cat
        
        for cat in ("共识","仅v3","仅GLM"):
            s = calc_trade_stats(trades.get(cat, {}).get(hold_key, []))
            if s["n"] == 0:
                rows += f'<tr><td style="font-weight:700">{cat}</td><td class="dim" colspan="4">暂无</td></tr>'
                continue
            wr_cls = "red" if s["win_rate"] >= 55 else ("green" if s["win_rate"] < 45 else "")
            avg_cls = "red" if s["avg_ret"] > 0 else "green"
            total_cls = "red" if s["total_ret"] > 0 else "green"
            highlight = 'style="background:#fffbeb"' if cat == best_cat else ""
            rows += (f'<tr {highlight}><td style="font-weight:700">{cat}</td>'
                    f'<td class="bold">{s["n"]}</td>'
                    f'<td><span class="{wr_cls}">{s["win_rate"]}%</span></td>'
                    f'<td><span class="{avg_cls} bold">{s["avg_ret"]:+.1f}%</span></td>'
                    f'<td><span class="{total_cls} bold">{s["total_ret"]:+.1f}%</span></td></tr>')
        rows += '</tbody></table>'
    return rows


def _build_trade_detail(trades):
    """返回交互式交易明细组件"""
    return _build_interactive_report(trades)


def send_email(html_path, today, recipient="914110627@qq.com"):
    if not os.environ.get("SMTP_USER"): return False
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders
    try:
        with open(html_path) as f: html = f.read()
    except: return False
    msg = MIMEMultipart()
    msg["Subject"] = f"📊 v3 vs GLM 对比追踪 {today}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = recipient
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(f"v3 vs GLM 对比追踪 {today}", "plain", "utf-8"))
    body.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(body)
    attach = MIMEBase("application", "octet-stream")
    attach.set_payload(html.encode("utf-8"))
    encoders.encode_base64(attach)
    attach.add_header("Content-Disposition", f'attachment; filename="v3_vs_glm_{today}.html"')
    msg.attach(attach)
    try:
        s = smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=30)
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.sendmail(os.environ["SMTP_USER"], recipient, msg.as_string())
        s.quit()
        return True
    except Exception as e:
        print(f"  邮件失败: {e}")
        return False


def run(recipient="914110627@qq.com", run_date=None):
    today = run_date if run_date else datetime.now().strftime("%Y-%m-%d")
    print(f"╔════════════════════════════════════╗")
    print(f"║  v3 vs GLM 对比追踪 [{today}]  ║")
    print(f"╚════════════════════════════════════╝")

    codes_file = os.path.join(PROJECT_DIR, "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    klines = _db.get_klines(codes, days=130)
    extra = _db.get_extra_info(codes, force_refresh=True)
    for c in extra:
        extra[c]["_sector"] = get_theme(c)

    # 回填模式：截断K线到目标日期，使因子计算使用当日视角
    klines_for_ranking = klines
    if run_date:
        klines_for_ranking = {}
        for code, kl in klines.items():
            truncated = [k for k in kl if k["date"] <= run_date]
            if truncated:
                klines_for_ranking[code] = truncated
        # 回填时用K线收盘价覆盖 extra_info 的实时价格
        for code in codes:
            kl = klines.get(code, [])
            td_close = None
            for k in kl:
                if k["date"] == run_date:
                    td_close = k["close"]
                    break
            if td_close and code in extra:
                extra[code]["price"] = td_close

    # 两套权重分别算排名
    v3_results, v3_sorted = compute_ranking(codes, klines_for_ranking, extra, ICIR_V3, today)
    glm_results, glm_sorted = compute_ranking(codes, klines_for_ranking, extra, ICIR_GLM, today)

    # 记录信号
    v3_buys, v3_sells, glm_buys, glm_sells = record_signals(
        today, v3_sorted, glm_sorted, v3_results, glm_results)

    # 生成实盘交易账本（T+1 open 买入，固定持仓周期收盘卖出）
    signals_all = load_json(TRACK_FILE)
    trades = generate_trade_ledger(signals_all, klines)
    save_json(TRADE_FILE, trades)

    # 生成报告
    html = build_report(today, v3_buys, v3_sells, glm_buys, glm_sells, trades)

    output_dir = os.path.join(PROJECT_DIR, "output")
    html_path = os.path.join(output_dir, f"v3_vs_glm_{today}.html")
    with open(html_path, "w") as f:
        f.write(html)

    # 统计（从 trades 账本取）
    total_v3 = sum(len(trades.get(c, {}).get(f"hold{h}", [])) for c in ("共识","仅v3") for h in HOLD_PERIODS)
    total_glm = sum(len(trades.get(c, {}).get(f"hold{h}", [])) for c in ("共识","仅GLM") for h in HOLD_PERIODS)
    v3_codes = set(v3_sorted[:BUY_COUNT])
    glm_codes = set(glm_sorted[:BUY_COUNT])
    overlap = v3_codes & glm_codes

    print(f"  v3: {len(v3_buys)}买/{len(v3_sells)}卖 | GLM: {len(glm_buys)}买/{len(glm_sells)}卖")
    print(f"  共识: {len(overlap)} | 仅v3: {len(v3_codes-glm_codes)} | 仅GLM: {len(glm_codes-v3_codes)}")
    print(f"  累计完成交易: v3侧{total_v3}笔, GLM侧{total_glm}笔")

    if os.environ.get("SMTP_USER"):
        send_email(html_path, today, recipient)
        print(f"  邮件已发送至 {recipient}")

    return {"v3_buys": len(v3_buys), "glm_buys": len(glm_buys),
            "overlap": len(overlap), "completed_v3": total_v3,
            "completed_glm": total_glm}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("recipient", nargs="?", default="914110627@qq.com")
    p.add_argument("--date", default=None, help="指定运行日期 YYYY-MM-DD (默认今天)")
    a = p.parse_args()
    run(a.recipient, run_date=a.date)
