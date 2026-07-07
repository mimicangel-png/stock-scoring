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
BUY_COUNT = 30

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


def settle_signals(today, klines):
    """结算历史信号的实际收益"""
    signals = load_json(TRACK_FILE)
    perf = load_json(PERF_FILE)

    for strategy in ("v3", "glm"):
        if strategy not in perf:
            perf[strategy] = {"buy": [], "sell": []}
        if strategy not in signals: continue

        for action in ("buy", "sell"):
            existing_keys = set()
            for p in perf[strategy][action]:
                existing_keys.add((p["entry_date"], p["code"]))

            for entry_date, day_data in signals[strategy].items():
                for sig in day_data[action]:
                    key = (entry_date, sig["code"])
                    if key in existing_keys: continue

                    entry_price = sig["price"]
                    if entry_price <= 0: continue

                    kl = klines.get(sig["code"], [])
                    # 找 entry_date 后 1/5/10/20 天的收盘价
                    entry_idx = None
                    for i, k in enumerate(kl):
                        if k["date"] == entry_date:
                            entry_idx = i
                            break
                    if entry_idx is None: continue

                    rets = {}
                    for nd in [1, 5, 10, 20]:
                        target_idx = entry_idx + nd
                        if target_idx < len(kl):
                            rets[f"ret_{nd}d"] = round((kl[target_idx]["close"] / entry_price - 1) * 100, 2)
                        else:
                            rets[f"ret_{nd}d"] = None

                    # 只有至少有 ret_1d 才记录
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


