#!/usr/bin/env python3
"""
SS-Enhanced 实时信号回测 V2
============================
改进版信号设计：
  C1 缩量回调 — 高分+日跌+缩量 → 洗盘而非出货
  C2 放量突破 — 趋势改善+放量+强势收盘 → 真突破
  C3 板块领涨 — 板块内评分提升+今日涨幅领先 → 板块龙头
  C0 极端分位 — 高分(≥75)内部，涨vs跌分组对比 → 控制前置条件

回测期间：向前回溯~60个交易日
"""

import json, math, os, sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from scoring_engine import (
    fetch_kline_batch, score_ss_enhanced,
    THEME_SECTORS, STOCK_SECTOR
)


# ========== 工具函数 ==========

def get_theme(code):
    """获取股票所属主题板块"""
    sub = STOCK_SECTOR.get(code, "其他")
    for theme_name, keywords in THEME_SECTORS.items():
        for kw in keywords:
            if kw in sub:
                return theme_name
    return "其他"


def calc_slope(scores):
    """线性回归斜率"""
    if len(scores) < 3:
        return 0
    n = len(scores)
    x_mean = (n - 1) / 2
    y_mean = sum(scores) / n
    num = sum((i - x_mean) * (scores[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den > 0 else 0


def close_position(o, h, l, c):
    """收盘价在日内振幅中的位置（0-100%）, 100=收在最高"""
    rng = h - l
    if rng <= 0:
        return 50
    return (c - l) / rng * 100


# ========== 统计分析 ==========

def calc_stats(values):
    if not values or len(values) < 2:
        return {"n": len(values), "mean": None, "std": None,
                "win_rate": None, "t_stat": None, "p_value": None}
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean)**2 for v in values) / (n - 1)) if n > 1 else 0
    win_rate = sum(1 for v in values if v > 0) / n
    if std > 0:
        t_stat = mean / (std / math.sqrt(n))
        p_value = _t_to_p(t_stat, n - 1)
    else:
        t_stat = None
        p_value = None
    return {"n": n, "mean": round(mean, 2), "std": round(std, 2),
            "win_rate": round(win_rate * 100, 1),
            "t_stat": round(t_stat, 3) if t_stat else None,
            "p_value": round(p_value, 4) if p_value else None}


def _t_to_p(t, df):
    if df <= 0 or t is None:
        return 1.0
    abs_t = abs(t)
    if df > 100:
        return 2 * (1 - 0.5 * (1 + math.erf(abs_t / math.sqrt(2))))
    x = df / (df + abs_t * abs_t)
    return _incomplete_beta_reg(x, df / 2, 0.5) * 2


def _incomplete_beta_reg(x, a, b, steps=200):
    from math import lgamma, exp, log
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    h = x / steps
    log_integrand = lambda t: (a-1)*log(t)+(b-1)*log(1-t) if 0<t<1 else float('-inf')
    log_h = log(h)
    log_beta_ab = lgamma(a) + lgamma(b) - lgamma(a + b)
    s = 0.0
    max_log = float('-inf')
    vals = []
    for i in range(steps + 1):
        t = i * h
        w = 1 if i in (0, steps) else (2 if i % 2 == 0 else 4)
        if 0 < t < 1:
            li = log_integrand(t); vals.append((li, w))
            if li > max_log: max_log = li
        else:
            vals.append((float('-inf'), w))
    if max_log == float('-inf'): return 0.0
    s = sum(w * exp(li - max_log) for li, w in vals)
    result = exp(log_h + max_log + log(s) - log(3) - log_beta_ab)
    return max(0.0, min(1.0, result))


def two_sample_test(m1, s1, n1, m2, s2, n2):
    """Welch双样本t检验，返回(t, p)"""
    if not all([s1, s2, n1 > 1, n2 > 1]):
        return None, None
    se = math.sqrt(s1**2/n1 + s2**2/n2)
    if se == 0: return None, None
    t = (m1 - m2) / se
    num = (s1**2/n1 + s2**2/n2)**2
    den = s1**4/(n1**2*(n1-1)) + s2**4/(n2**2*(n2-1))
    df_w = num/den if den > 0 else 1
    return round(t, 3), round(_t_to_p(t, df_w), 4)


