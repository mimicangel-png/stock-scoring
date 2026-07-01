#!/usr/bin/env python3
"""从已有JSON数据重新生成HTML报告（不需要重新拉取数据）"""
import json, os, sys
from datetime import datetime

# 导入主脚本中的函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scoring_engine import generate_html_report, generate_email_html_report, get_suggestion, get_theme

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")

# 加载JSON结果
json_path = os.path.join(OUTPUT_DIR, "SS增强版评分_2026-06-24.json")
with open(json_path) as f:
    data = json.load(f)

results = data["results"]
today = data["date"]

# 加载历史评分
hist_path = os.path.join(OUTPUT_DIR, "ss_score_history.json")
hist_data = {}
if os.path.exists(hist_path):
    with open(hist_path) as f:
        hist_data = json.load(f)

hist_scores = {}
for code, date_scores in hist_data.items():
    hist_scores[code] = [s for d, s in sorted(date_scores.items())]

# 分类统计
strong_buy = [r for r in results if r["sug_action"] == "strong_buy"]
buy_list = [r for r in results if r["sug_action"] == "buy"]
hold_list = [r for r in results if r["sug_action"] == "hold"]
watch_list = [r for r in results if r["sug_action"] == "watch"]
avoid_list = [r for r in results if r["sug_action"] == "avoid"]
critical = [r for r in avoid_list if r["score"] < 40]
risky = [r for r in avoid_list if r["score"] < 45]

# 板块统计
sectors = {}
for r in results:
    s = r.get("sector", "其他")
    sectors[s] = sectors.get(s, 0) + 1
top_sectors = sorted(sectors.items(), key=lambda x: x[1], reverse=True)[:8]

# 板块分组
sector_groups = {}
for r in results:
    sec = r.get("sector", "其他")
    if sec not in sector_groups:
        sector_groups[sec] = []
    sector_groups[sec].append(r)
sorted_sectors = sorted(sector_groups.items(), key=lambda x: max(r["score"] for r in x[1]), reverse=True)

# 生成交互版HTML（Tab切换，浏览器预览用）
html = generate_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors)
html_path = os.path.join(OUTPUT_DIR, f"SS增强版评分_{today}.html")
with open(html_path, "w") as f:
    f.write(html)
print(f"✅ 交互版HTML报告已重新生成: {html_path}")
print(f"   文件大小: {os.path.getsize(html_path):,} 字节")

# 生成邮件版HTML（全内联样式，无JS，锚点导航+分段表格）
email_html = generate_email_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors)
email_html_path = os.path.join(OUTPUT_DIR, f"SS增强版评分_{today}_email.html")
with open(email_html_path, "w") as f:
    f.write(email_html)
print(f"✅ 邮件版HTML报告已生成: {email_html_path}")
print(f"   文件大小: {os.path.getsize(email_html_path):,} 字节")
