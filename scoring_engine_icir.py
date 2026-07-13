#!/usr/bin/env python3
"""
SS-ICIR 决策日报 v3.0
======================
ICIR 排名 + SS 评分双列 + 关键指标 + 板块筛选 + 信号追踪

报告设计:
  - ICIR 截面排名作为核心信号
  - SS 技术面/资金面/信息面分项作为辅助决策参考
  - 关键指标列: RSI/量比/MA20偏离/换手率
  - 筛选器: 板块 / 排名区间 / 涨跌方向
  - 信号面板: 🟢新买入 🔴触发卖出 ✅持仓 🟡关注
"""

import sys, os, json, math, time
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

# ====== ICIR 权重 (SS-ICIR-GLM: mfi/pct_52w 取反, 基于 GLM 方向矛盾诊断) ======
ICIR_W = {
    "turnover_z": 0.451, "log_mcap": 0.162, "mfi": -0.153, "pct_52w": -0.091,
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

HISTORY_FILE = os.path.join(PROJECT_DIR, "output", "icir_signal_history.json")
RANK_FILE = os.path.join(PROJECT_DIR, "output", "icir_rank_history.json")
SS_HISTORY_FILE = os.path.join(PROJECT_DIR, "output", "icir_ss_history.json")
BUY_COUNT = 30  # 买入阈值：前30名

def load_json(path):
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return {}

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f: json.dump(data, f, ensure_ascii=False)


def compute_ss_score(klines, idx):
    """SS 评分 + 因子明细"""
    if idx < 60: return {"tech": 50, "capital": 50, "info": 50, "total": 50, "factors": []}

    w = klines[:idx+1]
    c = [k['close'] for k in w]
    v = [k['volume'] for k in w]
    h = [k['high'] for k in w]
    l = [k['low'] for k in w]

    tech = 50; capital = 50
    factors = []  # [(dim, name, delta, detail)]

    def add(dim, name, delta, detail=""):
        factors.append((dim, name, delta, detail))

    ma5, ma10, ma20 = calc_ma(c, 5), calc_ma(c, 10), calc_ma(c, 20)
    rsi = calc_rsi(c)
    dif, dea = calc_ema(c, 12), calc_ema(c, 26)

    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            tech += 15
            add("技术面", "均线多头排列", 15, f"MA5({ma5:.1f})>MA10({ma10:.1f})>MA20({ma20:.1f})")
        elif ma5 < ma10 < ma20:
            tech -= 10
            add("技术面", "均线空头排列", -10, f"MA5({ma5:.1f})<MA10({ma10:.1f})<MA20({ma20:.1f})")
    if dif and dea and dif > dea and dif > 0:
        add("技术面", "MACD金叉多头", 5, f"DIF({dif:.2f})>DEA")
        tech += 5
    if 40 <= rsi <= 55:
        tech -= 3; add("技术面", "RSI弱势区", -3, f"RSI={rsi:.1f}")
    if rsi > 80:
        tech += 12; add("技术面", "RSI极端强势", 12, f"RSI={rsi:.1f}")
    elif rsi > 75:
        tech += 10; add("技术面", "RSI强动量", 10, f"RSI={rsi:.1f}")

    if len(c) >= 20:
        mf_mult = [(c[i] - l[i] - (h[i] - c[i])) / (h[i] - l[i]) if h[i] != l[i] else 0.0
                   for i in range(-20, 0)]
        mf_vol = [m * v[i] for m, i in zip(mf_mult, range(-20, 0))]
        cmf = sum(mf_vol) / sum(v[-20:]) if sum(v[-20:]) > 0 else 0
        if cmf > 0.1:
            capital += 8; add("资金面", "CMF强流入", 8, f"CMF={cmf:.2f}")
        elif cmf > 0:
            capital += 3; add("资金面", "CMF微流入", 3, f"CMF={cmf:.2f}")
        elif cmf < -0.1:
            capital -= 8; add("资金面", "CMF持续流出", -8, f"CMF={cmf:.2f}")

    tech = max(5, min(95, tech))
    capital = max(5, min(95, capital))
    total = tech * 0.35 + capital * 0.55 + 50 * 0.10

    return {"tech": tech, "capital": capital, "info": 50, "total": round(total), "factors": factors}


def extract_indicators(klines, idx, extra=None):
    """提取关键指标"""
    w = klines[:idx+1]
    c = [k['close'] for k in w]
    v = [k['volume'] for k in w]
    h = [k['high'] for k in w]
    l = [k['low'] for k in w]

    rsi = calc_rsi(c) if len(c) >= 14 else 50
    ma5, ma10, ma20 = calc_ma(c, 5), calc_ma(c, 10), calc_ma(c, 20)

    vol_ratio = v[-1] / (sum(v[-6:-1]) / 5) if len(v) >= 6 and sum(v[-6:-1]) > 0 else 1.0
    dev_ma20 = (c[-1] / ma20 - 1) * 100 if ma20 else 0
    turnover = extra.get("turnover", 0) if extra else 0
    ret_5d = (c[-1] / c[-6] - 1) * 100 if len(c) >= 6 else 0
    ret_10d = (c[-1] / c[-11] - 1) * 100 if len(c) >= 11 else 0
    ret_20d = (c[-1] / c[-21] - 1) * 100 if len(c) >= 21 else 0
    pe = extra.get("pe_ttm", 0) if extra else 0

    return {
        "rsi": round(rsi, 1), "vol_ratio": round(vol_ratio, 2),
        "dev_ma20": round(dev_ma20, 1), "turnover": round(turnover, 2),
        "ret_5d": round(ret_5d, 1), "ret_10d": round(ret_10d, 1), "ret_20d": round(ret_20d, 1),
        "pe": round(pe, 1),
    }


# ========== 邮件 ==========
def send_email(html_path, today, recipient="914110627@qq.com"):
    if not os.environ.get("SMTP_USER"): return False
    import smtplib; from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase; from email import encoders
    try:
        with open(html_path) as f: html = f.read()
    except: return False
    msg = MIMEMultipart()
    msg["Subject"] = f"📊 SS-ICIR 决策日报 {today}"
    msg["From"] = os.environ["SMTP_USER"]; msg["To"] = recipient
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(f"SS-ICIR 决策日报 {today}", "plain", "utf-8"))
    body.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(body)
    attach = MIMEBase("application", "octet-stream")
    attach.set_payload(html.encode("utf-8")); encoders.encode_base64(attach)
    attach.add_header("Content-Disposition", f'attachment; filename="SS-ICIR_{today}.html"')
    msg.attach(attach)
    try:
        s = smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT","587")), timeout=30)
        s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.sendmail(os.environ["SMTP_USER"], recipient, msg.as_string()); s.quit()
        return True
    except Exception as e: print(f"  ❌ 邮件: {e}"); return False


