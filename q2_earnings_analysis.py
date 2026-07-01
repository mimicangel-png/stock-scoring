#!/usr/bin/env python3
"""Q2财报分析 V2 - 修正版：扩大数据覆盖，修正东财查询参数"""

import json
import time
import random
import requests
import urllib.request

STOCK_FILE = "/Users/bytedance/WorkBuddy/2026-06-24-18-57-20/stock-scoring/uploaded-stock-codes.txt"
SCORE_FILE = "/Users/bytedance/WorkBuddy/2026-06-24-18-57-20/stock-scoring/output/SS增强版评分_2026-06-25.json"
OUTPUT_FILE = "/Users/bytedance/WorkBuddy/2026-06-24-18-57-20/stock-scoring/output/Q2财报分析_2026-06-25.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
_em_last = [0.0]
EM_MIN = 1.0

# ===== 读取股票池 =====
with open(STOCK_FILE) as f:
    all_codes = [line.strip() for line in f if line.strip()]

with open(SCORE_FILE) as f:
    score_data = json.load(f)

stock_info = {}
for r in score_data["results"]:
    stock_info[r["code"]] = {
        "name": r["name"], "sector": r.get("sector", "其他"),
        "score": r["score"], "price": r["price"],
        "change_pct": r["change_pct"], "ret_5d": r["ret_5d"],
        "ret_20d": r["ret_20d"], "suggestion": r.get("suggestion", ""),
    }

# ===== 1. 腾讯财经批量估值 =====
print(f"[1/4] 腾讯估值: {len(all_codes)} 只...")

def tencent_batch(codes):
    result = {}
    for i in range(0, len(codes), 80):
        batch = codes[i:i+80]
        prefixed = []
        for c in batch:
            if c.startswith(("6", "9")): prefixed.append(f"sh{c}")
            elif c.startswith("8"): prefixed.append(f"bj{c}")
            else: prefixed.append(f"sz{c}")
        url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = resp.read().decode("gbk")
            for line in data.strip().split(";"):
                if "=" not in line or '"' not in line: continue
                vals = line.split('"')[1].split("~")
                if len(vals) < 53: continue
                code = line.split("=")[0].split("_")[-1][2:]
                result[code] = {
                    "name": vals[1], "price": float(vals[3]) if vals[3] else 0,
                    "last_close": float(vals[4]) if vals[4] else 0,
                    "change_pct": float(vals[32]) if vals[32] else 0,
                    "pe_ttm": float(vals[39]) if vals[39] else 0,
                    "pb": float(vals[46]) if vals[46] else 0,
                    "mcap_yi": float(vals[44]) if vals[44] else 0,
                    "turnover_pct": float(vals[38]) if vals[38] else 0,
                    "pe_static": float(vals[52]) if vals[52] else 0,
                }
        except Exception as e:
            print(f"  [WARN] batch fail: {e}")
        time.sleep(0.15)
    return result

valuation = tencent_batch(all_codes)
print(f"  OK: {len(valuation)} 只")

# ===== 2. 东财业绩预告 - 调大范围，多种尝试 =====
print(f"\n[2/4] 东财业绩预告...")

def em_get(url, params=None, headers=None, timeout=15):
    wait = EM_MIN - (time.time() - _em_last[0])
    if wait > 0: time.sleep(wait + random.uniform(0.05, 0.3))
    try:
        r = EM_SESSION.get(url, params=params, headers=headers, timeout=timeout)
        _em_last[0] = time.time()
        return r
    except:
        _em_last[0] = time.time()
        return None

def dc_query(report_name, filter_str="", page_size=1000, sort_columns="", sort_types="-1"):
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get", params=params, timeout=20)
    if r is None: return []
    try:
        d = r.json()
        if d.get("success") and d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
        elif d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
    except: pass
    return []

# 尝试多种查询方式
all_forecasts = []

# 方式1: 用NOTICE_DATE过滤，2026年以来的
print("  方式1: NOTICE_DATE >= 2026-04-01")
f1 = dc_query("RPT_PUBLIC_OP_NEWPREDICT",
    filter_str="(NOTICE_DATE>='2026-04-01')",
    sort_columns="NOTICE_DATE")
print(f"    返回: {len(f1)} 条")
all_forecasts.extend(f1)

# 方式2: 如果方式1为空，试不带filter全量拉第一页
if not f1:
    print("  方式2: 无filter全量")
    f2 = dc_query("RPT_PUBLIC_OP_NEWPREDICT", page_size=500)
    print(f"    返回: {len(f2)} 条")
    all_forecasts.extend(f2)

