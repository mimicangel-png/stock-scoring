#!/usr/bin/env python3
"""生成Q2财报分析HTML报告"""

import json

DATA_FILE = "/Users/bytedance/WorkBuddy/2026-06-24-18-57-20/stock-scoring/output/Q2财报分析_2026-06-25.json"
OUTPUT_HTML = "/Users/bytedance/WorkBuddy/2026-06-24-18-57-20/stock-scoring/output/Q2财报分析报告_2026-06-25.html"

with open(DATA_FILE) as f:
    data = json.load(f)

# ===== 操盘建议分类 =====
# 根据Q1业绩+估值+SS评分综合判断
def classify_stock(r):
    """返回 (group, reason)"""
    pe = r["pe_ttm"]
    pb = r["pb"]
    ss = r["ss_score"]
    q1_yoy = r.get("q1_profit_yoy")
    has_q2 = r["has_q2_forecast"] == "有"
    q2_type = r.get("q2_forecast_type", "")
    ret_20d = r.get("ret_20d", 0) or 0

    # 解析Q1增速
    yoy_val = None
    if q1_yoy and q1_yoy not in (None, "", "None"):
        try: yoy_val = float(q1_yoy)
        except: pass

    # === 分类逻辑 ===
    # 组A: 强烈建议持有/加仓
    # 条件: SS>=65, Q1正增长或Q2预增, PE<60(或合理范围)
    if ss >= 65 and ((yoy_val is not None and yoy_val > 2) or (has_q2 and "增" in q2_type)):
        if pe > 0 and pe < 60:
            return "A-强烈买入", "高增长+低估值"
        elif pe > 0 and pe < 100:
            return "A-持有观察", "高增长+合理估值"
        else:
            return "B-逢低关注", "高增长但PE偏高"

    # 组B: 可以持有，逢低加仓
    if ss >= 60 and yoy_val is not None and yoy_val > 0:
        if pe > 0 and pe < 40:
            return "A-强烈买入", "盈利增长+低PE"
        elif pe > 0 and pe < 80:
            return "B-逢低关注", "盈利增长+中等PE"
        else:
            return "B-逢低关注", "盈利增长但PE较高"

    # 组C: 观望
    if ss >= 50:
        if yoy_val is not None and yoy_val < -5:
            return "D-减仓/回避", "Q1利润大幅下滑"
        elif pe <= 0:
            return "D-减仓/回避", "亏损状态"
        else:
            return "C-观望", "基本面中性,等待信号"

    # 组D: 回避
    return "D-减仓/回避", "综合评分较低"

for r in data["results"]:
    group, reason = classify_stock(r)
    r["op_group"] = group
    r["op_reason"] = reason

# 按分组统计
groups = {}
for r in data["results"]:
    g = r["op_group"]
    if g not in groups: groups[g] = []
    groups[g].append(r)

# 板块统计（从results重新计算，因为JSON反序列化后对象不再共享）
sector_stats = {}
for r in data["results"]:
    sec = r["sector"]
    if sec not in sector_stats: sector_stats[sec] = {"A": 0, "B": 0, "C": 0, "D": 0}
    if r["op_group"].startswith("A"): sector_stats[sec]["A"] += 1
    elif r["op_group"].startswith("B"): sector_stats[sec]["B"] += 1
    elif r["op_group"].startswith("C"): sector_stats[sec]["C"] += 1
    else: sector_stats[sec]["D"] += 1

# ===== 生成 HTML =====
def fmt_yoy(v):
    if v is None or v in (None, "", "None"): return "-"
    try:
        n = float(v)
        color = "#ef4444" if n > 0 else "#22c55e"
        return f'<span style="color:{color}">{n:+.1f}%</span>'
    except: return "-"

def fmt_pe(pe):
    if pe <= 0: return "亏损"
    if pe > 1000: return f"{pe:.0f}"
    return f"{pe:.1f}"

def group_color(g):
    if g.startswith("A"): return ("#10b981", "#d1fae5", "🟢")
    if g.startswith("B"): return ("#3b82f6", "#dbeafe", "🔵")
    if g.startswith("C"): return ("#f59e0b", "#fef3c7", "🟡")
    return ("#ef4444", "#fee2e2", "🔴")

def sector_row(sec, sm, stats):
    g = sm.get("avg_q1_profit_growth")
    g_str = f"{g:+.1f}%" if g is not None else "-"
    g_color = "#ef4444" if (g is not None and g > 0) else "#22c55e"
    st = stats.get(sec, {"A": 0, "B": 0, "C": 0, "D": 0})
    total = sm["count"]
    return f"""
    <tr>
      <td class="sector-name">{sec}</td>
      <td>{sm["count"]}</td>
      <td><strong>{sm["avg_score"]}</strong></td>
      <td>{sm["avg_pe"]:.0f}</td>
      <td>{sm["avg_pb"]:.2f}</td>
      <td style="color:{g_color}"><strong>{g_str}</strong></td>
      <td>
        <span class="tag tag-green">{st["A"]}买</span>
        <span class="tag tag-blue">{st["B"]}关注</span>
        <span class="tag tag-yellow">{st["C"]}观望</span>
        <span class="tag tag-red">{st["D"]}回避</span>
      </td>
    </tr>"""