# ========== 信号计算（改进版） ==========

def compute_signals_v2(daily_data, code):
    """
    改进版信号计算。
    daily_data: [(date, score, close, open, high, low, volume, sector), ...]
    返回: signal列表 + 日级别特征数据
    """
    signals = []
    n = len(daily_data)

    for i in range(5, n - 10):
        t = daily_data[i]
        date, score, close = t[0], t[1], t[2]
        open_p, high_p, low_p, vol, sector = t[3], t[4], t[5], t[6], t[7]

        past_5_scores = [daily_data[j][1] for j in range(i-4, i+1)]
        past_5_avg = sum(past_5_scores) / 5
        slope = calc_slope(past_5_scores)
        yest_score = daily_data[i-1][1]
        yest_close = daily_data[i-1][2]
        ret_today = (close / yest_close - 1) * 100 if yest_close > 0 else 0

        # 5日均量比
        past_5_vols = [daily_data[j][6] for j in range(max(0,i-5), i)]
        avg_vol_5 = sum(past_5_vols) / len(past_5_vols) if past_5_vols else vol
        vol_ratio_5 = vol / avg_vol_5 if avg_vol_5 > 0 else 1

        # 收盘强势位
        cp = close_position(open_p, high_p, low_p, close)

        # 未来收益
        r5 = (daily_data[i+5][2]/close - 1)*100 if i+5 < n else None
        r10 = (daily_data[i+10][2]/close - 1)*100 if i+10 < n else None

        base = {"date": date, "code": code, "score": score,
                "avg5": round(past_5_avg, 1), "ret_today": round(ret_today, 2),
                "ret_5d_fwd": round(r5, 2) if r5 is not None else None,
                "ret_10d_fwd": round(r10, 2) if r10 is not None else None,
                "slope": round(slope, 2), "vol_ratio": round(vol_ratio_5, 2),
                "close_pos": round(cp, 1), "sector": sector}

        # ---- C1: 缩量回调 ----
        if past_5_avg >= 70 and ret_today < 0 and vol_ratio_5 < 0.8:
            signals.append({**base, "signal": "C1_缩量回调"})

        # ---- C2: 放量突破 ----
        if slope > 0 and vol_ratio_5 > 1.3 and cp > 80 and ret_today > 0:
            signals.append({**base, "signal": "C2_放量突破"})

    return signals


# ========== HTML报告 ==========