# ========== HTML 报告 ==========
def build_html(today, results, sorted_keys, sectors, new_buys, sell_alerts, holdings, watch_list,
               rank_risers, rank_fallers, sector_stats, rank_change, rank_history,
               rank_5d_change, ss_5d_change, kline_date="", extra_date="", data_fresh=True, missing_count=0):
    """构建完整 HTML 报告"""

    n = len(results)
    buy_count = BUY_COUNT
    top10_cnt = len(sorted_keys[:max(1,int(n*0.1))])
    top15_cnt = len(sorted_keys[:max(1,int(n*0.15))])

    # 数据状态徽章
    if data_fresh:
        data_status_badge = '<span style="color:#2e7d32">✅ 数据完整</span>'
    else:
        data_status_badge = f'<span style="color:#d32f2f">⚠️ 缺{missing_count}只</span>'

    # ====== 构建 code -> 信号状态映射 ======
    signal_map = {}  # {code: {"type": "buy"/"sell"/"hold"/"watch", "info": {...}}}
    for r in new_buys:
        signal_map[r["code"]] = {"type": "buy", "label": "🟢 新买入", "color": "#166534", "bg": "#dcfce7"}
    for r in holdings:
        signal_map[r["code"]] = {"type": "hold", "label": "✅ 持仓中", "color": "#0c447c", "bg": "#e6f1fb",
                                  "pnl": r.get("pnl", 0), "days": r.get("days_held", 0),
                                  "entry": r.get("entry_price", 0)}
    for r in sell_alerts:
        signal_map[r["code"]] = {"type": "sell", "label": "🔴 触发卖出", "color": "#991b1b", "bg": "#fee2e2",
                                  "trigger": r.get("trigger", ""), "pnl": r.get("pnl", 0),
                                  "days": r.get("days_held", 0), "entry": r.get("entry_price", 0)}
    for r in watch_list:
        signal_map[r["code"]] = {"type": "watch", "label": "🟡 关注", "color": "#854f0b", "bg": "#fef3c7",
                                  "pnl": r.get("pnl", 0), "days": r.get("days_held", 0),
                                  "entry": r.get("entry_price", 0)}

    # ====== 信号面板 ======
    signal_html = ""
    panels = [("🔴 触发卖出", "signal-red", sell_alerts, True),
              ("🟢 新买入信号", "signal-green", new_buys, False),
              ("✅ 持仓中", "signal-blue", holdings, False),
              ("🟡 关注列表", "signal-yellow", watch_list, False)]
    for idx, (title, cls, items, is_sell) in enumerate(panels):
        if not items: continue
        pid = f"sig_{idx}"
        signal_html += f'<div class="signal {cls}"><span class="sig-title">{title} · {len(items)}</span>'
        signal_html += '<div class="sig-items">'
        for i, r in enumerate(items):
            pnl = ""; pnl_cls = ""
            if is_sell or "entry_price" in r:
                ep = r.get("entry_price", r.get("price", 0))
                pnl = (r["price"] / ep - 1) * 100 if ep > 0 else 0
                pnl_cls = "red" if pnl > 0 else "green"
                pnl_str = f' <span class="{pnl_cls}">{pnl:+.1f}%</span>'
            else:
                pnl_str = ""
            detail = ""
            if is_sell: detail = f' <span class="dim">{r.get("trigger","")}</span>'
            elif "entry_date" in r: detail = f' <span class="dim">@{r["price"]:.2f}</span>'
            else: detail = f' <span class="dim">{r["sector"]}</span> <span class="{"red" if r["change_pct"]>0 else "green"}">{r["change_pct"]:+.1f}%</span>'
            if i >= 8:
                signal_html += f'<span class="sig-item sig-extra" style="display:none"><b>{r["code"]} {r["name"]}</b>{detail}{pnl_str}</span>'
            else:
                signal_html += f'<span class="sig-item"><b>{r["code"]} {r["name"]}</b>{detail}{pnl_str}</span>'
        if len(items) > 8:
            signal_html += f'<span class="sig-item dim" style="cursor:pointer;text-decoration:underline" onclick="toggleMore(\'{pid}\', this)">+{len(items)-8} 更多</span>'
        signal_html += '</div></div>'

    # ====== 仪表盘 ======
    dashboard = f"""
    <div class="dash">
    <div class="dcard"><div class="dv" style="color:#7c3aed">{top10_cnt}</div><div class="dl">前10%</div></div>
    <div class="dcard"><div class="dv" style="color:#1565c0">{top15_cnt}</div><div class="dl">前15%</div></div>
    <div class="dcard"><div class="dv" style="color:#d32f2f">{len(sell_alerts)}</div><div class="dl">卖出信号</div></div>
    <div class="dcard"><div class="dv" style="color:#2e7d32">{len(new_buys)}</div><div class="dl">新信号</div></div>
    <div class="dcard"><div class="dv">{len(holdings)}</div><div class="dl">持仓</div></div>
    <div class="dcard"><div class="dv" style="color:#f59e0b">{len(watch_list)}</div><div class="dl">关注</div></div>
    </div>"""

    # ====== 板块标签 ======
    all_sectors = sorted(sectors.keys())
    sector_tags = f'<span class="sector-tag active" onclick="filterSector(\'all\',this)">全部 ({n})</span>'
    for s in all_sectors:
        sector_tags += f'<span class="sector-tag" onclick="filterSector(\'{s}\',this)">{s} ({len(sectors[s])})</span>'

    # ====== 筛选器 ======
    filters = f"""
    <div class="filters">
    <div class="sector-tags">{sector_tags}</div>
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px">
    <select id="rankFilter" onchange="applyFilters()">
        <option value="all">全部排名</option><option value="top10">前30</option>
        <option value="top15">前60</option><option value="top30">前130</option>
        <option value="top50">前220</option></select>
    <select id="changeFilter" onchange="applyFilters()">
        <option value="all">全部涨跌</option><option value="up">上涨</option><option value="down">下跌</option></select>
    <input type="text" id="searchBox" placeholder="搜索代码/名称..." oninput="applyFilters()" style="padding:4px 8px;border:1px solid #ddd;border-radius:6px;font-size:12px;width:140px">
    <span class="filter-note" id="filterCount">{n}只</span>
    </div></div>"""

    # ====== 数据表行 ======
    table_rows = ""
    for code in sorted_keys:
        r = results[code]
        ind = r.get("indicators", {})
        ss = r.get("ss_score", {})
        arrow = rank_change.get(code, "●")
        ac = {"↑↑": "#d32f2f", "↑": "#e53935", "→": "#999", "↓": "#388e3c", "↓↓": "#2e7d32", "●": "#1565c0"}.get(arrow, "#999")
        chg_cls = "red" if r["change_pct"] > 0 else "green"
        ret5_cls = "red" if ind.get("ret_5d", 0) > 0 else "green"
        ret10_cls = "red" if ind.get("ret_10d", 0) > 0 else "green"
        ret20_cls = "red" if ind.get("ret_20d", 0) > 0 else "green"
        hl = ' class="hl"' if r["rank"] <= BUY_COUNT else ""

        # 5日排名变化
        r5d = rank_5d_change.get(code, 0)
        if r5d < 0:
            r5d_str = f' <span style="font-size:9px;color:#d32f2f">↑{-r5d}</span>'
        elif r5d > 0:
            r5d_str = f' <span style="font-size:9px;color:#2e7d32">↓{r5d}</span>'
        else:
            r5d_str = ''

        # 5日SS变化
        s5d = ss_5d_change.get(code, 0)
        if s5d > 0:
            s5d_str = f' <span style="font-size:9px;color:#d32f2f">↑{s5d}</span>'
        elif s5d < 0:
            s5d_str = f' <span style="font-size:9px;color:#2e7d32">↓{-s5d}</span>'
        else:
            s5d_str = ''

        # RSI color
        rsi_v = ind.get("rsi", 50)
        rsi_c = "#d32f2f" if rsi_v > 80 else ("#e65100" if rsi_v > 70 else ("#2e7d32" if rsi_v < 30 else "#555"))

        did = f"d{code}"
        table_rows += f'<tr{hl} data-sector="{r["sector"]}" data-rank="{r["rank"]}" data-change="{1 if r["change_pct"]>0 else 0}" data-arrow="{arrow}" data-code="{r["code"]}" data-name="{r["name"]}" style="cursor:pointer" onclick="toggleDetail(\'{did}\')">'
        table_rows += f'<td>{r["code"]}</td><td>{r["name"]}</td><td class="sector-cell">{r["sector"]}</td>'
        table_rows += f'<td class="rank-cell"><b># {r["rank"]}</b>{r5d_str} <span style="font-size:10px;color:{ac}">{arrow}</span></td>'
        table_rows += f'<td><b>{ss.get("total", "-")}</b>{s5d_str}</td>'
        table_rows += f'<td>{r["price"]:.2f}</td>'
        table_rows += f'<td class="{chg_cls}">{r["change_pct"]:+.2f}%</td>'
        table_rows += f'<td class="{ret5_cls}">{ind.get("ret_5d", 0):+.1f}%</td>'
        table_rows += f'<td class="{ret10_cls}">{ind.get("ret_10d", 0):+.1f}%</td>'
        table_rows += f'<td class="{ret20_cls}">{ind.get("ret_20d", 0):+.1f}%</td>'
        table_rows += f'<td><span style="color:{rsi_c};font-weight:600">{rsi_v:.0f}</span></td>'
        table_rows += f'<td>{ind.get("vol_ratio", "-")}</td>'
        table_rows += f'<td>{ind.get("dev_ma20", 0):+.1f}%</td>'
        table_rows += f'<td>{"{:.0f}".format(ind.get("pe", 0)) if ind.get("pe", 0) > 0 else "-"}</td>'
        table_rows += f'</tr>\n'

        # 详情行
        factors = ss.get("factors", [])
        pos_f = [f for f in factors if f[2] > 0]
        neg_f = [f for f in factors if f[2] < 0]
        rsi_v = ind.get("rsi", 50)
        rsi_c = "#d32f2f" if rsi_v > 80 else ("#e65100" if rsi_v > 70 else ("#2e7d32" if rsi_v < 30 else "#555"))

        detail = f'<tr id="{did}" class="detail-row" style="display:none"><td colspan="14" style="padding:12px 18px;background:#f8f9fc;border-bottom:2px solid #e0e0e0;font-size:12px">'

        # ---- 信号状态标记 ----
        sig = signal_map.get(code)
        if sig:
            sig_html = f'<div style="margin-bottom:10px;padding:6px 12px;background:{sig["bg"]};border-radius:6px;display:inline-block">'
            sig_html += f'<span style="font-weight:700;color:{sig["color"]};font-size:13px">{sig["label"]}</span>'
            if sig["type"] in ("hold", "watch", "sell"):
                pnl = sig.get("pnl", 0)
                pnl_cls = "#d32f2f" if pnl > 0 else "#2e7d32"
                sig_html += f' <span style="color:#888;font-size:11px">入场 ¥{sig.get("entry",0):.2f} · 持有{sig.get("days",0)}天 · 浮盈</span>'
                sig_html += f' <span style="color:{pnl_cls};font-weight:600">{pnl:+.1f}%</span>'
            if sig["type"] == "sell":
                sig_html += f' <span style="color:#991b1b;font-size:11px;font-weight:600">⚠ {sig.get("trigger","")}</span>'
            if sig["type"] == "buy":
                sig_html += f' <span style="color:#888;font-size:11px">今日新进入 top{BUY_COUNT}</span>'
            sig_html += '</div><br>'
            detail += sig_html

        # ---- 排名趋势 ----
        rh = rank_history.get(code, [r["rank"]])
        if len(rh) >= 2:
            trend_parts = []
            for i, rk in enumerate(rh[-5:]):
                if i == len(rh[-5:]) - 1:
                    trend_parts.append(f'<b style="color:#7c3aed;font-size:13px">#{rk}</b>')
                else:
                    trend_parts.append(f'<span style="color:#999">#{rk}</span>')
            trend_str = ' → '.join(trend_parts)
            detail += f'<div style="margin-bottom:10px;font-size:12px"><b>📈 近5日排名</b> {trend_str}</div>'

        # SS 5日趋势
        r5dc = rank_5d_change.get(code, 0)
        s5dc = ss_5d_change.get(code, 0)
        detail += f'<div style="margin-bottom:10px;font-size:12px"><b>📊 5日变化</b> 排名<span style="color:{"#d32f2f" if r5dc<0 else "#2e7d32" if r5dc>0 else "#999"};font-weight:600">{r5dc:+d}</span> &nbsp; SS分<span style="color:{"#d32f2f" if s5dc>0 else "#2e7d32" if s5dc<0 else "#999"};font-weight:600">{s5dc:+d}</span></div>'

        # ---- 第一行: SS评分分项 + 技术指标 ----
        detail += '<div style="display:flex;gap:32px;flex-wrap:wrap;margin-bottom:10px">'
        detail += f'<div><b style="color:#7c3aed">SS评分明细</b><br>技术面 <b style="font-size:15px">{ss.get("tech",50)}</b>'
        detail += f' &nbsp; 资金面 <b style="font-size:15px">{ss.get("capital",50)}</b>'
        detail += f' &nbsp; 信息面 <b style="font-size:15px">{ss.get("info",50)}</b>'
        detail += f' &nbsp; <span style="color:#888;font-size:10px">综合 {ss.get("total",50)} (35%/55%/10%)</span></div>'
        detail += f'<div><b style="color:#1565c0">技术指标</b><br>RSI <b style="color:{rsi_c}">{rsi_v:.0f}</b>'
        detail += f' &nbsp; 量比 <b>{ind.get("vol_ratio","-")}</b>'
        detail += f' &nbsp; MA20偏离 <b>{ind.get("dev_ma20",0):+.1f}%</b>'
        if ind.get("pe", 0) > 0:
            detail += f' &nbsp; PE <b>{ind.get("pe", 0):.0f}</b>'
        detail += '</div></div>'

        # ---- 第二行: SS加减分因子明细 ----
        if pos_f:
            detail += '<div style="margin:6px 0"><b style="color:#166534;font-size:11px">🟢 加分项</b></div>'
            detail += '<div style="display:flex;flex-wrap:wrap;gap:4px 6px">'
            for f in pos_f:
                detail += f'<span style="display:inline-block;padding:3px 10px;background:#dcfce7;border-radius:5px;font-size:11px">{f[1]} <b style="color:#166534">+{f[2]}</b> <span style="color:#888;font-size:10px">{f[3]}</span></span>'
            detail += '</div>'
        if neg_f:
            detail += '<div style="margin:6px 0"><b style="color:#991b1b;font-size:11px">🔴 减分项</b></div>'
            detail += '<div style="display:flex;flex-wrap:wrap;gap:4px 6px">'
            for f in neg_f:
                detail += f'<span style="display:inline-block;padding:3px 10px;background:#fee2e2;border-radius:5px;font-size:11px">{f[1]} <b style="color:#991b1b">{f[2]}</b> <span style="color:#888;font-size:10px">{f[3]}</span></span>'
            detail += '</div>'
        if not pos_f and not neg_f:
            detail += '<div style="color:#999;font-size:11px;margin-top:4px">无显著加减分因子触发（中性区间）</div>'

        detail += '</td></tr>'
        table_rows += detail + '\n'

    # ====== 板块热力 ======
    sector_html = '<div class="card"><h3>板块热力</h3><div class="sector-grid">'
    for ss in sector_stats:
        top3_str = " ".join(f'{c} {n}' for c, n, _ in ss["top3"])
        sector_html += f'<div class="sector-card" style="cursor:pointer" onclick="filterSector(\'{ss["name"]}\',this,true)">'
        sector_html += f'<div class="sector-name">{ss["name"]}</div>'
        sector_html += f'<div class="sector-num">{ss["count"]}只</div>'
        sector_html += f'<div>前30: <b>{ss["top10"]}</b> | 前60: <b>{ss["top15"]}</b></div>'
        sector_html += f'<div class="sector-top3">{top3_str}</div></div>'
    sector_html += '</div></div>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SS-ICIR {today}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;background:#eef0f5;color:#1a1a2e}}