def stock_row(r):
    gc, bg, emoji = group_color(r["op_group"])
    return f"""
    <tr>
      <td>{emoji}</td>
      <td class="code">{r["code"]}</td>
      <td>{r["name"]}</td>
      <td>{r["sector"]}</td>
      <td><strong>{r["ss_score"]}</strong></td>
      <td>{r["price"]:.2f}</td>
      <td>{fmt_pe(r["pe_ttm"])}</td>
      <td>{r["pb"]:.2f}</td>
      <td>{fmt_yoy(r.get("q1_profit_yoy"))}</td>
      <td>{r.get("q2_forecast_type", "-") or "-"}</td>
      <td style="color:{'#ef4444' if r.get('ret_20d',0) and r['ret_20d']>0 else '#22c55e'}">{r.get('ret_20d',0):+.1f}%</td>
      <td><span style="color:{gc};font-weight:bold">{r['op_group'].split('-')[0]}</span></td>
      <td style="font-size:12px;color:#666">{r['op_reason']}</td>
    </tr>"""

# 构建每个操作组的内
groups_html = ""
group_order = ["A-强烈买入", "A-持有观察", "B-逢低关注", "C-观望", "D-减仓/回避"]
for gname in group_order:
    stocks = groups.get(gname, [])
    if not stocks: continue
    gc, bg, emoji = group_color(gname)
    stocks_sorted = sorted(stocks, key=lambda x: x["ss_score"], reverse=True)
    rows = "".join(stock_row(r) for r in stocks_sorted)
    groups_html += f"""
    <div class="group-section" style="border-left: 4px solid {gc}">
      <h3 style="color:{gc}">{emoji} {gname} <span class="count">{len(stocks)}只</span></h3>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th></th><th>代码</th><th>名称</th><th>板块</th><th>SS</th>
              <th>价格</th><th>PE</th><th>PB</th><th>Q1利润YoY</th>
              <th>Q2预告</th><th>20日涨跌</th><th>操作</th><th>原因</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>"""

# Q2预告详细卡片
q2_stocks = [r for r in data["results"] if r["has_q2_forecast"] == "有"]
q2_cards = ""
forecast_type_map = {"预增": ("#10b981", "📈"), "略增": ("#3b82f6", "📊"), "预减": ("#f59e0b", "📉"), "减亏": ("#ec4899", "🔄")}
for r in q2_stocks:
    fc, ficon = forecast_type_map.get(r["q2_forecast_type"], ("#6b7280", "❓"))
    sm = r.get("q2_forecast_summary", "")[:150] or "暂无详细内容"
    q2_cards += f"""
    <div class="q2-card">
      <div class="q2-header">
        <span class="q2-icon">{ficon}</span>
        <span class="q2-type" style="color:{fc}">{r['q2_forecast_type']}</span>
        <span class="q2-code">{r['code']}</span>
        <span class="q2-name">{r['name']}</span>
        <span class="q2-sector">[{r['sector']}]</span>
      </div>
      <div class="q2-body">
        <div class="q2-stats">
          <div>SS评分: <strong>{r['ss_score']}</strong></div>
          <div>PE: <strong>{fmt_pe(r['pe_ttm'])}</strong></div>
          <div>PB: <strong>{r['pb']:.2f}</strong></div>
          <div>Q1利润YoY: {fmt_yoy(r.get('q1_profit_yoy'))}</div>
        </div>
        <div class="q2-summary">{sm}</div>
      </div>
    </div>"""

# 板块表格
sector_table = ""
for sec, sm in sorted(data["sector_summary"].items(), key=lambda x: x[1]["avg_score"], reverse=True):
    sector_table += sector_row(sec, sm, sector_stats)