def generate_html_v2(all_signals, baseline_stats, signal_reports,
                     extreme_report, sector_report, codes, today_str):
    signal_colors = {
        "C1_缩量回调": "#8b5cf6", "C2_放量突破": "#06b6d4",
        "C3_板块领涨": "#f59e0b",
    }
    signal_labels = {
        "C1_缩量回调": "C1 缩量回调 (均分≥70+日跌+缩量<0.8)",
        "C2_放量突破": "C2 放量突破 (斜率>0+放量>1.3+强势收盘>80%)",
        "C3_板块领涨": "C3 板块领涨 (板块内评分↑+今日涨幅超板块中位数)",
    }

    # 方法论
    method_html = """
    <div style="background:#f0f4ff;border-radius:10px;padding:20px;margin:16px 0">
        <h3 style="margin:0 0 12px;color:#1a1a2e">📐 改进方法论</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px;color:#333;line-height:1.8">
            <div>
                <strong style="color:#8b5cf6">C1 缩量回调</strong>
                <p style="margin:4px 0">5日均分≥70 + 今日跌 + 5日均量比＜0.8</p>
                <p style="color:#888;font-size:12px">逻辑: 强势股缩量下跌→洗盘概率大,非真实出货</p>
            </div>
            <div>
                <strong style="color:#06b6d4">C2 放量突破</strong>
                <p style="margin:4px 0">5日评分斜率>0 + 量比>1.3 + 收盘位置>80% + 今日涨</p>
                <p style="color:#888;font-size:12px">逻辑: 趋势改善+量价配合+强势收盘→真突破</p>
            </div>
            <div>
                <strong style="color:#f59e0b">C3 板块领涨</strong>
                <p style="margin:4px 0">当日评分高于昨日 + 今日涨幅超板块中位数</p>
                <p style="color:#888;font-size:12px">逻辑: 板块内相对强势→可能成为龙头</p>
            </div>
            <div>
                <strong style="color:#ef4444">C0 极端分位</strong>
                <p style="margin:4px 0">高分(≥75)内部：今日涨 vs 今日跌 分组对比</p>
                <p style="color:#888;font-size:12px">逻辑: 控制"高分"前提,对比涨跌组的未来收益差异</p>
            </div>
        </div>
    </div>
    """

    # 基线
    base_html = """
    <div style="background:#f8f9fc;border-radius:10px;padding:20px;margin:16px 0">
        <h3 style="margin:0 0 12px;color:#1a1a2e">📊 全样本基线</h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#374151;color:#fff">
                <th style="padding:8px;text-align:left">指标</th><th style="padding:8px;text-align:right">样本量</th>
                <th style="padding:8px;text-align:right">均值(%)</th><th style="padding:8px;text-align:right">标准差</th>
                <th style="padding:8px;text-align:right">胜率(%)</th></tr>"""
    for h, k in [("5日","5d"),("10日","10d")]:
        b = baseline_stats.get(k, {})
        c = "#dc2626" if b.get('mean',0) and b['mean']>0 else "#16a34a"
        base_html += f"""<tr style="background:#fff;border-bottom:1px solid #e8ecf1">
            <td style="padding:8px;font-weight:600">未来{h}收益</td>
            <td style="padding:8px;text-align:right">{b.get('n',0)}</td>
            <td style="padding:8px;text-align:right;color:{c}">{b.get('mean','-')}</td>
            <td style="padding:8px;text-align:right">{b.get('std','-')}</td>
            <td style="padding:8px;text-align:right">{b.get('win_rate','-')}</td></tr>"""
    base_html += "</table></div>"

    # 信号表格
    sig_html = '<div style="margin:16px 0"><h3 style="color:#1a1a2e">🎯 改进信号 vs 基线</h3>'

    for sr in signal_reports:
        name = sr["signal"]
        label = signal_labels.get(name, name)
        color = signal_colors.get(name, "#666")
        sig_html += f"""
        <div style="background:#fff;border:2px solid {color}33;border-radius:10px;padding:20px;margin:12px 0">
            <h4 style="margin:0 0 12px;color:{color}">{label}</h4>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#374151;color:#fff">
                <th style="padding:8px;text-align:left">指标</th><th style="padding:8px;text-align:right">样本</th>
                <th style="padding:8px;text-align:right">均值%</th><th style="padding:8px;text-align:right">σ</th>
                <th style="padding:8px;text-align:right">胜率%</th><th style="padding:8px;text-align:right">超额%</th>
                <th style="padding:8px;text-align:right">t</th><th style="padding:8px;text-align:right">p</th></tr>"""

        for h in ["5日","10日"]:
            ss = sr.get(f"{h}_信号",{})
            bs = sr.get(f"{h}_基线",{})
            exc = sr.get(f"{h}_超额收益")
            tv = sr.get(f"{h}_t_双样本")
            pv = sr.get(f"{h}_p_双样本")
            mark = " ✅显著" if pv and pv<0.05 else (" ⚠️边缘" if pv and pv<0.10 else "")
            ec = "#dc2626" if exc and exc>0 else "#16a34a"
            ed = f"+{exc}" if exc and exc>0 else str(exc)
            mc = "#dc2626" if ss.get('mean',0) and ss['mean']>0 else "#16a34a"
            pc = "#dc2626" if pv and pv<0.05 else "#888"
            sig_html += f"""<tr style="border-bottom:1px solid #e8ecf1">
                <td style="padding:8px;font-weight:600">未来{h}收益{mark}</td>
                <td style="padding:8px;text-align:right">{ss.get('n',0)}</td>
                <td style="padding:8px;text-align:right;color:{mc};font-weight:600">{ss.get('mean','-')}</td>
                <td style="padding:8px;text-align:right">{ss.get('std','-')}</td>
                <td style="padding:8px;text-align:right;font-weight:600">{ss.get('win_rate','-')}</td>
                <td style="padding:8px;text-align:right;color:{ec};font-weight:700">{ed}</td>
                <td style="padding:8px;text-align:right">{tv or '-'}</td>
                <td style="padding:8px;text-align:right;color:{pc}">{pv or '-'}</td></tr>"""

        sig_html += "</table></div>"

    # C0 极端分位
    if extreme_report:
        er = extreme_report
        sig_html += f"""
        <div style="background:#fff;border:2px solid #ef444433;border-radius:10px;padding:20px;margin:12px 0">
            <h4 style="margin:0 0 12px;color:#ef4444">C0 极端分位 — 高分(≥75)内涨 vs 跌</h4>
            <p style="font-size:12px;color:#888;margin:0 0 8px">
                控制"高分"前置条件,对比当日涨跌组的后续表现差异
            </p>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#374151;color:#fff">
                <th style="padding:8px;text-align:left">分组</th><th style="padding:8px;text-align:right">样本</th>
                <th style="padding:8px;text-align:right">5日均值%</th><th style="padding:8px;text-align:right">5日胜率%</th>
                <th style="padding:8px;text-align:right">10日均值%</th><th style="padding:8px;text-align:right">10日胜率%</th></tr>
            <tr style="background:#f0fdf4;border-bottom:1px solid #e8ecf1">
                <td style="padding:8px;font-weight:600">🟢 高分+今日涨</td>
                <td style="padding:8px;text-align:right">{er['up']['n']}</td>
                <td style="padding:8px;text-align:right;color:#dc2626;font-weight:600">{er['up']['mean_5d']}</td>
                <td style="padding:8px;text-align:right">{er['up']['win_5d']}</td>
                <td style="padding:8px;text-align:right;color:#dc2626;font-weight:600">{er['up']['mean_10d']}</td>
                <td style="padding:8px;text-align:right">{er['up']['win_10d']}</td></tr>
            <tr style="background:#fef2f2;border-bottom:1px solid #e8ecf1">
                <td style="padding:8px;font-weight:600">🔴 高分+今日跌</td>
                <td style="padding:8px;text-align:right">{er['down']['n']}</td>
                <td style="padding:8px;text-align:right;color:{'#dc2626' if er['down']['mean_5d']>0 else '#16a34a'};font-weight:600">{er['down']['mean_5d']}</td>
                <td style="padding:8px;text-align:right">{er['down']['win_5d']}</td>
                <td style="padding:8px;text-align:right;color:{'#dc2626' if er['down']['mean_10d']>0 else '#16a34a'};font-weight:600">{er['down']['mean_10d']}</td>
                <td style="padding:8px;text-align:right">{er['down']['win_10d']}</td></tr>
            <tr style="background:#fffbeb">
                <td style="padding:8px;font-weight:700">涨-跌 差异</td>
                <td style="padding:8px;text-align:right">—</td>
                <td style="padding:8px;text-align:right;color:{'#dc2626' if er['diff_5d']>0 else '#16a34a'};font-weight:700">{er['diff_5d']:+.2f}%</td>
                <td style="padding:8px;text-align:right">—</td>
                <td style="padding:8px;text-align:right;color:{'#dc2626' if er['diff_10d']>0 else '#16a34a'};font-weight:700">{er['diff_10d']:+.2f}%</td>
                <td style="padding:8px;text-align:right">—</td></tr>
            </table>
        </div>"""

    # C3 板块领涨
    if sector_report:
        sr = sector_report
        sig_html += f"""
        <div style="background:#fff;border:2px solid #f59e0b33;border-radius:10px;padding:20px;margin:12px 0">
            <h4 style="margin:0 0 12px;color:#f59e0b">C3 板块领涨 (板块内评分改善+涨幅超板块中位数)</h4>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
            <tr style="background:#374151;color:#fff">
                <th style="padding:8px;text-align:left">指标</th><th style="padding:8px;text-align:right">样本</th>
                <th style="padding:8px;text-align:right">均值%</th><th style="padding:8px;text-align:right">σ</th>
                <th style="padding:8px;text-align:right">胜率%</th><th style="padding:8px;text-align:right">超额%</th>
                <th style="padding:8px;text-align:right">t</th><th style="padding:8px;text-align:right">p</th></tr>"""
        for h in ["5日","10日"]:
            ss = sr.get(f"{h}_信号",{})
            bs = sr.get(f"{h}_基线",{})
            exc = sr.get(f"{h}_超额收益")
            tv = sr.get(f"{h}_t_双样本")
            pv = sr.get(f"{h}_p_双样本")
            mark = " ✅显著" if pv and pv<0.05 else (" ⚠️边缘" if pv and pv<0.10 else "")
            ec = "#dc2626" if exc and exc>0 else "#16a34a"
            ed = f"+{exc}" if exc and exc>0 else str(exc)
            mc = "#dc2626" if ss.get('mean',0) and ss['mean']>0 else "#16a34a"
            sig_html += f"""<tr style="border-bottom:1px solid #e8ecf1">
                <td style="padding:8px;font-weight:600">未来{h}收益{mark}</td>
                <td style="padding:8px;text-align:right">{ss.get('n',0)}</td>
                <td style="padding:8px;text-align:right;color:{mc};font-weight:600">{ss.get('mean','-')}</td>
                <td style="padding:8px;text-align:right">{ss.get('std','-')}</td>
                <td style="padding:8px;text-align:right;font-weight:600">{ss.get('win_rate','-')}</td>
                <td style="padding:8px;text-align:right;color:{ec};font-weight:700">{ed}</td>
                <td style="padding:8px;text-align:right">{tv or '-'}</td>
                <td style="padding:8px;text-align:right;color:{'#dc2626' if pv and pv<0.05 else '#888'}">{pv or '-'}</td></tr>"""
        sig_html += "</table></div>"

    sig_html += "</div>"

    # 结论
    best = None
    best_exc = -999
    for sr in signal_reports:
        e = sr.get("5日_超额收益")
        if e and e > best_exc:
            best_exc = e
            best = sr
    concl = ""
    if best:
        label = signal_labels.get(best["signal"], best["signal"])
        p5 = best.get("5日_p_双样本")
        sig_word = "统计显著" if p5 and p5<0.05 else ("边缘显著" if p5 and p5<0.10 else "尚未达到统计显著")
        concl = f"""
        <div style="background:#fef3c7;border-radius:10px;padding:20px;margin:16px 0">
            <h3 style="margin:0 0 8px;color:#92400e">💡 结论</h3>
            <p style="margin:0;font-size:14px;color:#333;line-height:1.8">
                改进后最优信号: <strong style="color:#92400e">{label}</strong><br>
                5日超额收益 <strong style="color:#dc2626">+{best_exc}%</strong>，
                p={p5} ({sig_word})。
            </p>
            <p style="margin:8px 0 0;font-size:12px;color:#888">
                ⚠️ 回测基于历史数据，不构成投资建议。信号有效性可能随市场环境变化。
            </p></div>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SS-Enhanced 实时信号回测V2 {today_str}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}}
.container{{max-width:1000px;margin:0 auto;padding:20px}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);border-radius:12px;padding:30px;color:#fff;margin-bottom:0}}
.header h1{{margin:0;font-size:22px;color:#e94560}}
.header p{{margin:8px 0 0;font-size:13px;color:#94a3b8}}
</style></head><body><div class="container">
<div class="header">
<h1>🔬 SS-Enhanced 实时信号回测 V2</h1>
<p>缩量回调 · 放量突破 · 板块领涨 · 极端分位 — 四种改进信号</p>
<p>数据: {today_str} · {len(codes)}只A股 · 回测窗口: 60+交易日</p></div>
{method_html}{base_html}{sig_html}{concl}
<div style="text-align:center;padding:16px;color:#999;font-size:11px">
自动生成 | SS-Enhanced V2 | 历史数据回测 · 不构成投资建议</div>
</div></body></html>"""
    return html