def build_report(today, v3_buys, v3_sells, glm_buys, glm_sells, perf):
    """生成 HTML 对比报告"""
    v3_buy_perf = perf.get("v3", {}).get("buy", [])
    glm_buy_perf = perf.get("glm", {}).get("buy", [])
    v3_sell_perf = perf.get("v3", {}).get("sell", [])
    glm_sell_perf = perf.get("glm", {}).get("sell", [])

    # 重叠分析
    v3_codes = set(b["code"] for b in v3_buys)
    glm_codes = set(b["code"] for b in glm_buys)
    overlap = v3_codes & glm_codes
    v3_only = v3_codes - glm_codes
    glm_only = glm_codes - v3_codes

    # 统计
    stats = {}
    for label, trades in [("v3_buy", v3_buy_perf), ("glm_buy", glm_buy_perf),
                           ("v3_sell", v3_sell_perf), ("glm_sell", glm_sell_perf)]:
        stats[label] = {}
        for nd in [1, 5, 10, 20]:
            stats[label][f"{nd}d"] = calc_stats(trades, f"ret_{nd}d")

    def fmt_stat(s):
        if s["n"] == 0: return "<span class='dim'>待数据</span>"
        wr_cls = "red" if s["win_rate"] >= 55 else ("green" if s["win_rate"] < 45 else "")
        avg_cls = "red" if s["avg"] > 0 else "green"
        return (f"<span class='{wr_cls}'>{s['win_rate']:.0f}%</span>"
                f" <span class='{avg_cls}'>{s['avg']:+.1f}%</span>"
                f" <span class='dim'>n={s['n']}</span>")

    # 统计已追踪天数
    track_days = max(len(v3_buy_perf), len(glm_buy_perf))

    # 买入信号对比表
    all_buy_codes = list(v3_codes | glm_codes)
    buy_rows = ""
    for code in sorted(all_buy_codes, key=lambda c: (
        0 if c in v3_buys and c in glm_buys else 1,
        v3_results_map.get(c, 999) if 'v3_results_map' in dir() else 999
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

    # 卖出信号
    sell_html = ""
    for label, sells, cls in [("v3 卖出", v3_sells, "tag-v3"), ("GLM 卖出", glm_sells, "tag-glm")]:
        if sells:
            sell_html += f'<div class="sell-block"><b>{label} ({len(sells)})</b> '
            sell_html += ' '.join(f'<span class="sig-item">{s["code"]} {s["name"]} @ {s["price"]:.2f}</span>' for s in sells[:10])
            if len(sells) > 10: sell_html += f' <span class="dim">+{len(sells)-10}</span>'
            sell_html += '</div>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>v3 vs GLM 对比追踪 {today}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,sans-serif;background:#eef0f5;color:#1a1a2e;font-size:13px}}
.container{{max-width:900px;margin:0 auto;padding:12px}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:16px 20px;border-radius:8px;margin-bottom:12px}}
.header h1{{font-size:17px}} .header h1 span{{color:#e94560}}
.header p{{font-size:11px;color:#94a3b8;margin-top:4px}}
.card{{background:#fff;border-radius:8px;padding:14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.card h3{{font-size:13px;margin-bottom:8px}}
.dash{{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}}
.dcard{{flex:1;min-width:120px;background:#fff;border-radius:8px;padding:10px;text-align:center;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
.dcard .dv{{font-size:20px;font-weight:700}} .dcard .dl{{font-size:10px;color:#888}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
th,td{{padding:5px 8px;text-align:center;border-bottom:1px solid #f0f0f0;white-space:nowrap}}
th{{background:#f5f6f8;font-weight:600;color:#666;font-size:10px}}
td:first-child,th:first-child{{text-align:left;font-weight:600}}
.red{{color:#d32f2f}} .green{{color:#2e7d32}} .dim{{color:#999}}
.tag-both{{background:#dcfce7;color:#166534;padding:2px 6px;border-radius:4px;font-size:10px}}
.tag-v3{{background:#e6f1fb;color:#0c447c;padding:2px 6px;border-radius:4px;font-size:10px}}
.tag-glm{{background:#faf5ff;color:#6b21a8;padding:2px 6px;border-radius:4px;font-size:10px}}
.sell-block{{margin-bottom:6px;padding:8px;background:#fff5f5;border-radius:6px;font-size:11px}}
.sig-item{{display:inline-block;margin-right:8px}}
.footer{{text-align:center;padding:10px;font-size:10px;color:#aaa}}
.stats-table th{{background:#1a1a2e;color:#fff}}
</style></head><body>
<div class="container">
<div class="header">
<h1><span>v3</span> vs <span>SS-ICIR-GLM</span> 对比追踪</h1>
<p>{today} | 纸面追踪第 {track_days + 1} 天 | 买入信号=top30 | 收益=实际收盘价</p>
</div>

<div class="dash">
<div class="dcard"><div class="dv" style="color:#d32f2f">{len(v3_buys)}</div><div class="dl">v3 买入</div></div>
<div class="dcard"><div class="dv" style="color:#6b21a8">{len(glm_buys)}</div><div class="dl">GLM 买入</div></div>
<div class="dcard"><div class="dv" style="color:#166534">{len(overlap)}</div><div class="dl">共识标的</div></div>
<div class="dcard"><div class="dv" style="color:#185fa5">{len(v3_only)}</div><div class="dl">仅 v3</div></div>
<div class="dcard"><div class="dv" style="color:#854f0b">{len(glm_only)}</div><div class="dl">仅 GLM</div></div>
</div>

<div class="card">
<h3>累计绩效对比（买入信号）</h3>
<table class="stats-table"><thead><tr>
<th>策略</th><th>1日胜率/均收</th><th>5日胜率/均收</th><th>10日胜率/均收</th><th>20日胜率/均收</th>
</tr></thead><tbody>
<tr><td><b>v3</b></td>
<td>{fmt_stat(stats['v3_buy']['1d'])}</td><td>{fmt_stat(stats['v3_buy']['5d'])}</td>
<td>{fmt_stat(stats['v3_buy']['10d'])}</td><td>{fmt_stat(stats['v3_buy']['20d'])}</td></tr>
<tr><td><b>GLM</b></td>
<td>{fmt_stat(stats['glm_buy']['1d'])}</td><td>{fmt_stat(stats['glm_buy']['5d'])}</td>
<td>{fmt_stat(stats['glm_buy']['10d'])}</td><td>{fmt_stat(stats['glm_buy']['20d'])}</td></tr>
</tbody></table>
</div>

<div class="card">
<h3>累计绩效对比（卖出信号）</h3>
<table class="stats-table"><thead><tr>
<th>策略</th><th>1日胜率/均收</th><th>5日胜率/均收</th><th>10日胜率/均收</th><th>20日胜率/均收</th>
</tr></thead><tbody>
<tr><td><b>v3</b></td>
<td>{fmt_stat(stats['v3_sell']['1d'])}</td><td>{fmt_stat(stats['v3_sell']['5d'])}</td>
<td>{fmt_stat(stats['v3_sell']['10d'])}</td><td>{fmt_stat(stats['v3_sell']['20d'])}</td></tr>
<tr><td><b>GLM</b></td>
<td>{fmt_stat(stats['glm_sell']['1d'])}</td><td>{fmt_stat(stats['glm_sell']['5d'])}</td>
<td>{fmt_stat(stats['glm_sell']['10d'])}</td><td>{fmt_stat(stats['glm_sell']['20d'])}</td></tr>
</tbody></table>
<p style="font-size:10px;color:#888;margin-top:6px">卖出信号收益 = 卖出后该股涨跌。正收益=卖错了（卖完还涨），负收益=卖对了（卖完跌了）</p>
</div>

{sell_html}

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
· <b>v3</b>: 原始 ICIR 权重 (mfi +0.153, pct_52w +0.091)<br>
· <b>SS-ICIR-GLM</b>: mfi/pct_52w 取反 (mfi -0.153, pct_52w -0.091)<br>
· <b>买入信号</b>: 当日排名 top30<br>
· <b>卖出信号</b>: 昨日在 top30 今日掉出<br>
· <b>收益计算</b>: entry_date 收盘价 → N 日后收盘价<br>
· <b>胜率</b>: 正收益占比。红色≥55%, 绿色<45%<br>
· 本报告为纸面追踪，不构成投资建议
</div>

<div class="footer">v3 vs GLM Tracker | 自动生成 {datetime.now().strftime('%H:%M')}</div>
</div></body></html>"""
    return html


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


def run(recipient="914110627@qq.com"):
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"╔════════════════════════════════════╗")
    print(f"║  v3 vs GLM 对比追踪 [{today}]  ║")
    print(f"╚════════════════════════════════════╝")

    codes_file = os.path.join(PROJECT_DIR, "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    klines = _db.get_klines(codes, days=130)
    extra = _db.get_extra_info(codes)
    for c in extra:
        extra[c]["_sector"] = get_theme(c)

    # 两套权重分别算排名
    v3_results, v3_sorted = compute_ranking(codes, klines, extra, ICIR_V3, today)
    glm_results, glm_sorted = compute_ranking(codes, klines, extra, ICIR_GLM, today)

    # 记录信号
    v3_buys, v3_sells, glm_buys, glm_sells = record_signals(
        today, v3_sorted, glm_sorted, v3_results, glm_results)

    # 结算历史信号
    perf = settle_signals(today, klines)

    # 生成报告
    html = build_report(today, v3_buys, v3_sells, glm_buys, glm_sells, perf)

    output_dir = os.path.join(PROJECT_DIR, "output")
    html_path = os.path.join(output_dir, f"v3_vs_glm_{today}.html")
    with open(html_path, "w") as f:
        f.write(html)

    # 统计
    v3_buy_perf = perf.get("v3", {}).get("buy", [])
    glm_buy_perf = perf.get("glm", {}).get("buy", [])
    v3_codes = set(v3_sorted[:BUY_COUNT])
    glm_codes = set(glm_sorted[:BUY_COUNT])
    overlap = v3_codes & glm_codes

    print(f"  v3: {len(v3_buys)}买/{len(v3_sells)}卖 | GLM: {len(glm_buys)}买/{len(glm_sells)}卖")
    print(f"  共识: {len(overlap)} | 仅v3: {len(v3_codes-glm_codes)} | 仅GLM: {len(glm_codes-v3_codes)}")
    print(f"  累计: v3买入{len(v3_buy_perf)}笔, GLM买入{len(glm_buy_perf)}笔")

    if os.environ.get("SMTP_USER"):
        send_email(html_path, today, recipient)
        print(f"  邮件已发送至 {recipient}")

    return {"v3_buys": len(v3_buys), "glm_buys": len(glm_buys),
            "overlap": len(overlap), "tracked_v3": len(v3_buy_perf),
            "tracked_glm": len(glm_buy_perf)}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("recipient", nargs="?", default="914110627@qq.com")
    a = p.parse_args()
    run(a.recipient)