.container{{max-width:1200px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:18px 24px}}
.header h1{{font-size:18px;margin-bottom:4px}} .header h1 span{{color:#e94560}}
.header p{{font-size:12px;color:#94a3b8;margin-bottom:6px}}
.header .meta{{display:flex;gap:14px;font-size:11px;color:#64748b;flex-wrap:wrap}}
.body-pad{{padding:12px 16px}}
.signal{{margin-bottom:6px;padding:10px 14px;border-radius:8px}}
.signal-red{{background:#fff5f5;border:1px solid #fecaca}}
.signal-green{{background:#f0fdf4;border:1px solid #bbf7d0}}
.signal-blue{{background:#eff6ff;border:1px solid #bfdbfe}}
.signal-yellow{{background:#fffbeb;border:1px solid #fde68a}}
.sig-title{{font-size:13px;font-weight:700;display:block;margin-bottom:6px}}
.sig-items{{display:flex;flex-wrap:wrap;gap:4px 12px}}
.sig-item{{font-size:11px;white-space:nowrap}}
.sig-item .dim{{color:#888;font-size:10px;margin-left:3px}}
.dash{{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap}}
.dcard{{flex:1;min-width:80px;background:#fff;border-radius:8px;padding:12px;text-align:center;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
.dcard .dv{{font-size:22px;font-weight:700}}
.dcard .dl{{font-size:10px;color:#888;margin-top:2px}}
.filters{{display:flex;gap:6px;align-items:center;padding:8px 0;flex-wrap:wrap;flex-direction:column;align-items:flex-start}}
.filters select,.filters input{{padding:5px 10px;border:1px solid #d0d0d0;border-radius:6px;font-size:12px;background:#fff}}
.filter-note{{font-size:11px;color:#999;margin-left:auto}}
.sector-tags{{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:2px}}
.sector-tag{{padding:4px 10px;border-radius:14px;font-size:11px;cursor:pointer;background:#f1f5f9;color:#64748b;border:1px solid transparent;transition:all .15s;user-select:none}}
.sector-tag:hover{{background:#e2e8f0;color:#334155}}
.sector-tag.active{{background:#1a1a2e;color:#fff;font-weight:600}}
.change-panel{{padding:8px 0;font-size:11px;min-height:24px}}
.card{{background:#fff;border-radius:10px;padding:14px;margin-bottom:8px;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.card h3{{font-size:13px;margin-bottom:8px;color:#333}}
.sector-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}}
.sector-card{{padding:8px 10px;background:#f8f9fa;border-radius:6px;font-size:11px}}
.sector-name{{font-weight:700;font-size:12px;margin-bottom:2px}}
.sector-num{{color:#7c3aed;font-size:16px;font-weight:700}}
.sector-top3{{color:#888;margin-top:4px}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th,td{{padding:5px 8px;text-align:center;border-bottom:1px solid #f0f0f0;white-space:nowrap}}
th{{background:#f5f6f8;font-weight:600;color:#666;font-size:10px;position:sticky;top:0;z-index:1;cursor:pointer}}
th:hover{{background:#e8eaed}}
td:first-child,th:first-child{{text-align:left;font-weight:600}}
.sector-cell{{font-size:10px;color:#888;max-width:60px;overflow:hidden;text-overflow:ellipsis}}
tr.hl{{background:#fefce8}}
tr:hover:not(.hl){{background:#f5f7ff}}
tr.hl:hover{{background:#fef3c7}}
.red{{color:#d32f2f}}.green{{color:#2e7d32}}.purple{{color:#7c3aed}}.dim{{color:#999}}
.footer{{text-align:center;padding:10px;font-size:10px;color:#aaa}}
.rank-cell{{min-width:70px}}
.detail-row{{transition:all .2s}}
</style>
<script>
var currentSector='all';
function toggleMore(pid, el){{
var parent=el.parentElement;
var extras=parent.querySelectorAll('.sig-extra');
var hidden=extras[0]&&extras[0].style.display==='none';
extras.forEach(function(e){{e.style.display=hidden?'':'none'}});
el.textContent=hidden?('+'+extras.length+' 收起'):'+'+extras.length+' 更多';
}}
function toggleDetail(id){{
var el=document.getElementById(id);
if(el)el.style.display=el.style.display==='none'?'':'none';
}}
function filterSector(sector,el,fromCard){{
currentSector=sector;
document.querySelectorAll('.sector-tag').forEach(t=>t.classList.remove('active'));
if(!fromCard&&el)el.classList.add('active');
else{{var tags=document.querySelectorAll('.sector-tag');tags.forEach(function(t){{if(t.textContent.startsWith(sector.split('(')[0])||(sector==='all'&&t.textContent.startsWith('全部')))t.classList.add('active')}})}}
applyFilters();
}}
function applyFilters(){{
var sector=currentSector;
var rank=document.getElementById('rankFilter').value;
var change=document.getElementById('changeFilter').value;
var search=document.getElementById('searchBox').value.toLowerCase();
var rows=document.querySelectorAll('tbody tr');
var visible=0;
var risers=[],fallers=[];
rows.forEach(function(r){{
var s=r.getAttribute('data-sector');
var rk=parseFloat(r.getAttribute('data-rank')||1);
var ch=r.getAttribute('data-change');
var arrow=r.getAttribute('data-arrow')||'●';
var txt=r.textContent.toLowerCase();
var show=true;
if(sector!=='all'&&s!==sector)show=false;
if(rank==='top10'&&rk>30)show=false;
if(rank==='top15'&&rk>60)show=false;
if(rank==='top30'&&rk>130)show=false;
if(rank==='top50'&&rk>220)show=false;
if(change==='up'&&ch!=='1')show=false;
if(change==='down'&&ch!=='0')show=false;
if(search&&!txt.includes(search))show=false;
r.style.display=show?'':'none';
if(show){{visible++;
var code=r.getAttribute('data-code')||'';
var name=r.getAttribute('data-name')||'';
if(arrow==='↑↑'||arrow==='↑')risers.push(code+' '+name);
if(arrow==='↓↓'||arrow==='↓')fallers.push(code+' '+name);
}}
}});
document.getElementById('filterCount').textContent=visible+'/'+rows.length+' 只';
// Update change panel
var cp=document.getElementById('changePanel');
var html='';
if(risers.length)html+='<b style=color:#d32f2f>↑ 上升:</b> '+risers.slice(0,15).join(' &nbsp;');
if(risers.length&&fallers.length)html+=' &nbsp;&nbsp; ';
if(fallers.length)html+='<b style=color:#2e7d32>↓ 下滑:</b> '+fallers.slice(0,15).join(' &nbsp;');
cp.innerHTML=html||'排名变动不显著';
}}
var sc=3,sa=false;
function sortTable(col){{
var tbody=document.querySelector('tbody');
var rows=Array.from(tbody.rows);
if(col===sc)sa=!sa;else{{sc=col;sa=false}}
rows.sort(function(a,b){{
var ca=(a.cells[col]||{{}}).textContent||'';
var cb=(b.cells[col]||{{}}).textContent||'';
var va=parseFloat(ca.replace(/[^0-9.-]/g,''));
var vb=parseFloat(cb.replace(/[^0-9.-]/g,''));
if(!isNaN(va)&&!isNaN(vb))return sa?va-vb:vb-va;
return sa?ca.localeCompare(cb):cb.localeCompare(ca)
}});
rows.forEach(function(r){{tbody.appendChild(r)}})
}}
</script>
</head>
<body>
<div class="container">
<div class="header">
<h1><span>SS-ICIR</span> 决策日报</h1>
<p><b>{today}</b> &nbsp;|&nbsp; {n}只A股 &nbsp;|&nbsp; ICIR截面排名 + SS辅助评分 &nbsp;|&nbsp; 分级止损</p>
<div class="meta">
<span>K线数据: <b>{kline_date}</b></span><span>行情数据: <b>{extra_date}</b></span>{data_status_badge}
<span>回测: 209笔/61.2%胜率/+5.9%均</span><span>买入: 前30</span>
<span>止损: 前30=-12%, 30-60=-8%, 60-130=-5%</span>
</div>
</div>
<div class="body-pad">
{signal_html}
{dashboard}
{filters}
<div class="card" style="padding:8px 0 0 0">
<div style="max-height:60vh;overflow:auto">
<table><thead><tr>
<th onclick="sortTable(0)">代码</th><th>名称</th><th>板块</th>
<th onclick="sortTable(3)">ICIR排名</th>
<th onclick="sortTable(4)">SS分</th>
<th>收盘价</th><th>日涨跌</th>
<th>5日涨跌</th><th>10日涨跌</th><th>20日涨跌</th>
<th>RSI</th><th>量比</th><th>MA20偏离</th><th>PE</th>
</tr></thead><tbody>{table_rows}</tbody></table>
</div></div>
<div class="change-panel" id="changePanel"></div>
{sector_html}
<div class="card" style="font-size:11px;color:#666;line-height:1.6">
<b>使用说明</b><br>
· <b>ICIR排名</b>: 33因子有效性加权后的截面排名，top 15% 触发买入信号<br>
· <b>SS分</b>: 传统技术面+资金面评分(基分50)，辅助判断动量强弱<br>
· <b>RSI</b>: <span style="color:#d32f2f">&gt;80过热</span> · <span style="color:#2e7d32">&lt;30超卖</span> · 40-55弱势<br>
· <b>量比</b>: &gt;1.5放量 · &lt;0.5缩量<br>
· <b>MA20偏离</b>: &gt;+10%远离均线 · &lt;-10%超跌<br>
· 筛选器支持板块/排名/涨跌/搜索组合筛选
</div>
<div class="footer">SS-ICIR-GLM | K线:{kline_date} 行情:{extra_date} | mfi/pct_52w 取反 + 全量因子计算 | 自动生成 {datetime.now().strftime('%H:%M')}</div>
</div></div></body></html>"""
    return html


# ========== 主流程 ==========
def run_daily(codes_file=None, output_dir=None, recipient=None):
    today = datetime.now().strftime("%Y-%m-%d")
    if codes_file is None: codes_file = os.path.join(PROJECT_DIR, "uploaded-stock-codes.txt")
    if output_dir is None: output_dir = os.path.join(PROJECT_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)

    print(f"╔══════════════════════════════════╗")
    print(f"║  SS-ICIR-GLM 决策日报 [{today}]  ║")
    print(f"╚══════════════════════════════════╝")

    with open(codes_file) as f: codes = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    # ====== 数据完整性预检 ======
    fresh = _db.check_data_freshness(codes)
    kline_date = fresh["kline_latest"]
    extra_date = fresh["extra_latest"]
    expected_td = fresh["expected_td"]
    n_missing_k = len(fresh["missing_klines"])
    n_missing_e = len(fresh["missing_extra"])

    print(f"  📅 数据日期: K线={kline_date} | 行情={extra_date} | 预期交易日={expected_td}")
    print(f"  📊 K线分布: {fresh['kline_counts']}")
    if fresh["fresh"]:
        print(f"  ✅ 数据完整性: {fresh['total_codes']}只全部最新")
    else:
        print(f"  ⚠️ 数据不完整: 缺K线{n_missing_k}只 | 缺行情{n_missing_e}只")
        if n_missing_k > 0 and n_missing_k <= 20:
            print(f"     缺K线: {fresh['missing_klines']}")
        if n_missing_e > 0 and n_missing_e <= 20:
            print(f"     缺行情: {fresh['missing_extra']}")
        if kline_date < expected_td:
            print(f"  🔄 K线数据过期({kline_date} < {expected_td})，正在增量更新...")

    klines = _db.get_klines(codes, days=130)
    extra = _db.get_extra_info(codes, force_refresh=True)
    for c in extra: extra[c]["_sector"] = get_theme(c)

    # ====== 数据完整性自动重试（最多5轮）======
    MAX_RETRY = 5
    prev_missing = set()
    suspended = set()

    for round_num in range(1, MAX_RETRY + 1):
        fresh2 = _db.check_data_freshness(codes)
        kline_date = fresh2["kline_latest"]
        extra_date = fresh2["extra_latest"]
        n_missing_k = len(fresh2["missing_klines"])
        n_missing_e = len(fresh2["missing_extra"])
        data_fresh = fresh2["fresh"]

        if data_fresh:
            print(f"  ✅ 数据完整: {fresh2['total_codes']}只全部最新 (第{round_num}轮检查)")
            break

        cur_missing = set(fresh2["missing_klines"]) | set(fresh2["missing_extra"])

        # 连续两轮缺失列表完全相同 → 判定停牌，不再重试
        if cur_missing == prev_missing and round_num > 1:
            suspended = cur_missing
            print(f"  ⏸️ 第{round_num}轮: 缺失列表无变化({len(cur_missing)}只)，判定停牌，停止重试")
            break

        prev_missing = cur_missing

        if round_num < MAX_RETRY:
            all_missing = list(set(fresh2["missing_klines"]) | set(fresh2["missing_extra"]))
            print(f"  🔄 第{round_num}/{MAX_RETRY}轮重试: 补拉{len(all_missing)}只 (缺K线{n_missing_k} 行情{n_missing_e})")
            _db.force_refresh(all_missing)
            time.sleep(2)
        else:
            suspended = cur_missing
            print(f"  ⚠️ 达到最大重试次数({MAX_RETRY}轮)，仍缺{len(cur_missing)}只")

    # 最终状态
    fresh_final = _db.check_data_freshness(codes)
    kline_date = fresh_final["kline_latest"]
    extra_date = fresh_final["extra_latest"]
    n_missing_k = len(fresh_final["missing_klines"])
    n_missing_e = len(fresh_final["missing_extra"])
    data_fresh = fresh_final["fresh"]

    if suspended:
        suspended = sorted(suspended)
        print(f"  ⏸️ 疑似停牌({len(suspended)}只): {suspended[:10]}{'...' if len(suspended)>10 else ''}")

    # ====== ICIR + SS + 指标 (v4.0: 全量一次性算因子, 截面标准化生效) ======
    # 构建当日全量数据池
    pool_klines = {}; pool_extra = {}
    for code in codes:
        kl = klines.get(code)
        if not kl or len(kl) < 60: continue
        pool_klines[code] = kl
        pool_extra[code] = extra.get(code, {})

    # 一次性计算全股票池因子 (截面标准化需要截面数据)
    all_factors = compute_all_factors(pool_klines, pool_extra, today_str=today)

    results = {}
    raw = {}
    for code in pool_klines:
        kl = pool_klines[code]
        ex = pool_extra.get(code, {})
        fv = all_factors.get(code, {})
        if not fv: continue

        icir_score = sum(ICIR.get(fn, 0.01) * fv.get(fn, 0) for fn in FACTOR_NAMES)
        ss = compute_ss_score(kl, len(kl)-1)
        ind = extract_indicators(kl, len(kl)-1, ex)

        results[code] = {
            "code": code, "name": ex.get("name", ""), "price": ex.get("price", 0),
            "change_pct": ex.get("change_pct", 0), "sector": ex.get("_sector", get_theme(code)),
            "icir_raw": icir_score, "ss_score": ss, "indicators": ind,
            "factor_values": {fn: round(fv.get(fn, 0), 3) for fn in [
                "turnover_z","log_mcap","mfi","pct_52w","pe_percentile","pb_percentile",
                "gap_open","ma_trend","rsi_signal","macd_signal","cmf","vwap_premium",
                "event_score","main_flow_5d","main_flow_20d","ret_5d","ret_20d",
                "volatility_20d",
            ]},
        }
        raw[code] = icir_score

    sorted_keys = sorted(raw.keys(), key=lambda k: -raw[k])
    n = len(sorted_keys)
    for rank, code in enumerate(sorted_keys):
        results[code]["rank_pct"] = rank / n
        results[code]["rank"] = rank + 1

    # ====== 排名历史 + 5日变化 ======
    prev_ranks = load_json(RANK_FILE)  # {code: {rank_history: [rank1, rank2, ...]}}
    prev_ss = load_json(SS_HISTORY_FILE)  # {code: {ss_history: [ss1, ss2, ...]}}
    rank_change = {}
    rank_history = {}  # {code: [最近5日排名]}
    rank_5d_change = {}  # {code: 5日排名变化 (负=上升=好)}
    ss_5d_change = {}  # {code: 5日SS变化 (正=上升=好)}
    for code, r in results.items():
        prev = prev_ranks.get(code, {})
        prev_s = prev_ss.get(code, {})
        ss_now = r["ss_score"].get("total", 50)

        # --- 排名历史 ---
        history = prev.get("history", [])
        history.append(r["rank"])
        if len(history) > 5:
            history = history[-5:]
        rank_history[code] = history
        if len(history) >= 2:
            rank_5d_change[code] = r["rank"] - history[0]  # 负=排名上升
        else:
            rank_5d_change[code] = 0

        # --- SS历史 ---
        ss_hist = prev_s.get("history", [])
        ss_hist.append(ss_now)
        if len(ss_hist) > 5:
            ss_hist = ss_hist[-5:]
        if len(ss_hist) >= 2:
            ss_5d_change[code] = ss_now - ss_hist[0]  # 正=SS分上升
        else:
            ss_5d_change[code] = 0

        # --- 日间排名箭头 ---
        prev_rp = prev.get("rank_pct")
        if prev_rp is not None:
            prev_rank = prev.get("rank", r["rank"])
            delta = r["rank"] - prev_rank  # 负=排名上升（好）
            if delta < -10: rank_change[code] = "↑↑"
            elif delta < -3: rank_change[code] = "↑"
            elif delta > 10: rank_change[code] = "↓↓"
            elif delta > 3: rank_change[code] = "↓"
            else: rank_change[code] = "→"
        else:
            rank_change[code] = "●"

        # 保存SS历史
        prev_ss[code] = {"history": ss_hist, "date": today}

    save_json(RANK_FILE, {c: {"rank_pct": results[c]["rank_pct"], "rank": results[c]["rank"], "history": rank_history[c], "date": today} for c in results})
    save_json(SS_HISTORY_FILE, prev_ss)

    # ====== 板块 ======
    sectors = defaultdict(list)
    for r in results.values(): sectors[r["sector"]].append(r)
    sector_stats = []
    for sec, stocks in sorted(sectors.items(), key=lambda x: -len([s for s in x[1] if s["rank"] <= BUY_COUNT])):
        top10 = len([s for s in stocks if s["rank"] <= 30])
        top15 = len([s for s in stocks if s["rank"] <= 60])
        top3 = sorted(stocks, key=lambda s: s["rank_pct"])[:3]
        sector_stats.append({"name": sec, "count": len(stocks), "top10": top10, "top15": top15,
                             "top3": [(s["code"], s["name"], s["rank_pct"]) for s in top3]})

    # ====== 信号追踪 ======
    signal_history = load_json(HISTORY_FILE)
    new_buys, sell_alerts, holdings, watch_list = [], [], [], []
    rank_risers, rank_fallers = [], []

    close_out = max(BUY_COUNT, int(n*0.3))  # 卖出阈值：前30%或大于买入线
    for code, r in results.items():
        if code not in signal_history: signal_history[code] = {"buy_dates": [], "alerts": []}
        hist = signal_history[code]
        has_pos = len(hist["buy_dates"]) > 0
        last_buy = hist["buy_dates"][-1] if has_pos else None

        if r["rank"] <= BUY_COUNT:
            if not has_pos:
                hist["buy_dates"].append({"date": today, "price": r["price"], "rank_pct": r["rank_pct"], "rank": r["rank"]})
                new_buys.append(r)
            else:
                ep = last_buy["price"]; pnl = (r["price"]/ep-1)*100 if ep > 0 else 0
                days = (datetime.strptime(today,"%Y-%m-%d")-datetime.strptime(last_buy["date"],"%Y-%m-%d")).days
                holdings.append({**r, "entry_date": last_buy["date"], "entry_price": ep, "pnl": round(pnl,1), "days_held": days})
        elif has_pos and last_buy:
            ep = last_buy["price"]; pnl = (r["price"]/ep-1)*100 if ep > 0 else 0
            days = (datetime.strptime(today,"%Y-%m-%d")-datetime.strptime(last_buy["date"],"%Y-%m-%d")).days
            entry_rank = last_buy.get("rank_pct", 0.15)
            stop = -12 if entry_rank < 0.10 else (-8 if entry_rank < 0.20 else -5)
            tracking = {**r, "entry_date": last_buy["date"], "entry_price": ep, "pnl": round(pnl,1), "days_held": days}
            if r["rank"] > n * 0.5 or pnl <= stop:
                trigger = "排名崩溃" if r["rank"] > n * 0.5 else f"止损({pnl:+.1f}%≤{stop}%)"
                hist["alerts"].append({"date":today,"reason":"sell","trigger":trigger})
                sell_alerts.append({**tracking, "trigger": trigger})
            else:
                watch_list.append(tracking)

        # 排名异动
        if r["rank"] > BUY_COUNT and rank_change.get(code) in ("↑↑","↑"): rank_risers.append(r)
        if r["rank"] <= BUY_COUNT and rank_change.get(code) in ("↓↓","↓"): rank_fallers.append(r)

    for code in signal_history:
        signal_history[code]["buy_dates"] = signal_history[code]["buy_dates"][-3:]
        signal_history[code]["alerts"] = signal_history[code]["alerts"][-20:]
    save_json(HISTORY_FILE, signal_history)

    # ====== HTML ======
    html = build_html(today, results, sorted_keys, sectors, new_buys, sell_alerts,
                      holdings, watch_list, rank_risers, rank_fallers, sector_stats, rank_change, rank_history,
                      rank_5d_change, ss_5d_change,
                      kline_date=kline_date, extra_date=extra_date,
                      data_fresh=data_fresh, missing_count=n_missing_k+n_missing_e)

    html_path = os.path.join(output_dir, f"SS-ICIR_{today}.html")
    with open(html_path, "w") as f: f.write(html)

    json_path = os.path.join(output_dir, f"SS-ICIR_{today}.json")
    with open(json_path, "w") as f:
        json.dump({"date": today, "model": "SS-ICIR-GLM", "total": n,
                   "kline_date": kline_date, "extra_date": extra_date, "data_fresh": data_fresh,
                   "new_buys": len(new_buys), "sell_alerts": len(sell_alerts),
                   "results": [results[c] for c in sorted_keys]}, f, ensure_ascii=False, indent=2)

    if os.environ.get("SMTP_USER") and recipient: send_email(html_path, today, recipient)

    print(f"  📊 {n}只 | K线:{kline_date} 行情:{extra_date} | 🟢{len(new_buys)} 🔴{len(sell_alerts)} ✅{len(holdings)} 🟡{len(watch_list)}")
    return results


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("codes", nargs="?", default="uploaded-stock-codes.txt")
    p.add_argument("recipient", nargs="?", default=None)
    p.add_argument("--output", default="output")
    a = p.parse_args()
    cf = a.codes if os.path.isabs(a.codes) else os.path.join(PROJECT_DIR, a.codes)
    od = a.output if os.path.isabs(a.output) else os.path.join(PROJECT_DIR, a.output)
    run_daily(codes_file=cf, output_dir=od, recipient=a.recipient)