# ========== 主流程 ==========

def compare_signals(baseline_stats, signal_vals_5d, signal_vals_10d, signal_name):
    """信号组 vs 基线对比"""
    report = {"signal": signal_name}
    for horizon, sig_vals, raw_key in [
        ("5日", signal_vals_5d, "5d"),
        ("10日", signal_vals_10d, "10d")
    ]:
        ss = calc_stats(sig_vals)
        bs = baseline_stats.get(raw_key, {})
        exc = round(ss["mean"] - bs["mean"], 2) if ss["mean"] and bs["mean"] else None
        t2, p2 = None, None
        if ss["mean"] and bs["mean"] and ss["std"] and bs["std"]:
            t2, p2 = two_sample_test(
                ss["mean"], ss["std"], ss["n"],
                bs["mean"], bs["std"], bs["n"])
        report[f"{horizon}_信号"] = ss
        report[f"{horizon}_基线"] = bs
        report[f"{horizon}_超额收益"] = exc
        report[f"{horizon}_t_双样本"] = t2
        report[f"{horizon}_p_双样本"] = p2
    return report


def run_backtest():
    today_str = datetime.now().strftime("%Y-%m-%d")
    OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    codes_file = os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip()]

    print(f"╔══════════════════════════════════════════╗")
    print(f"║ SS-Enhanced 实时信号回测 V2 [{today_str}] ║")
    print(f"╚══════════════════════════════════════════╝")
    print(f"股票池: {len(codes)} 只\n")

    # [1] K线数据
    print("[1/5] 获取K线数据...")
    klines_all = fetch_kline_batch(codes, days=130)
    print(f"  成功: {len(klines_all)} 只")

    # [2] 历史评分 + 扩展字段
    print("\n[2/5] 计算历史每日评分 + 扩展字段...")
    daily_scores = {}  # {code: [(date,score,close,open,high,low,volume,sector),...]}

    for idx, code in enumerate(codes):
        kl = klines_all.get(code)
        if not kl or len(kl) < 80:
            continue
        sector = get_theme(code)
        daily_scores[code] = []
        for i in range(60, len(kl)):
            s = score_ss_enhanced(kl, i)
            if s is None:
                continue
            k = kl[i]
            daily_scores[code].append((
                k['date'], s['score'], k['close'],
                k['open'], k['high'], k['low'],
                k['volume'], sector
            ))
        if (idx+1) % 30 == 0:
            print(f"  进度: {idx+1}/{len(codes)}")
    print(f"  完成: {len(daily_scores)} 只")

    # [3] 计算C1/C2信号 + 收集日级别面板数据
    print("\n[3/5] 计算改进信号...")
    all_signals = []
    all_ret_5d = []
    all_ret_10d = []

    # 日级别面板: {date: [(code,score,close,ret_today,ret_5d_fwd,ret_10d_fwd,sector,score_change),...]}
    day_panel = {}

    for code, data in daily_scores.items():
        if len(data) < 20:
            continue

        # C1/C2信号
        sigs = compute_signals_v2(data, code)
        all_signals.extend(sigs)

        # 全样本基准
        for i in range(5, len(data)-10):
            ct = data[i][2]
            if i+5 < len(data):
                all_ret_5d.append((data[i+5][2]/ct - 1)*100)
            if i+10 < len(data):
                all_ret_10d.append((data[i+10][2]/ct - 1)*100)

        # 日级别面板
        for i in range(5, len(data)-10):
            d = data[i]
            date = d[0]
            if date not in day_panel:
                day_panel[date] = []
            yest_c = data[i-1][2]
            rt = (d[2]/yest_c - 1)*100 if yest_c > 0 else 0
            r5 = (data[i+5][2]/d[2] - 1)*100
            r10 = (data[i+10][2]/d[2] - 1)*100
            sc_chg = d[1] - data[i-1][1]  # 今日评分 - 昨日评分
            day_panel[date].append({
                "code": code, "score": d[1], "close": d[2],
                "ret_today": rt, "ret_5d_fwd": r5, "ret_10d_fwd": r10,
                "sector": d[7], "score_change": sc_chg
            })

    print(f"  C1/C2总信号: {len(all_signals)}")

    # 信号分组
    signals_by_type = {}
    for s in all_signals:
        st = s["signal"]
        signals_by_type.setdefault(st, []).append(s)
    print(f"  分布: { {k:len(v) for k,v in signals_by_type.items()} }")

    # [4] C0 极端分位分析: 高分(≥75)内涨vs跌
    print("\n[4/5] C0 极端分位 + C3 板块领涨...")
    extreme_up_5d, extreme_up_10d = [], []
    extreme_down_5d, extreme_down_10d = [], []

    # C3 板块领涨信号
    sector_sig_5d, sector_sig_10d = [], []

    for date, items in day_panel.items():
        # C0: 高分(≥75)分组
        high_score_items = [it for it in items if it["score"] >= 75]
        for it in high_score_items:
            if it["ret_today"] > 0:
                extreme_up_5d.append(it["ret_5d_fwd"])
                extreme_up_10d.append(it["ret_10d_fwd"])
            else:
                extreme_down_5d.append(it["ret_5d_fwd"])
                extreme_down_10d.append(it["ret_10d_fwd"])

        # C3: 按板块聚合
        sectors = {}
        for it in items:
            sec = it["sector"]
            sectors.setdefault(sec, []).append(it)

        for sec, sec_items in sectors.items():
            if len(sec_items) < 3:
                continue
            # 板块中位数
            rets = sorted(it["ret_today"] for it in sec_items)
            median_ret = rets[len(rets)//2]
            # 评分变化中位数
            sc_chgs = sorted(it["score_change"] for it in sec_items)
            median_sc = sc_chgs[len(sc_chgs)//2]

            for it in sec_items:
                # C3: 评分改善 + 涨幅超板块中位数
                if it["score_change"] > 0 and it["ret_today"] > median_ret:
                    sector_sig_5d.append(it["ret_5d_fwd"])
                    sector_sig_10d.append(it["ret_10d_fwd"])

    # C0统计
    extreme_up_stats_5d = calc_stats(extreme_up_5d)
    extreme_up_stats_10d = calc_stats(extreme_up_10d)
    extreme_down_stats_5d = calc_stats(extreme_down_5d)
    extreme_down_stats_10d = calc_stats(extreme_down_10d)

    extreme_report = {
        "up": {"n": extreme_up_stats_5d["n"],
               "mean_5d": extreme_up_stats_5d["mean"],
               "win_5d": extreme_up_stats_5d["win_rate"],
               "mean_10d": extreme_up_stats_10d["mean"],
               "win_10d": extreme_up_stats_10d["win_rate"]},
        "down": {"n": extreme_down_stats_5d["n"],
                 "mean_5d": extreme_down_stats_5d["mean"],
                 "win_5d": extreme_down_stats_5d["win_rate"],
                 "mean_10d": extreme_down_stats_10d["mean"],
                 "win_10d": extreme_down_stats_10d["win_rate"]},
    }
    diff_5d = round((extreme_up_stats_5d["mean"] or 0) - (extreme_down_stats_5d["mean"] or 0), 2)
    diff_10d = round((extreme_up_stats_10d["mean"] or 0) - (extreme_down_stats_10d["mean"] or 0), 2)
    extreme_report["diff_5d"] = diff_5d
    extreme_report["diff_10d"] = diff_10d

    print(f"  C0 高分涨: n={extreme_up_stats_5d['n']}, 5d={extreme_up_stats_5d['mean']}%")
    print(f"  C0 高分跌: n={extreme_down_stats_5d['n']}, 5d={extreme_down_stats_5d['mean']}%")
    print(f"  C0 差异: 5d={diff_5d:+.2f}%, 10d={diff_10d:+.2f}%")
    print(f"  C3 板块领涨: n={len(sector_sig_5d)}")

    # [5] 综合统计
    print("\n[5/5] 统计检验...")
    baseline_stats = {"5d": calc_stats(all_ret_5d), "10d": calc_stats(all_ret_10d)}
    print(f"  基线: 5d={baseline_stats['5d']['mean']}%(n={baseline_stats['5d']['n']}), "
          f"10d={baseline_stats['10d']['mean']}%(n={baseline_stats['10d']['n']})")

    signal_reports = []

    # C1/C2
    for st in ["C1_缩量回调", "C2_放量突破"]:
        sigs = signals_by_type.get(st, [])
        if not sigs:
            continue
        v5 = [s["ret_5d_fwd"] for s in sigs if s["ret_5d_fwd"] is not None]
        v10 = [s["ret_10d_fwd"] for s in sigs if s["ret_10d_fwd"] is not None]
        r = compare_signals(baseline_stats, v5, v10, st)
        signal_reports.append(r)

        print(f"\n  {st}:")
        for h in ["5日", "10日"]:
            ss = r.get(f"{h}_信号", {})
            exc = r.get(f"{h}_超额收益")
            pv = r.get(f"{h}_p_双样本")
            mark = " **显著**" if pv and pv<0.05 else (" 边缘" if pv and pv<0.10 else "")
            print(f"    {h}: n={ss.get('n',0)}, mean={ss.get('mean','-')}%, "
                  f"excess={exc}, p={pv}{mark}")

    # C3 板块领涨
    if sector_sig_5d:
        r = compare_signals(baseline_stats, sector_sig_5d, sector_sig_10d, "C3_板块领涨")
        signal_reports.append(r)
        print(f"\n  C3_板块领涨:")
        for h in ["5日", "10日"]:
            ss = r.get(f"{h}_信号", {})
            exc = r.get(f"{h}_超额收益")
            pv = r.get(f"{h}_p_双样本")
            mark = " **显著**" if pv and pv<0.05 else (" 边缘" if pv and pv<0.10 else "")
            print(f"    {h}: n={ss.get('n',0)}, mean={ss.get('mean','-')}%, "
                  f"excess={exc}, p={pv}{mark}")

    # 生成报告
    sector_rpt = signal_reports[-1] if signal_reports and signal_reports[-1]["signal"] == "C3_板块领涨" else None
    other_rpts = [r for r in signal_reports if r["signal"] != "C3_板块领涨"]

    html = generate_html_v2(all_signals, baseline_stats, other_rpts,
                            extreme_report, sector_rpt, codes, today_str)

    html_path = os.path.join(OUTPUT_DIR, f"实时信号回测V2_{today_str}.html")
    with open(html_path, "w") as f:
        f.write(html)
    print(f"\n  HTML: {html_path}")

    # JSON
    json_path = os.path.join(OUTPUT_DIR, f"实时信号回测V2_{today_str}.json")
    with open(json_path, "w") as f:
        json.dump({
            "date": today_str,
            "baseline": baseline_stats,
            "signals_c1c2": signal_reports[:2],
            "sector_signal": sector_rpt,
            "extreme_report": extreme_report,
        }, f, ensure_ascii=False, indent=2)
    print(f"  JSON: {json_path}")

    return html_path


if __name__ == "__main__":
    run_backtest()