# 方式3: 试试其他report名称
if not all_forecasts:
    print("  方式3: 尝试 RPT_PERFORMANCEFORECAST")
    f3 = dc_query("RPT_PERFORMANCEFORECAST", page_size=500)
    print(f"    返回: {len(f3)} 条")
    all_forecasts.extend(f3)

# 方式4: 换个日期格式（不带横线）
if not all_forecasts:
    print("  方式4: 试试不同日期格式")
    f4 = dc_query("RPT_PUBLIC_OP_NEWPREDICT",
        filter_str="(NOTICE_DATE>='20260401')",
        sort_columns="NOTICE_DATE")
    print(f"    返回: {len(f4)} 条")
    all_forecasts.extend(f4)

# 方式5: 试RPT_FORECAST
if not all_forecasts:
    print("  方式5: RPT_FORECAST")
    f5 = dc_query("RPT_FORECAST", page_size=500)
    print(f"    返回: {len(f5)} 条")
    all_forecasts.extend(f5)

# 方式6: 试 RPT_PERFORMANCEFORECAST_STA
if not all_forecasts:
    print("  方式6: RPT_PERFORMANCEFORECAST_STA")
    f6 = dc_query("RPT_PERFORMANCEFORECAST_STA", page_size=500)
    print(f"    返回: {len(f6)} 条")
    all_forecasts.extend(f6)

# 过滤股票池中的
pool_forecasts = {}
for row in all_forecasts:
    code = str(row.get("SECURITY_CODE", ""))
    if code in stock_info:
        if code not in pool_forecasts:
            pool_forecasts[code] = row

print(f"  股票池中有Q2预告: {len(pool_forecasts)} 只")

# 打印字段名帮助调试
if all_forecasts:
    sample = all_forecasts[0]
    print(f"  预告字段样例: {list(sample.keys())[:15]}")

# ===== 3. 新浪 Q1 财报 - 扩大覆盖 =====
print(f"\n[3/4] 新浪Q1财报 - 拉取所有137只...")

def sina_lrb(code):
    prefix = "sh" if code.startswith("6") else "sz"
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    params = {"paperCode": f"{prefix}{code}", "source": "lrb", "type": "0", "page": "1", "num": "3"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=10)
        report_list = r.json().get("result", {}).get("data", {}).get("report_list", {}) or {}
        rows = []
        for period in sorted(report_list.keys(), reverse=True)[:3]:
            obj = report_list[period]
            rec = {"报告期": f"{period[:4]}-{period[4:6]}-{period[6:8]}"}
            for it in obj.get("data", []) or []:
                title = it.get("item_title", "")
                if not title or it.get("item_value") is None: continue
                rec[title] = it.get("item_value")
                tongbi = it.get("item_tongbi")
                if tongbi not in (None, ""): rec[title + "_同比"] = tongbi
            rows.append(rec)
        return rows
    except:
        return []

lrb_data = {}
for idx, code in enumerate(all_codes):
    wait = 0.3 - (time.time() - _em_last[0])
    if wait > 0: time.sleep(wait)
    try:
        lrb = sina_lrb(code)
        if lrb: lrb_data[code] = lrb
    except: pass
    _em_last[0] = time.time()
    if (idx + 1) % 30 == 0:
        print(f"  {idx+1}/{len(all_codes)}... OK={len(lrb_data)}")

print(f"  Q1财报覆盖: {len(lrb_data)} 只")

# ===== 4. 组装数据 =====
print(f"\n[4/4] 组装分析结果...")