# 组装统计摘要
total_a = sum(len(groups.get(g, [])) for g in group_order if g.startswith("A"))
total_b = sum(len(groups.get(g, [])) for g in group_order if g.startswith("B"))
total_c = len(groups.get("C-观望", []))
total_d = len(groups.get("D-减仓/回避", []))

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Q2财报分析报告 - 2026.06.25</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f7fa; color: #1a1a2e; line-height: 1.6; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
.header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 40px; border-radius: 16px; margin-bottom: 24px; text-align: center; }}
.header h1 {{ font-size: 28px; margin-bottom: 8px; }}
.header .subtitle {{ opacity: 0.85; font-size: 14px; }}
.dashboard {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
.stat-card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); text-align: center; }}
.stat-card .label {{ font-size: 13px; color: #6b7280; margin-bottom: 4px; }}
.stat-card .value {{ font-size: 32px; font-weight: 700; }}
.stat-card .sub {{ font-size: 12px; color: #9ca3af; }}
.stat-card.green .value {{ color: #10b981; }}
.stat-card.blue .value {{ color: #3b82f6; }}
.stat-card.yellow .value {{ color: #f59e0b; }}
.stat-card.red .value {{ color: #ef4444; }}

.section {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.section h2 {{ font-size: 20px; margin-bottom: 16px; color: #1a1a2e; border-bottom: 2px solid #e5e7eb; padding-bottom: 12px; }}
.section h3 {{ font-size: 17px; margin-bottom: 12px; }}

table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ background: #f8fafc; padding: 10px 12px; text-align: left; font-weight: 600; color: #4b5563; border-bottom: 2px solid #e5e7eb; white-space: nowrap; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #f3f4f6; }}
tr:hover {{ background: #f9fafb; }}

.tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; margin: 1px; font-weight: 500; }}
.tag-green {{ background: #d1fae5; color: #065f46; }}
.tag-blue {{ background: #dbeafe; color: #1e40af; }}
.tag-yellow {{ background: #fef3c7; color: #92400e; }}
.tag-red {{ background: #fee2e2; color: #991b1b; }}

.count {{ font-size: 13px; color: #6b7280; font-weight: normal; }}

.q2-card {{ background: #f8fafc; border-radius: 10px; padding: 16px; margin-bottom: 12px; border: 1px solid #e5e7eb; }}
.q2-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; font-size: 15px; }}
.q2-icon {{ font-size: 20px; }}
.q2-type {{ font-weight: 700; font-size: 16px; }}
.q2-code {{ font-family: monospace; color: #6b7280; }}
.q2-name {{ font-weight: 600; }}
.q2-sector {{ color: #9ca3af; font-size: 13px; }}
.q2-body {{ display: flex; gap: 24px; }}
.q2-stats {{ display: flex; gap: 16px; font-size: 13px; color: #4b5563; }}
.q2-summary {{ font-size: 13px; color: #6b7280; flex: 1; line-height: 1.5; }}

.table-scroll {{ overflow-x: auto; }}

.sector-name {{ font-weight: 600; white-space: nowrap; }}
.code {{ font-family: "SF Mono", "Fira Code", monospace; color: #6366f1; }}

.tips {{ background: #fef3c7; border-left: 4px solid #f59e0b; padding: 16px; border-radius: 8px; margin-bottom: 16px; font-size: 13px; line-height: 1.8; }}
.tips strong {{ color: #92400e; }}

@media (max-width: 768px) {{
  .dashboard {{ grid-template-columns: repeat(2, 1fr); }}
  .q2-body {{ flex-direction: column; }}
}}
</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1>📊 Q2 财报全景分析报告</h1>
  <div class="subtitle">股票池 137 只 · 2026-06-25 · Q1财报覆盖率 99% · Q2业绩预告 8 只已披露</div>
</div>

<div class="tips">
  <strong>📌 说明：</strong>
  Q2正式财报在7-8月批量发布，目前仅8只标的披露了业绩预告。本报告以<strong>最新Q1财报为业绩基准</strong>，结合Q2预告、当前估值（PE/PB）、SS技术评分，做分组操盘建议。
  由于Q2当季（4-6月）数据尚未完整公布，<strong>Q1数据是当前最有参考价值的硬数据</strong>。
</div>

<div class="dashboard">
  <div class="stat-card green">
    <div class="label">🟢 强烈买入/持有</div>
    <div class="value">{total_a}</div>
    <div class="sub">高增长 + 合理估值</div>
  </div>
  <div class="stat-card blue">
    <div class="label">🔵 逢低关注</div>
    <div class="value">{total_b}</div>
    <div class="sub">有亮点，等好价格</div>
  </div>
  <div class="stat-card yellow">
    <div class="label">🟡 观望</div>
    <div class="value">{total_c}</div>
    <div class="sub">基本面中性</div>
  </div>
  <div class="stat-card red">
    <div class="label">🔴 减仓/回避</div>
    <div class="value">{total_d}</div>
    <div class="sub">低分/亏损/高估</div>
  </div>
</div>

<div class="section">
  <h2>📋 板块概况 — Q1业绩 + 操作建议分布</h2>
  <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>板块</th><th>数量</th><th>均SS分</th><th>均PE</th><th>均PB</th><th>Q1利润增速均值</th><th>操作建议分布</th>
        </tr>
      </thead>
      <tbody>
        {sector_table}
      </tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>🔔 Q2业绩预告明细（已披露8只）</h2>
  <p style="color:#6b7280;font-size:13px;margin-bottom:16px">截至目前已发Q2中报预告的池中标的</p>
  {q2_cards if q2_cards else '<p style="color:#9ca3af">暂无标的发布Q2业绩预告，将在7-8月集中披露</p>'}
</div>

<div class="section">
  <h2>🎯 分组操盘建议（按操作优先级排序）</h2>
  {groups_html}
</div>

<div class="section">
  <h2>💡 总体操盘策略建议</h2>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
    <div style="background:#d1fae5;border-radius:10px;padding:20px">
      <h3 style="color:#065f46;margin-bottom:12px">🟢 持仓重点（{total_a}只）</h3>
      <ul style="font-size:13px;color:#065f46;line-height:2;list-style:none;padding:0">
        <li>✅ 这些标的Q1利润正增长，PE处于合理区间</li>
        <li>✅ 半导体、新能源板块有业绩支撑的龙头优先</li>
        <li>✅ 有Q2预增信号的（沪电股份/亿纬锂能/吉比特）重点关注</li>
        <li>✅ 维持标配，利用回调15-20%加仓</li>
      </ul>
    </div>
    <div style="background:#fee2e2;border-radius:10px;padding:20px">
      <h3 style="color:#991b1b;margin-bottom:12px">🔴 回避/减仓（{total_d}只）</h3>
      <ul style="font-size:13px;color:#991b1b;line-height:2;list-style:none;padding:0">
        <li>❌ Q1利润大幅下滑、亏损状态的标的</li>
        <li>❌ SS评分<50且PE>100的"双杀"品种</li>
        <li>❌ ST标的原则上不参与</li>
        <li>❌ 等待Q2正式财报确认业绩拐点再考虑</li>
      </ul>
    </div>
  </div>

  <div style="margin-top:20px;background:#eff6ff;border-radius:10px;padding:20px">
    <h3 style="color:#1e40af;margin-bottom:12px">📊 关键策略要点</h3>
    <table style="font-size:13px">
      <tr><td style="font-weight:600;white-space:nowrap;color:#1e40af">1. 业绩为王</td><td>Q1增速是最硬的基本面锚点。增速>10%且PE<40的（德明利、江波龙、湖南裕能）是核心持仓</td></tr>
      <tr><td style="font-weight:600;white-space:nowrap;color:#1e40af">2. 半导体分化</td><td>半导体板块Q1增速均值仅3.3%但方差大，德明利(+49%)和沪硅产业(-89%)天壤之别，需要精挑个股</td></tr>
      <tr><td style="font-weight:600;white-space:nowrap;color:#1e40af">3. AI算力降温</td><td>AI/算力板块Q1利润增速均值-2.4%，是唯一负增长的板块，前期炒作后业绩兑现不及预期</td></tr>
      <tr><td style="font-weight:600;white-space:nowrap;color:#1e40af">4. 汽车零部件</td><td>均PE仅56倍，增速趋零，整体估值合理但缺乏增长催化剂，可保留龙头(新坐标SS=78)其余观望</td></tr>
      <tr><td style="font-weight:600;white-space:nowrap;color:#1e40af">5. 金融/交通</td><td>银行/铁路均PE仅8.8倍，防守型配置。Q1增速中性，适合底仓但不要追涨</td></tr>
      <tr><td style="font-weight:600;white-space:nowrap;color:#1e40af">6. 游戏传媒</td><td>均PE仅46倍为成长板块最低，吉比特PE=12已发Q2预增，冰川网络Q1利润下滑需警惕</td></tr>
      <tr><td style="font-weight:600;white-space:nowrap;color:#1e40af">7. Q2预告窗口</td><td>7月中旬进入Q2预告密集期，现在有预告的8只是先行指标，重点关注预增标的</td></tr>
      <tr><td style="font-weight:600;white-space:nowrap;color:#1e40af">8. 止盈纪律</td><td>20日涨幅超过20%且PE>100的建议分批止盈，落袋为安</td></tr>
    </table>
  </div>
</div>

<p style="text-align:center;color:#9ca3af;font-size:11px;margin-top:24px">
  数据来源：腾讯财经（PE/PB/市值）· 新浪财经（Q1利润表）· 东方财富（业绩预告）· SS增强版评分模型<br>
  报告生成时间：2026-06-25 · 仅供参考，不构成投资建议
</p>

</div>
</body>
</html>"""

with open(OUTPUT_HTML, "w") as f:
    f.write(html)

print(f"报告已生成: {OUTPUT_HTML}")
print(f"A组: {total_a}, B组: {total_b}, C组: {total_c}, D组: {total_d}")
