#!/usr/bin/env python3
"""
V11 回测报告生成器 — ICIR 加权 + 分级止损
"""

import sys, os, json, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import numpy as np
from stock_db import StockDB
from v11.factor_engine import compute_all_factors, FACTOR_NAMES
from v11.data_builder import compute_forward_returns
from v11.trade_sim import TradeSimulator, Trade
from scheme_comparison import ICIRWeightedScheme, _snapshot


def main():
    t0 = time.time()

    # ====== 加载 ======
    db = StockDB()
    codes_file = os.path.join(os.path.dirname(__file__), "..", "uploaded-stock-codes.txt")
    with open(codes_file) as f:
        codes = [l.strip() for l in f if l.strip()]
    klines_all = db.get_klines(codes, days=300)
    extra_all = db.get_extra_info(codes)
    forward_returns = compute_forward_returns(klines_all, codes)
    all_dates = sorted(set(k["date"] for kl in klines_all.values() for k in kl))

    # ====== WFO窗口 ======
    train_size, test_size, purge = 200, 50, 5
    windows = []
    start = 0
    while start + train_size + purge + test_size <= len(all_dates):
        windows.append({
            "train": all_dates[start:start+train_size],
            "purge": all_dates[start+train_size:start+train_size+purge],
            "test": all_dates[start+train_size+purge:start+train_size+purge+test_size],
            "idx": len(windows),
        })
        start += test_size

    scheme = ICIRWeightedScheme()
    simulator = TradeSimulator(top_pct=0.15, max_positions=20, stop_mode="scored")
    all_trades = []
    all_nav = []

    for w in windows:
        print(f"W{w['idx']}: train={w['train'][0]}~{w['train'][-1]}, test={w['test'][0]}~{w['test'][-1]}")

        # 因子预计算
        test_factors, test_prices = {}, {}
        for date in w["test"]:
            snap = _snapshot(date, klines_all)
            if len(snap) < 30: continue
            fvals = compute_all_factors(snap, extra_all, today_str=date)
            if not fvals: continue
            raw = scheme.score(fvals, FACTOR_NAMES)
            codes_list = list(raw.keys())
            vals = np.array([raw[c] for c in codes_list])
            si = np.argsort(vals)[::-1]
            n = len(si)
            test_factors[date] = {codes_list[i]: {"percentile": rank/n, "ensemble_z": float(vals[i])} for rank, i in enumerate(si)}
            test_prices[date] = {c: snap[c][-1]["close"] for c in snap}

        trades, nav = simulator.run(test_factors, test_prices)
        all_trades.extend(trades)
        all_nav.extend(nav)
        print(f"  → {len(trades)}笔")

    # ====== 统计 ======
    returns = [t.return_pct for t in all_trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    stops = [t for t in all_trades if "stop" in t.exit_reason]
    profits = [t for t in all_trades if "profit" in t.exit_reason]

    # 按评分分桶
    buckets = {}
    for t in all_trades:
        b = min(int(t.entry_percentile * 10), 9)
        if b not in buckets: buckets[b] = []
        buckets[b].append(t)

    # 退出原因
    exit_stats = {}
    for t in all_trades:
        r = t.exit_reason
        if r not in exit_stats: exit_stats[r] = []
        exit_stats[r].append(t.return_pct)

    # ====== HTML ======
    output_dir = os.path.join(os.path.dirname(__file__), "..", "output", "v11")
    os.makedirs(output_dir, exist_ok=True)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>V11 回测报告 | ICIR加权 + 分级止损</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:#f5f5f7;color:#1d1d1f;padding:20px;line-height:1.6}}
h1{{font-size:22px;margin-bottom:4px}}h2{{font-size:16px;margin:24px 0 12px;color:#333}}
.card{{background:#fff;border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}}
.stat{{text-align:center;padding:16px;background:#f8f9fa;border-radius:8px}}
.stat .v{{font-size:28px;font-weight:700}}
.stat .l{{font-size:12px;color:#86868b;margin-top:4px}}
.positive{{color:#d32f2f}}.negative{{color:#2e7d32}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 12px;text-align:center;border-bottom:1px solid #eee}}
th{{background:#f8f9fa;font-weight:600;color:#555}}
tr:hover{{background:#f0f0f5}}
.bar{{height:8px;border-radius:4px;background:#eee;margin-top:4px;overflow:hidden}}
.bar div{{height:100%;border-radius:4px}}
.red{{background:#d32f2f}}.green{{background:#2e7d32}}.blue{{background:#1565c0}}
.tag{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}}
.tag-buy{{background:#e3f2fd;color:#1565c0}}.tag-sell{{background:#ffebee;color:#c62828}}.tag-ok{{background:#e8f5e9;color:#2e7d32}}
td:first-child,th:first-child{{text-align:left}}
</style></head>
<body>
<h1>📊 V11 回测报告</h1>
<p style="color:#86868b;font-size:13px;margin-bottom:20px">
ICIR加权评分 + 分级止损 (top10%=-12%, 10-20%=-8%, 20-30%=-5%) | 
271只股票池 | {len(windows)}个WFO窗口 | {all_dates[0]} ~ {all_dates[-1]}</p>

<div class="grid">
<div class="stat"><div class="v">{len(all_trades)}</div><div class="l">总交易数</div></div>
<div class="stat"><div class="v" class="positive">{len(wins)/max(1,len(all_trades)):.1%}</div><div class="l">胜率</div></div>
<div class="stat"><div class="v" class="{'positive' if returns and np.mean(returns)>0 else 'negative'}">{np.mean(returns):+.1f}%</div><div class="l">平均收益</div></div>
<div class="stat"><div class="v" class="{'positive' if returns and np.median(returns)>0 else 'negative'}">{np.median(returns):+.1f}%</div><div class="l">中位收益</div></div>
<div class="stat"><div class="v">{max(returns):+.1f}%</div><div class="l">最大收益</div></div>
<div class="stat"><div class="v" style="color:#2e7d32">{min(returns):+.1f}%</div><div class="l">最大亏损</div></div>
<div class="stat"><div class="v">{len(stops)/max(1,len(all_trades)):.0%}</div><div class="l">止损率</div></div>
<div class="stat"><div class="v">{np.mean([t.holding_days for t in all_trades]):.0f}天</div><div class="l">平均持仓</div></div>
</div>"""

    # 分桶表
    html += '<div class="card"><h2>📈 分数标定表（按买入排名分桶）</h2><table>'
    html += '<tr><th>截面排名</th><th>交易数</th><th>胜率</th><th>平均收益</th><th>中位收益</th><th>最大收益</th><th>最大亏损</th><th>持仓天</th><th>止损率</th><th>建议</th></tr>'
    for b in sorted(buckets.keys()):
        bt = buckets[b]
        br = [t.return_pct for t in bt]
        bw = [r for r in br if r > 0]
        bs = [t for t in bt if "stop" in t.exit_reason]
        if not bt: continue
        wr = len(bw)/len(bt)
        suggestion = "✅ 积极买入" if wr>=0.6 and np.mean(br)>1 else ("🟡 谨慎买入" if wr>=0.5 else ("⚪ 中性" if wr>=0.4 else "🔴 回避"))
        html += f'<tr><td>top {b*10}-{(b+1)*10}%</td><td>{len(bt)}</td><td>{wr:.0%}</td>'
        html += f'<td class="{"positive" if np.mean(br)>0 else "negative"}">{np.mean(br):+.1f}%</td>'
        html += f'<td>{np.median(br):+.1f}%</td><td>{max(br):+.1f}%</td><td class="negative">{min(br):+.1f}%</td>'
        html += f'<td>{np.mean([t.holding_days for t in bt]):.0f}天</td><td>{len(bs)/max(1,len(bt)):.0%}</td><td>{suggestion}</td></tr>'
    html += '</table></div>'

    # 退出原因
    html += '<div class="card"><h2>🚪 退出原因分析</h2><table>'
    html += '<tr><th>退出原因</th><th>笔数</th><th>占比</th><th>平均收益</th><th>中位收益</th><th>效果</th></tr>'
    for reason in ["take_profit", "signal_decay", "time_exit", "stop_loss"]:
        rets = exit_stats.get(reason, [])
        if not rets: continue
        avg = np.mean(rets); med = np.median(rets)
        effect = "🟢 利润贡献" if avg>2 else ("🟡 微利退出" if avg>0 else "🔴 截断亏损")
        html += f'<tr><td>{reason}</td><td>{len(rets)}</td><td>{len(rets)/len(all_trades):.0%}</td>'
        html += f'<td class="{"positive" if avg>0 else "negative"}">{avg:+.1f}%</td><td>{med:+.1f}%</td><td>{effect}</td></tr>'
    html += '</table></div>'

    # WFO 窗口对比
    html += '<div class="card"><h2>🔄 WFO 窗口对比</h2><table>'
    html += '<tr><th>窗口</th><th>训练期</th><th>测试期</th><th>交易数</th><th>胜率</th><th>均收益</th></tr>'
    for w in windows:
        wt = [t for t in all_trades if t.exit_date <= w["test"][-1]]
        # 简化
        html += f'<tr><td>W{w["idx"]}</td><td>{w["train"][0]}~{w["train"][-1]}</td><td>{w["test"][0]}~{w["test"][-1]}</td><td>—</td><td>—</td><td>—</td></tr>'
    html += '</table></div>'

    # 交易明细 TOP/BOTTOM
    sorted_trades = sorted(all_trades, key=lambda t: -t.return_pct)
    html += '<div class="card"><h2>🏆 最佳/最差交易 TOP 10</h2><table>'
    html += '<tr><th>代码</th><th>入场日</th><th>出场日</th><th>收益</th><th>持仓天</th><th>退出原因</th><th>买入排名</th></tr>'
    for t in sorted_trades[:10]:
        html += f'<tr><td>{t.code}</td><td>{t.entry_date}</td><td>{t.exit_date}</td><td class="positive">+{t.return_pct:.1f}%</td><td>{t.holding_days}天</td><td>{t.exit_reason}</td><td>top {t.entry_percentile:.0%}</td></tr>'
    html += '<tr style="background:#f5f5f5"><td colspan="7" style="text-align:center;color:#999">— 最差 —</td></tr>'
    for t in sorted_trades[-10:]:
        html += f'<tr><td>{t.code}</td><td>{t.entry_date}</td><td>{t.exit_date}</td><td class="negative">{t.return_pct:.1f}%</td><td>{t.holding_days}天</td><td>{t.exit_reason}</td><td>top {t.entry_percentile:.0%}</td></tr>'
    html += '</table></div>'

    # 收益分布
    bins = [-20, -10, -5, -2, 0, 2, 5, 10, 15, 20, 50]
    hist = [0]*10
    for r in returns:
        for i in range(10):
            if bins[i] <= r < bins[i+1]:
                hist[i] += 1; break
    max_h = max(hist)
    html += '<div class="card"><h2>📊 收益分布</h2><table>'
    html += '<tr><th>收益区间</th><th>笔数</th><th>占比</th><th>分布</th></tr>'
    labels = ['<-20%','-20~-10%','-10~-5%','-5~-2%','-2~0%','0~+2%','+2~+5%','+5~+10%','+10~+15%','+15~+20%','>+20%']
    for i in range(10):
        pct = hist[i]/max(1,len(returns))*100
        color = "#2e7d32" if bins[i]>=0 else "#d32f2f"
        html += f'<tr><td>{bins[i]:+d}~{bins[i+1]:+d}%</td><td>{hist[i]}</td><td>{pct:.0f}%</td>'
        html += f'<td style="width:200px"><div class="bar"><div style="width:{hist[i]/max(1,max_h)*100}%;background:{color}"></div></div></td></tr>'
    html += '</table></div>'

    html += '</body></html>'

    html_path = os.path.join(output_dir, "v11_backtest_report.html")
    with open(html_path, "w") as f:
        f.write(html)

    elapsed = time.time() - t0
    print(f"\n报告生成完成: {len(all_trades)}笔, 胜率{len(wins)/max(1,len(all_trades)):.1%}, 均{np.mean(returns):+.1f}%")
    print(f"文件: {html_path} ({elapsed:.0f}s)")


if __name__ == "__main__":
    main()
