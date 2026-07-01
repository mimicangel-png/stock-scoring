#!/usr/bin/env python3
"""
SS-Enhanced 实时信号回测 V3 — 网格搜索 + 收益曲线 + 成交量交互
=================================================================
核心探索：
  1. 网格搜索 — 评分阈值 × 跌幅阈值 热力图，找最优参数
  2. 前向收益曲线 — 触发后 1-20 天累计收益，看信号衰减
  3. 成交量分层 — 缩量/正常/放量下的回调效果差异
  4. 连续回调 — 单日跌 vs 连跌2日 vs 连跌3日
"""

import json, math, os, sys
from datetime import datetime
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from scoring_engine import (
    fetch_kline_batch, score_ss_enhanced,
    THEME_SECTORS, STOCK_SECTOR
)

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ========== 工具函数 ==========

def get_theme(code):
    sub = STOCK_SECTOR.get(code, "其他")
    for theme_name, keywords in THEME_SECTORS.items():
        for kw in keywords:
            if kw in sub:
                return theme_name
    return "其他"


def calc_stats(values):
    if not values or len(values) < 2:
        return {"n": 0, "mean": None, "std": None, "win_rate": None,
                "median": None, "q25": None, "q75": None}
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean)**2 for v in values) / (n - 1)) if n > 1 else 0
    win_rate = sum(1 for v in values if v > 0) / n
    sv = sorted(values)
    median = sv[n // 2]
    q25 = sv[n // 4]
    q75 = sv[3 * n // 4]
    return {"n": n, "mean": round(mean, 2), "std": round(std, 2),
            "win_rate": round(win_rate * 100, 1),
            "median": round(median, 2), "q25": round(q25, 2), "q75": round(q75, 2)}


# ========== 数据准备：构建日级别面板，预计算前向收益 ==========

def build_panel(daily_scores, max_fwd=20):
    """
    从 daily_scores 构建日级别面板。
    返回:
      rows: [{date, code, score, close, ret_today, vol_ratio, sector,
              fwd_rets: [1d_ret, 2d_ret, ..., max_fwd_ret]}, ...]
    """
    rows = []
    for code, data in daily_scores.items():
        n = len(data)
        for i in range(5, n - max_fwd):
            d = data[i]
            date = d[0]
            score = d[1]
            close = d[2]
            open_p, high_p, low_p, vol = d[3], d[4], d[5], d[6]
            sector = d[7]

            # 今日收益率
            yest_c = data[i-1][2]
            ret_today = (close / yest_c - 1) * 100 if yest_c > 0 else 0

            # 5日均量比
            past_vols = [data[j][6] for j in range(max(0, i-5), i)]
            avg_vol = sum(past_vols) / len(past_vols) if past_vols else vol
            vol_ratio = vol / avg_vol if avg_vol > 0 else 1

            # 前向 1-max_fwd 天收益
            fwd_rets = []
            for fwd in range(1, max_fwd + 1):
                if i + fwd < n:
                    fwd_rets.append(round((data[i+fwd][2] / close - 1) * 100, 4))
                else:
                    fwd_rets.append(None)

            rows.append({
                "date": date, "code": code, "score": score,
                "close": close, "ret_today": round(ret_today, 4),
                "vol_ratio": round(vol_ratio, 4), "sector": sector,
                "fwd_rets": fwd_rets
            })
    return rows


# ========== 网格搜索 ==========

def grid_search(panel, score_thresholds, drop_thresholds, fwd_days=[5, 10, 15, 20]):
    """
    对每个 (score_thresh, drop_thresh) 组合计算超额收益。
    返回: {(s, d): {fwd_day: {signal_stats, excess}}}
    """
    # 预计算基线
    baseline = {}
    for fwd in fwd_days:
        vals = [r["fwd_rets"][fwd-1] for r in panel if r["fwd_rets"][fwd-1] is not None]
        baseline[fwd] = calc_stats(vals)

    results = {}
    for s_th in score_thresholds:
        for d_th in drop_thresholds:
            key = (s_th, d_th)
            results[key] = {}
            for fwd in fwd_days:
                signal_vals = [
                    r["fwd_rets"][fwd-1]
                    for r in panel
                    if r["score"] >= s_th and r["ret_today"] <= d_th
                    and r["fwd_rets"][fwd-1] is not None
                ]
                ss = calc_stats(signal_vals)
                excess = round(ss["mean"] - baseline[fwd]["mean"], 2) if ss["mean"] and baseline[fwd]["mean"] else None
                results[key][fwd] = {
                    "n": ss["n"],
                    "mean": ss["mean"],
                    "win_rate": ss["win_rate"],
                    "excess": excess,
                    "baseline_mean": baseline[fwd]["mean"],
                }

    return results, baseline


# ========== 前向收益曲线 ==========

def forward_curve(panel, condition_fn, name, max_fwd=20):
    """
    condition_fn(r) -> bool, 判定是否为信号日
    返回 1..max_fwd 天累计收益曲线
    """
    signal_vals = []
    for r in panel:
        if condition_fn(r):
            signal_vals.append(r["fwd_rets"])

    if not signal_vals:
        return None

    curve = []
    for fwd in range(max_fwd):
        day_vals = [rets[fwd] for rets in signal_vals if fwd < len(rets) and rets[fwd] is not None]
        s = calc_stats(day_vals)
        curve.append({"day": fwd + 1, "n": s["n"], "mean": s["mean"],
                       "win_rate": s["win_rate"], "median": s["median"]})
    return curve


def forward_curve_baseline(panel, max_fwd=20):
    """全样本基线收益曲线"""
    all_fwds = [[] for _ in range(max_fwd)]
    for r in panel:
        for fwd in range(max_fwd):
            if fwd < len(r["fwd_rets"]) and r["fwd_rets"][fwd] is not None:
                all_fwds[fwd].append(r["fwd_rets"][fwd])
    curve = []
    for fwd, vals in enumerate(all_fwds):
        s = calc_stats(vals)
        curve.append({"day": fwd + 1, "n": s["n"], "mean": s["mean"],
                       "win_rate": s["win_rate"], "median": s["median"]})
    return curve


# ========== 成交量分层分析 ==========

def volume_layer_analysis(panel, score_th=70, drop_th=-1.0, fwd_days=[5, 10, 15, 20]):
    """高分回调信号，按成交量比分层"""
    layers = {
        "缩量 (<0.7)": lambda r: r["vol_ratio"] < 0.7,
        "低量 (0.7-0.9)": lambda r: 0.7 <= r["vol_ratio"] < 0.9,
        "正常 (0.9-1.1)": lambda r: 0.9 <= r["vol_ratio"] < 1.1,
        "放量 (1.1-1.5)": lambda r: 1.1 <= r["vol_ratio"] < 1.5,
        "巨量 (>1.5)": lambda r: r["vol_ratio"] >= 1.5,
    }
    results = {}
    for layer_name, vol_fn in layers.items():
        signal_vals = {
            fwd: [r["fwd_rets"][fwd-1]
                  for r in panel
                  if r["score"] >= score_th and r["ret_today"] <= drop_th
                  and vol_fn(r) and r["fwd_rets"][fwd-1] is not None]
            for fwd in fwd_days
        }
        results[layer_name] = {fwd: calc_stats(vals) for fwd, vals in signal_vals.items()}
    return results


# ========== 连续回调分析 ==========

def consecutive_drop_analysis(panel, score_th=70, fwd_days=[5, 10, 15, 20]):
    """分析连续N日回调的信号效果"""
    # 按股票分组，找连续下跌的序列
    by_code = defaultdict(list)
    for r in panel:
        by_code[r["code"]].append(r)

    results = {}
    # 单日回调 (基准)
    single = {
        fwd: [r["fwd_rets"][fwd-1]
              for r in panel
              if r["score"] >= score_th and r["ret_today"] < 0
              and r["fwd_rets"][fwd-1] is not None]
        for fwd in fwd_days
    }
    results["单日回调"] = {fwd: calc_stats(vals) for fwd, vals in single.items()}

    # 连跌2日
    for streak in [2, 3]:
        signal_vals = {fwd: [] for fwd in fwd_days}
        for code, code_rows in by_code.items():
            # 按日期排序
            code_rows.sort(key=lambda r: r["date"])
            for i in range(streak - 1, len(code_rows) - max(fwd_days)):
                r = code_rows[i]
                if r["score"] < score_th:
                    continue
                # 检查前 streak-1 天是否都跌
                prev_drops = all(
                    code_rows[j]["ret_today"] < 0
                    for j in range(i - streak + 1, i)
                )
                # 今天也跌
                if prev_drops and r["ret_today"] < 0:
                    for fwd in fwd_days:
                        if r["fwd_rets"][fwd-1] is not None:
                            signal_vals[fwd].append(r["fwd_rets"][fwd-1])

        if signal_vals[fwd_days[0]]:
            results[f"连跌{streak}日"] = {fwd: calc_stats(vals) for fwd, vals in signal_vals.items()}

    return results


# ========== HTML 报告生成 ==========

def generate_html_v3(grid_results, baseline, score_thresholds, drop_thresholds,
                      curves, vol_layers, consec_drops, codes_count, today_str):
    """生成完整的 V3 HTML 报告"""

    # ---- 颜色常量 ----
    RED = "#dc2626"
    GREEN = "#16a34a"
    DARK = "#1a1a2e"
    TH_STYLE = "padding:8px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;text-align:center;white-space:nowrap"
    TD_STYLE = "padding:6px 8px;font-size:12px;text-align:center;white-space:nowrap"

    # ---- 热力图 (5日超额) ----
    fwd = 5
    # 找最优组合
    best_key = None
    best_excess = -999
    best_n = 0
    heat_rows = []
    for s_th in score_thresholds:
        cells = []
        for d_th in drop_thresholds:
            key = (s_th, d_th)
            g = grid_results.get(key, {}).get(fwd, {})
            exc = g.get("excess")
            n = g.get("n", 0)
            if exc is not None and exc > best_excess and n >= 10:
                best_excess = exc
                best_key = key
                best_n = n
            if exc is None or n < 5:
                bg = "#f1f5f9"
                color = "#cbd5e1"
                text = "-"
            else:
                # 红=超额正, 绿=超额负, 强度=abs(excess)
                intensity = min(abs(exc) / 3.0, 1.0)
                if exc > 0:
                    r, g_b, b = 220, int(38 * (1 - intensity)), int(38 * (1 - intensity))
                    bg_val = f"rgb({r},{g_b},{b})"
                    color = "#fff" if intensity > 0.5 else "#333"
                else:
                    r = int(22 * (1 - intensity))
                    g_b = 163 - int(125 * intensity)
                    b = 74 - int(40 * intensity)
                    bg_val = f"rgb({r},{g_b},{b})"
                    color = "#fff" if intensity > 0.5 else "#333"
                text = f"{exc:+.1f}%"
            cells.append({
                "bg": bg_val if n >= 5 else bg,
                "color": color if n >= 5 else "#cbd5e1",
                "text": text,
                "n": n
            })
        heat_rows.append({"score": s_th, "cells": cells})

    # 热力图 HTML
    heat_html = f"""
    <div style="background:#fff;border-radius:10px;padding:16px;margin:12px 0;border:1px solid #e2e8f0">
      <h3 style="margin:0 0 4px;color:{DARK}">🔢 网格搜索热力图 — 5日超额收益</h3>
      <p style="font-size:12px;color:#888;margin:0 0 12px">
        纵轴=最低评分阈值 | 横轴=最大跌幅阈值(%) |
        颜色=超额收益(红=正超额,绿=负超额) | 括号内=样本量
      </p>
      <table style="border-collapse:collapse;font-size:12px">
        <tr><th style="{TH_STYLE};width:70px">评分↓ \\ 跌幅→</th>
          {''.join(f'<th style="{TH_STYLE};width:64px">{d_th:+.0f}%</th>' for d_th in drop_thresholds)}
        </tr>
        {''.join(
          '<tr><td style="padding:6px 8px;font-weight:700;background:#f8f9fc;text-align:center">{score}</td>'
          + ''.join(
            f'<td style="{TD_STYLE};background:{c["bg"]};color:{c["color"]};font-weight:600">{c["text"]}<br><span style="font-size:10px;opacity:0.7">n={c["n"]}</span></td>'
            for c in row["cells"])
          + '</tr>'
          for row in heat_rows
        )}
      </table>
    </div>"""

    # ---- 最优参数详解 ----
    opt_html = ""
    if best_key:
        s_th, d_th = best_key
        opt_html = f"""
    <div style="background:#fef3c7;border-radius:10px;padding:16px;margin:12px 0;border:1px solid #f59e0b44">
      <h3 style="margin:0 0 8px;color:#92400e">🏆 最优参数组合: 评分≥{s_th} + 跌幅≤{d_th:+.0f}%</h3>
      <table style="border-collapse:collapse;font-size:13px;width:100%">
        <tr style="background:#374151;color:#fff">
          <th style="padding:7px 10px;text-align:left">前向周期</th>
          <th style="padding:7px 10px;text-align:right">信号均值</th>
          <th style="padding:7px 10px;text-align:right">基线均值</th>
          <th style="padding:7px 10px;text-align:right">超额收益</th>
          <th style="padding:7px 10px;text-align:right">胜率</th>
          <th style="padding:7px 10px;text-align:right">样本量</th>
        </tr>"""
        for fwd_day in [5, 10, 15, 20]:
            g = grid_results.get(best_key, {}).get(fwd_day, {})
            exc = g.get("excess", "-")
            exc_color = RED if (exc and exc > 0) else (GREEN if (exc and exc < 0) else "#888")
            opt_html += f"""<tr style="border-bottom:1px solid #e8ecf1">
              <td style="padding:7px 10px;font-weight:600">未来{fwd_day}日</td>
              <td style="padding:7px 10px;text-align:right;font-weight:600;color:{RED if g.get('mean',0) and g['mean']>0 else GREEN}">{g.get('mean','-')}%</td>
              <td style="padding:7px 10px;text-align:right">{g.get('baseline_mean','-')}%</td>
              <td style="padding:7px 10px;text-align:right;font-weight:700;color:{exc_color}">{exc:+}%</td>
              <td style="padding:7px 10px;text-align:right">{g.get('win_rate','-')}%</td>
              <td style="padding:7px 10px;text-align:right">{g.get('n','-')}</td></tr>"""
        opt_html += "</table></div>"

    # ---- 前向收益曲线 ----
    curves_html = """
    <div style="background:#fff;border-radius:10px;padding:16px;margin:12px 0;border:1px solid #e2e8f0">
      <h3 style="margin:0 0 12px;color:{DARK}">📈 前向收益曲线 (累计)</h3>
      <p style="font-size:12px;color:#888;margin:0 0 8px">信号触发后1-20天的累计收益 vs 全样本基线</p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">"""

    # 为每个信号画一个 mini SVG chart
    curve_colors = {
        "最优高分回调": RED,
        "高分(≥70)+跌<-1%": "#f59e0b",
        "高分(≥75)+跌<-2%": "#8b5cf6",
        "基线": "#94a3b8",
    }

    for idx, (cur_name, curve_data, base_curve) in enumerate(curves):
        if not curve_data:
            continue
        color = curve_colors.get(cur_name, "#6366f1")
        max_day = min(20, len(curve_data))

        # 准备SVG数据点
        svg_w, svg_h = 340, 200
        pad_l, pad_r, pad_t, pad_b = 50, 20, 24, 32

        # 计算y范围
        all_y = [p["mean"] for p in curve_data[:max_day] if p["mean"] is not None]
        if base_curve:
            all_y += [p["mean"] for p in base_curve[:max_day] if p["mean"] is not None]
        if not all_y:
            continue
        y_min = min(all_y) - 1
        y_max = max(all_y) + 1
        if y_max - y_min < 2:
            y_mid = (y_max + y_min) / 2
            y_min = y_mid - 1
            y_max = y_mid + 1

        def tx(d):
            return pad_l + (d - 1) / (max_day - 1) * (svg_w - pad_l - pad_r) if max_day > 1 else pad_l + (svg_w - pad_l - pad_r) / 2

        def ty(v):
            return pad_t + (y_max - v) / (y_max - y_min) * (svg_h - pad_t - pad_b)

        # 信号曲线点
        points = []
        for p in curve_data[:max_day]:
            if p["mean"] is not None:
                points.append(f"{tx(p['day']):.1f},{ty(p['mean']):.1f}")
        polyline = " ".join(points)

        # 基线曲线点
        base_points = []
        if base_curve:
            for p in base_curve[:max_day]:
                if p["mean"] is not None:
                    base_points.append(f"{tx(p['day']):.1f},{ty(p['mean']):.1f}")
        base_poly = " ".join(base_points)

        # 标记点
        markers = []
        for p in [curve_data[4], curve_data[9], curve_data[14], curve_data[19]]:
            if p and p["mean"] is not None and p["day"] <= max_day:
                markers.append(
                    f'<circle cx="{tx(p["day"]):.1f}" cy="{ty(p["mean"]):.1f}" r="4" fill="{color}" stroke="#fff" stroke-width="2"/>'
                    f'<text x="{tx(p["day"]):.1f}" y="{ty(p["mean"]) - 8}" text-anchor="middle" font-size="10" fill="{color}" font-weight="600">{p["mean"]:+.1f}%</text>'
                )

        # Y轴标签
        y_labels = ""
        for v in [y_min, (y_min + y_max) / 2, y_max]:
            y_labels += f'<text x="{pad_l - 6}" y="{ty(v) + 4}" text-anchor="end" font-size="10" fill="#888">{v:.1f}%</text>'
            y_labels += f'<line x1="{pad_l - 2}" y1="{ty(v)}" x2="{svg_w - pad_r}" y2="{ty(v)}" stroke="#e8ecf1" stroke-dasharray="3,3"/>'

        # X轴标签
        x_labels = ""
        for d in [1, 5, 10, 15, 20]:
            if d <= max_day:
                x_labels += f'<text x="{tx(d):.1f}" y="{svg_h - 8}" text-anchor="middle" font-size="10" fill="#888">第{d}天</text>'

        svg = f"""
        <div style="margin-bottom:8px">
          <span style="font-weight:600;font-size:13px;color:{color}">{cur_name}</span>
          <span style="font-size:11px;color:#888;margin-left:8px">n={curve_data[0]['n']}</span>
        </div>
        <svg viewBox="0 0 {svg_w} {svg_h}" style="width:100%;max-width:{svg_w}px;height:{svg_h}px;background:#fafbfc;border-radius:8px">
          {y_labels}
          {x_labels}
          <!-- 基线 -->
          {f'<polyline points="{base_poly}" fill="none" stroke="#94a3b8" stroke-width="2" stroke-dasharray="6,3" opacity="0.8"/>' if base_points else ''}
          <!-- 信号曲线 -->
          {f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="2.5"/>' if points else ''}
          {''.join(markers)}
        </svg>"""

        curves_html += svg

    curves_html += "</div></div>"

    # ---- 成交量分层 ----
    vol_html = f"""
    <div style="background:#fff;border-radius:10px;padding:16px;margin:12px 0;border:1px solid #e2e8f0">
      <h3 style="margin:0 0 12px;color:{DARK}">📊 成交量分层 — 高分(≥70)+跌(≤-1%) 信号</h3>
      <p style="font-size:12px;color:#888;margin:0 0 8px">不同成交量比下的回调买入效果</p>
      <table style="border-collapse:collapse;font-size:13px;width:100%">
        <tr style="background:#374151;color:#fff">
          <th style="padding:7px 10px;text-align:left">成交量分层</th>
          <th style="padding:7px 10px;text-align:right">5日均值</th>
          <th style="padding:7px 10px;text-align:right">5日胜率</th>
          <th style="padding:7px 10px;text-align:right">10日均值</th>
          <th style="padding:7px 10px;text-align:right">10日胜率</th>
          <th style="padding:7px 10px;text-align:right">15日均值</th>
          <th style="padding:7px 10px;text-align:right">样本量</th>
        </tr>"""

    for layer_name in ["缩量 (<0.7)", "低量 (0.7-0.9)", "正常 (0.9-1.1)", "放量 (1.1-1.5)", "巨量 (>1.5)"]:
        layer = vol_layers.get(layer_name, {})
        s5 = layer.get(5, {})
        s10 = layer.get(10, {})
        s15 = layer.get(15, {})
        if s5.get("n", 0) == 0:
            continue
        row_color = "#fef2f2" if "缩量" in layer_name else ("#fffbeb" if "巨量" in layer_name else "#fff")
        vol_html += f"""<tr style="background:{row_color};border-bottom:1px solid #e8ecf1">
          <td style="padding:7px 10px;font-weight:600">{layer_name}</td>
          <td style="padding:7px 10px;text-align:right;font-weight:600;color:{RED if s5.get('mean',0) and s5['mean']>0 else GREEN}">{s5.get('mean','-')}%</td>
          <td style="padding:7px 10px;text-align:right">{s5.get('win_rate','-')}%</td>
          <td style="padding:7px 10px;text-align:right;font-weight:600;color:{RED if s10.get('mean',0) and s10['mean']>0 else GREEN}">{s10.get('mean','-')}%</td>
          <td style="padding:7px 10px;text-align:right">{s10.get('win_rate','-')}%</td>
          <td style="padding:7px 10px;text-align:right">{s15.get('mean','-')}%</td>
          <td style="padding:7px 10px;text-align:right;color:#888">{s5.get('n','-')}</td></tr>"""

    vol_html += "</table></div>"

    # ---- 连续回调 ----
    consec_html = f"""
    <div style="background:#fff;border-radius:10px;padding:16px;margin:12px 0;border:1px solid #e2e8f0">
      <h3 style="margin:0 0 12px;color:{DARK}">🔁 连续回调 — 高分(≥70)的N日连跌效果</h3>
      <table style="border-collapse:collapse;font-size:13px;width:100%">
        <tr style="background:#374151;color:#fff">
          <th style="padding:7px 10px;text-align:left">回调类型</th>
          <th style="padding:7px 10px;text-align:right">5日均值</th>
          <th style="padding:7px 10px;text-align:right">5日胜率</th>
          <th style="padding:7px 10px;text-align:right">10日均值</th>
          <th style="padding:7px 10px;text-align:right">10日胜率</th>
          <th style="padding:7px 10px;text-align:right">15日均值</th>
          <th style="padding:7px 10px;text-align:right">样本量</th>
        </tr>"""

    for name in ["单日回调", "连跌2日", "连跌3日"]:
        data = consec_drops.get(name, {})
        s5 = data.get(5, {})
        s10 = data.get(10, {})
        s15 = data.get(15, {})
        if s5.get("n", 0) == 0:
            continue
        bg = "#fef3c7" if "3" in name else ("#fff7ed" if "2" in name else "#fff")
        consec_html += f"""<tr style="background:{bg};border-bottom:1px solid #e8ecf1">
          <td style="padding:7px 10px;font-weight:600">{name}</td>
          <td style="padding:7px 10px;text-align:right;font-weight:600;color:{RED if s5.get('mean',0) and s5['mean']>0 else GREEN}">{s5.get('mean','-')}%</td>
          <td style="padding:7px 10px;text-align:right">{s5.get('win_rate','-')}%</td>
          <td style="padding:7px 10px;text-align:right;font-weight:600;color:{RED if s10.get('mean',0) and s10['mean']>0 else GREEN}">{s10.get('mean','-')}%</td>
          <td style="padding:7px 10px;text-align:right">{s10.get('win_rate','-')}%</td>
          <td style="padding:7px 10px;text-align:right">{s15.get('mean','-')}%</td>
          <td style="padding:7px 10px;text-align:right;color:#888">{s5.get('n','-')}</td></tr>"""

    consec_html += "</table></div>"

    # ---- 合成为完整 HTML ----
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SS-Enhanced 实时信号回测 V3 — 网格搜索 {today_str}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}}
.container{{max-width:960px;margin:0 auto;padding:20px}}
.header{{background:linear-gradient(135deg,{DARK},#16213e);border-radius:12px;padding:28px 30px;color:#fff;margin-bottom:0}}
.header h1{{margin:0;font-size:22px;color:#e94560}}
.header p{{margin:6px 0 0;font-size:13px;color:#94a3b8}}
.section-title{{font-size:16px;font-weight:700;color:{DARK};margin:20px 0 8px}}
</style></head><body><div class="container">
<div class="header">
<h1>🔬 SS-Enhanced 实时信号回测 V3</h1>
<p>网格搜索 · 收益曲线 · 成交量分层 · 连续回调 — 高分回调信号深度探索</p>
<p>数据: {today_str} · {codes_count}只A股 · 回测窗口: 120交易日</p></div>

{heat_html}
{opt_html}
{curves_html}
{vol_html}
{consec_html}

<div style="text-align:center;padding:16px;color:#999;font-size:11px">
自动生成 | SS-Enhanced V3 | 历史数据回测 · 不构成投资建议</div>
</div></body></html>"""

    return html


# ========== 主流程 ==========

def run():
    today_str = datetime.now().strftime("%Y-%m-%d")

    codes_file = os.path.join(SCRIPT_DIR, "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip()]

    print(f"╔══════════════════════════════════════════════╗")
    print(f"║  SS-Enhanced 实时信号回测 V3 [{today_str}]   ║")
    print(f"╚══════════════════════════════════════════════╝")
    print(f"股票池: {len(codes)} 只\n")

    # [1] K线 + 评分
    print("[1/6] 获取K线 + 计算历史评分...")
    klines_all = fetch_kline_batch(codes, days=200)
    print(f"  K线: {len(klines_all)} 只")

    daily_scores = {}
    for idx, code in enumerate(codes):
        kl = klines_all.get(code)
        if not kl or len(kl) < 120:
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
        if (idx + 1) % 50 == 0:
            print(f"  进度: {idx+1}/{len(codes)}")
    print(f"  评分完成: {len(daily_scores)} 只")

    # [2] 构建面板
    print("\n[2/6] 构建日级别面板 (前向20天)...")
    panel = build_panel(daily_scores, max_fwd=20)
    print(f"  面板总记录: {len(panel)} 条")

    # [3] 网格搜索
    print("\n[3/6] 网格搜索...")
    score_thresholds = [60, 65, 70, 75, 80]
    drop_thresholds = [-0.5, -1.0, -1.5, -2.0, -2.5, -3.0, -4.0, -5.0]
    grid_results, baseline = grid_search(panel, score_thresholds, drop_thresholds)

    # 打印最佳组合
    best_key = None
    best_exc = -999
    for (s, d), res in grid_results.items():
        exc = res.get(5, {}).get("excess")
        n = res.get(5, {}).get("n", 0)
        if exc is not None and exc > best_exc and n >= 10:
            best_exc = exc
            best_key = (s, d)

    if best_key:
        s, d = best_key
        r = grid_results[best_key]
        print(f"  最优: 评分≥{s}, 跌幅≤{d:+.0f}%")
        print(f"    5日: {r[5]['mean']}% (超额{r[5]['excess']:+.2f}%, n={r[5]['n']})")
        print(f"    10日: {r[10]['mean']}% (超额{r[10]['excess']:+.2f}%)")
        print(f"    15日: {r[15]['mean']}% (超额{r[15]['excess']:+.2f}%)")
        print(f"    20日: {r[20]['mean']}% (超额{r[20]['excess']:+.2f}%)")

    # 基线打印
    print(f"\n  基线: 5d={baseline[5]['mean']}%, 10d={baseline[10]['mean']}%, "
          f"15d={baseline[15]['mean']}%, 20d={baseline[20]['mean']}%")

    # [4] 前向收益曲线
    print("\n[4/6] 计算前向收益曲线...")
    base_curve = forward_curve_baseline(panel, max_fwd=20)

    # 三条信号曲线
    curve_specs = []
    if best_key:
        s_th, d_th = best_key
        c = forward_curve(panel,
                          lambda r: r["score"] >= s_th and r["ret_today"] <= d_th,
                          f"最优高分回调 (≥{s_th}, ≤{d_th:+.0f}%)")
        curve_specs.append((f"最优高分回调 (≥{s_th}, ≤{d_th:+.0f}%)", c, base_curve))

    c2 = forward_curve(panel,
                       lambda r: r["score"] >= 70 and r["ret_today"] <= -1,
                       "高分(≥70)+跌<-1%")
    curve_specs.append(("高分(≥70)+跌<-1%", c2, base_curve))

    c3 = forward_curve(panel,
                       lambda r: r["score"] >= 75 and r["ret_today"] <= -2,
                       "高分(≥75)+跌<-2%")
    curve_specs.append(("高分(≥75)+跌<-2%", c3, base_curve))

    print(f"  曲线: {len(curve_specs)} 条信号曲线 + 1 基线")

    # [5] 成交量分层
    print("\n[5/6] 成交量分层分析...")
    vol_layers = volume_layer_analysis(panel, score_th=70, drop_th=-1.0)
    for name, res in vol_layers.items():
        n = res[5]["n"]
        m = res[5]["mean"]
        if n > 0:
            print(f"  {name}: n={n}, 5d={m}%")

    # [6] 连续回调
    print("\n[6/6] 连续回调分析...")
    consec_drops = consecutive_drop_analysis(panel, score_th=70)
    for name, res in consec_drops.items():
        n = res[5]["n"]
        m = res[5]["mean"]
        print(f"  {name}: n={n}, 5d={m}%")

    # ---- 生成 HTML ----
    print("\n生成 HTML 报告...")
    html = generate_html_v3(
        grid_results, baseline, score_thresholds, drop_thresholds,
        curve_specs, vol_layers, consec_drops,
        len(codes), today_str
    )
    html_path = os.path.join(OUTPUT_DIR, f"实时信号回测V3_{today_str}.html")
    with open(html_path, "w") as f:
        f.write(html)
    print(f"  HTML: {html_path}")

    # 保存 JSON
    json_path = os.path.join(OUTPUT_DIR, f"实时信号回测V3_{today_str}.json")
    json_out = {
        "date": today_str,
        "baseline": {str(k): v for k, v in baseline.items()},
        "grid_search": {
            "score_thresholds": score_thresholds,
            "drop_thresholds": drop_thresholds,
            "results": {f"{s}_{d}": {
                str(fwd): r for fwd, r in res.items()
            } for (s, d), res in grid_results.items()}
        },
        "best_params": {"score": best_key[0], "drop": best_key[1]} if best_key else None,
        "vol_layers": {k: {str(fwd): v for fwd, v in res.items()} for k, res in vol_layers.items()},
        "consec_drops": {k: {str(fwd): v for fwd, v in res.items()} for k, res in consec_drops.items()},
    }
    with open(json_path, "w") as f:
        json.dump(json_out, f, ensure_ascii=False, indent=2)
    print(f"  JSON: {json_path}")

    return html_path


if __name__ == "__main__":
    run()