results = []
for code in sorted(all_codes):
    info = stock_info.get(code, {})
    val = valuation.get(code, {})
    lrb = lrb_data.get(code, [])
    fc = pool_forecasts.get(code)

    pe_ttm = val.get("pe_ttm", 0)
    pb = val.get("pb", 0)
    mcap = val.get("mcap_yi", 0)
    price = val.get("price", 0)

    # Q1数据
    q1_net_profit = None
    q1_revenue = None
    q1_rev_yoy = None
    q1_profit_yoy = None
    for report in lrb:
        if "03-31" in report.get("报告期", ""):
            try:
                q1_revenue = float(report.get("营业收入", 0)) if report.get("营业收入") else None
                q1_net_profit = float(report.get("净利润", 0)) if report.get("净利润") else None
                q1_rev_yoy = report.get("营业收入_同比", None)
                q1_profit_yoy = report.get("净利润_同比", None)
            except: pass
            break

    # 业绩预告
    fc_type = ""
    fc_summary = ""
    profit_change = ""
    if fc:
        fc_type = str(fc.get("FORECAST_TYPE", fc.get("FORECASTTYPE", fc.get("PREDICT_TYPE", ""))))
        fc_summary = str(fc.get("FORECAST_CONTENT", fc.get("FORECASTCONTENT", fc.get("FORECAST_SUMMARY", ""))))[:200]
        # 尝试获取净利润变化
        ratio = fc.get("PROFIT_RANGE_RATIO", fc.get("CHANGE_RATIO", None))
        if ratio is not None and ratio != 0:
            profit_change = f"{ratio:.1f}%" if isinstance(ratio, (int, float)) else str(ratio)

    # 估值分档
    if pe_ttm > 0:
        if pe_ttm < 20: pe_health = "低估"
        elif pe_ttm < 40: pe_health = "合理"
        elif pe_ttm < 80: pe_health = "偏高"
        elif pe_ttm < 200: pe_health = "高估"
        else: pe_health = "极高"
    else: pe_health = "亏损"

    results.append({
        "code": code,
        "name": info.get("name", val.get("name", "")),
        "sector": info.get("sector", "其他"),
        "ss_score": info.get("score", 0),
        "price": price,
        "change_pct": info.get("change_pct", 0),
        "ret_5d": info.get("ret_5d", 0),
        "ret_20d": info.get("ret_20d", 0),
        "pe_ttm": pe_ttm,
        "pb": pb,
        "mcap_yi": mcap,
        "pe_health": pe_health,
        "turnover_pct": val.get("turnover_pct", 0),
        "q1_revenue": q1_revenue,
        "q1_net_profit": q1_net_profit,
        "q1_rev_yoy": q1_rev_yoy,
        "q1_profit_yoy": q1_profit_yoy,
        "q2_forecast_type": fc_type,
        "q2_profit_change": profit_change,
        "q2_forecast_summary": fc_summary,
        "has_q2_forecast": "有" if fc else "无",
        "has_q1_data": "有" if q1_net_profit is not None else "无",
    })

# ===== 5. 按板块分组 =====
sector_groups = {}
for r in results:
    sec = r["sector"]
    if sec not in sector_groups: sector_groups[sec] = []
    sector_groups[sec].append(r)

sector_summary = {}
for sec, stocks in sector_groups.items():
    scores = [s["ss_score"] for s in stocks]
    pes = [s["pe_ttm"] for s in stocks if s["pe_ttm"] > 0]
    pbs = [s["pb"] for s in stocks if s["pb"] > 0]
    with_fc = sum(1 for s in stocks if s["has_q2_forecast"] == "有")
    with_q1 = sum(1 for s in stocks if s["has_q1_data"] == "有")
    pos_5d = sum(1 for s in stocks if s["ret_5d"] > 0)

    # 盈利增长情况统计
    q1_growth = []
    for s in stocks:
        if s.get("q1_profit_yoy") and s["q1_profit_yoy"] not in (None, "", "None"):
            try: q1_growth.append(float(s["q1_profit_yoy"]))
            except: pass

    sector_summary[sec] = {
        "count": len(stocks),
        "avg_score": round(sum(scores) / len(scores), 1),
        "avg_pe": round(sum(pes) / len(pes), 1) if pes else 0,
        "avg_pb": round(sum(pbs) / len(pbs), 2) if pbs else 0,
        "with_forecast": with_fc,
        "with_q1": with_q1,
        "q1_coverage_pct": round(with_q1 / len(stocks) * 100, 0),
        "pos_5d_pct": round(pos_5d / len(stocks) * 100, 1),
        "avg_q1_profit_growth": round(sum(q1_growth) / len(q1_growth), 1) if q1_growth else None,
        "stocks": sorted(stocks, key=lambda x: x["ss_score"], reverse=True),
    }

output = {
    "date": "2026-06-25",
    "total": len(results),
    "sector_summary": sector_summary,
    "results": results,
}

with open(OUTPUT_FILE, "w") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\n[完成] {OUTPUT_FILE}")
print(f"  总股票: {len(results)}")
print(f"  Q2预告: {sum(1 for r in results if r['has_q2_forecast'] == '有')} 只")
print(f"  Q1数据: {sum(1 for r in results if r['has_q1_data'] == '有')} 只")
print(f"  板块: {len(sector_groups)}")
for sec, sm in sorted(sector_summary.items()):
    print(f"  {sec}: {sm['count']}只, 均分{sm['avg_score']}, 均PE{sm['avg_pe']}, Q1覆盖{sm['q1_coverage_pct']:.0f}%")
