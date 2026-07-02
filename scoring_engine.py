#!/usr/bin/env python3
"""
SS with GLM 评分引擎 + 盘中建议引擎 + 邮件报告

=== 每日评分 (SS with GLM V8) ===
- 运行时间: 15:30 收盘后
- 权重: 技术40% + 资金40% + 信息20%
- 数据: EOD K线 + 事件公告 + 主力资金 + 风险因子
- 推荐: ≥75强烈买入 · ≥70逢低买入 · ≥60持有 · ≥45观望 · <45回避

=== 盘中建议 (独立产品线) ===
- 运行时间: 14:30 盘中
- 权重: 技术25% + 资金35% + 信息15% + 盘中动量25%
- 数据: 实时行情 + K线 + 主力资金T-1 + 日内形态
- 推荐: ≥72强烈买入 · ≥67逢低买入 · ≥57持有 · ≥42观望 · <42回避
- 关键特征: 动量因子经回测反转 — 追高信号扣分，回调信号加分
"""
import json, math, urllib.request, sys, os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 加载 .env 配置（避免硬编码敏感信息）==========
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
_load_env()

# ========== 本地数据库缓存（可选，自动检测）==========
try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       ".workbuddy/skills/stock-db"))
    from stock_db import StockDB
    _db = StockDB()
    _USE_DB = True
except Exception:
    _USE_DB = False

# ========== 数据获取 ==========
def fetch_kline_batch(codes, days=130):
    """并发获取K线（ThreadPoolExecutor, 20 workers），返回 {code: [kline_dicts]}"""
    if _USE_DB:
        return _db.get_klines(codes, days)
    all_kline = {}
    total = len(codes)

    def fetch_one(code):
        if code.startswith(("6", "9")): sym = f"sh{code}"
        else: sym = f"sz{code}"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode("utf-8"))
            klines = data.get("data", {}).get(sym, {}).get("qfqday", []) or \
                     data.get("data", {}).get(sym, {}).get("day", [])
            if klines:
                return code, [{'date':k[0],'open':float(k[1]),'close':float(k[2]),
                    'high':float(k[3]),'low':float(k[4]),'volume':float(k[5]) if len(k)>5 else 0} for k in klines]
        except: pass
        return code, None

    completed = 0
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_one, c): c for c in codes}
        for f in as_completed(futures):
            code, kl = f.result()
            if kl: all_kline[code] = kl
            completed += 1
            if completed % 50 == 0:
                print(f"  K线: {completed}/{total}")

    return all_kline

def fetch_extra_info(codes):
    if _USE_DB:
        return _db.get_extra_info(codes)
    prefixed = [f"sh{c}" if c.startswith(("6","9")) else f"sz{c}" for c in codes]
    info = {}
    for i in range(0, len(prefixed), 60):
        batch = prefixed[i:i+60]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            for line in resp.read().decode("gbk").strip().split(";"):
                if "=" not in line or '"' not in line: continue
                vals = line.split('"')[1].split("~")
                if len(vals) < 55: continue
                code = line.split("=")[0].split("_")[-1][2:]
                info[code] = {"name":vals[1],"price":float(vals[3]) if vals[3] else 0,
                    "change_pct":float(vals[32]) if vals[32] else 0,
                    "pe_ttm":float(vals[39]) if vals[39] else 0,
                    "pb":float(vals[46]) if vals[46] else 0,
                    "mcap":float(vals[44])*1e8 if vals[44] else 0,  # QQ接口返回亿元，转为元
                    "turnover":float(vals[38]) if vals[38] else 0,
                    "vol_ratio":float(vals[49]) if vals[49] else 0}
        except: pass
    return info

# ========== 事件驱动：公告与新闻抓取 ==========
EVENT_RULES = [
    # 格式: (事件类型, [关键词], 基础分)
    # 负面事件优先匹配（短期冲击大、需要被看见）
    ("减持", ["减持", "减持计划", "减持股份"], -15),
    ("预减", ["预减", "业绩预减", "亏损", "净利润下降", "营收下降", "业绩下滑"], -18),
    ("解禁", ["解禁", "限售股解禁", "限售股上市"], -10),
    ("增发", ["增发", "配股", "可转债", "定增"], -6),
    ("关联交易", ["关联交易"], -3),
    # 正面事件
    ("预增", ["预增", "扭亏为盈", "业绩增长", "净利润增长", "营收增长", "同比大增", "大幅盈利"], 20),
    ("重大合同", ["重大合同", "中标", "签订", "框架协议", "战略合作协议", "采购合同"], 15),
    ("增持", ["增持", "增持计划", "增持股份"], 12),
    ("回购", ["回购", "股份回购", "回购股份"], 10),
    ("扩产", ["扩产", "产能扩张", "投产", "新建项目", "扩建", "产能释放", "产线建设"], 8),
    ("股权激励", ["股权激励", "员工持股计划", "限制性股票"], 6),
    ("分红", ["分红", "权益分派", "派息", "高送转", "转增"], 5),
]

def classify_event(title):
    """根据公告标题识别事件类型和基础分"""
    t = title.lower()
    for event_type, keywords, score in EVENT_RULES:
        for kw in keywords:
            if kw in t:
                return event_type, score
    return None, 0

def calc_event_score(events, today_str, max_lookback=10):
    """
    对近 max_lookback 天内的事件进行衰减评分。
    同一类型只取最强影响；不同类型可叠加。
    返回: (score_delta, {event_type: (delta, title, date)})
    """
    if not events:
        return 0, {}
    today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    best_by_type = {}
    for e in events:
        edate = e.get("date", "")
        if not edate: continue
        try:
            edt = datetime.strptime(edate, "%Y-%m-%d")
        except ValueError:
            continue
        days = (today_dt - edt).days
        if days < 0 or days > max_lookback:
            continue
        event_type = e.get("event_type")
        base = e.get("base_score", 0)
        if not event_type or base == 0:
            continue
        factor = max(0, 1 - days / max_lookback)
        adj = round(base * factor)
        if adj == 0: continue  # 衰减后无影响，跳过
        if event_type not in best_by_type or abs(adj) > abs(best_by_type[event_type][0]):
            best_by_type[event_type] = (adj, e.get("title", ""), edate)
    total = sum(v[0] for v in best_by_type.values())
    # 信息面事件冲击上下限
    total = max(-25, min(25, total))
    return total, best_by_type

def fetch_news_events(codes, lookback_days=14, limit=20):
    """使用 westock-data 批量获取公告，返回 {code: [{title, date, event_type, base_score}, ...]}。"""
    if _USE_DB:
        return _db.get_events(codes, days=lookback_days)
    import subprocess
    script = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
    events = {}
    today = datetime.now().strftime("%Y-%m-%d")
    today_dt = datetime.strptime(today, "%Y-%m-%d")

    def to_symbol(c):
        return f"sh{c}" if c.startswith(("6", "9")) else f"sz{c}"

    batch_size = 30
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        symbols = ",".join(to_symbol(c) for c in batch)
        cmd = ["node", script, "notice", "list", symbols, "--limit", str(limit), "--raw"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode != 0:
                print(f"  [WARN] 事件获取失败: {res.stderr[:200]}")
                continue
            data = json.loads(res.stdout)
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and "sections" in data:
                for sec in data["sections"]:
                    if isinstance(sec, list):
                        items.extend(sec)
            for item in items:
                symbol = item.get("symbol", "")
                if not symbol or len(symbol) < 6: continue
                code = symbol[2:]
                title = item.get("title", "")
                ts = item.get("time", "")
                if not title or not ts: continue
                date = ts.split()[0]
                # 只保留 lookback 天内
                try:
                    edt = datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    continue
                if (today_dt - edt).days > lookback_days:
                    continue
                event_type, base_score = classify_event(title)
                if code not in events: events[code] = []
                events[code].append({
                    "title": title,
                    "date": date,
                    "event_type": event_type,
                    "base_score": base_score
                })
        except Exception as e:
            print(f"  [WARN] 事件获取异常: {e}")
        if (i+batch_size) % 50 == 0 or i+batch_size >= len(codes):
            print(f"  事件: {min(i+batch_size, len(codes))}/{len(codes)}")
    return events

# ========== 大盘环境 &amp; 板块强度 ==========

def fetch_sector_context():
    """
    拉取 sector ranking 数据，返回：
      market_regime: {"regime": "bullish/neutral/bearish", "market_score_delta": ±3}
      sector_strength: {"半导体/芯片": +5, "新能源/电力": -3, ...} (每个板块的强度分)
    """
    import subprocess
    script = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
    cmd = ["node", script, "sector", "ranking", "--raw"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return {"regime": "neutral", "market_score_delta": 0}, {}
        data = json.loads(res.stdout)
    except:
        return {"regime": "neutral", "market_score_delta": 0}, {}

    sections = data.get("sections", [])
    if not sections:
        return {"regime": "neutral", "market_score_delta": 0}, {}

    # ==== Layer 1: 大盘环境 ====
    # 用前10个行业的平均涨幅判断市场强弱
    if len(sections) >= 1 and sections[0]:
        top10_changes = []
        for s in sections[0][:10]:
            try:
                top10_changes.append(float(s.get("changePct", 0)))
            except: pass
        if top10_changes:
            avg_change = sum(top10_changes) / len(top10_changes)
            if avg_change > 2:
                market_regime = {"regime": "bullish", "market_score_delta": 3}
            elif avg_change < -2:
                market_regime = {"regime": "bearish", "market_score_delta": -3}
            else:
                market_regime = {"regime": "neutral", "market_score_delta": 0}
        else:
            market_regime = {"regime": "neutral", "market_score_delta": 0}
    else:
        market_regime = {"regime": "neutral", "market_score_delta": 0}

    # ==== Layer 2: 板块相对强度 ====
    # 申万一级行业名 → 涨跌幅映射
    industry_change = {}
    if len(sections) >= 1:
        for s in sections[0]:
            try:
                industry_change[s.get("name", "")] = float(s.get("changePct", 0))
            except: pass

    # 申万行业 → 我们的主题板块 映射（用于把行业涨幅映射到主题）
    INDUSTRY_MAP = {
        "半导体": "半导体/芯片", "元件": "半导体/芯片", "电子化学品": "半导体/芯片",
        "光学光电子": "半导体/芯片", "消费电子": "电子/消费电子",
        "计算机设备": "AI/算力/通信", "IT服务": "AI/算力/通信", "软件开发": "AI/算力/通信",
        "通信设备": "AI/算力/通信", "通信服务": "AI/算力/通信",
        "电力设备": "新能源/电力", "电网设备": "新能源/电力", "光伏设备": "新能源/电力",
        "风电设备": "新能源/电力", "电池": "新能源/电力", "电力": "新能源/电力",
        "煤炭开采": "新能源/电力", "石油石化": "新能源/电力", "环保": "新能源/电力",
        "油气开采": "新能源/电力", "公用事业": "新能源/电力",
        "传媒": "传媒/游戏", "游戏": "传媒/游戏", "影视院线": "传媒/游戏",
        "广告营销": "传媒/游戏", "出版": "传媒/游戏",
        "自动化设备": "智能制造", "通用设备": "智能制造", "专用设备": "智能制造",
        "汽车零部件": "智能制造", "汽车整车": "智能制造", "军工电子": "智能制造",
        "航空装备": "智能制造", "航海装备": "智能制造", "地面兵装": "智能制造",
        "工程机械": "智能制造", "轨交设备": "智能制造",
        "化学制药": "医药生物", "生物制品": "医药生物", "医疗器械": "医药生物",
        "医药商业": "医药生物", "中药": "医药生物", "医疗服务": "医药生物",
        "化学制品": "化工/新材料", "化学原料": "化工/新材料", "塑料": "化工/新材料",
        "橡胶": "化工/新材料", "非金属材料": "化工/新材料", "金属新材料": "化工/新材料",
        "建筑材料": "化工/新材料", "装修建材": "化工/新材料", "钢铁": "化工/新材料",
        "有色金属": "化工/新材料", "工业金属": "化工/新材料", "能源金属": "化工/新材料",
        "小金属": "化工/新材料", "贵金属": "化工/新材料",
        "饮料制造": "消费", "食品加工": "消费", "白色家电": "消费",
        "黑色家电": "消费", "小家电": "消费", "厨卫电器": "消费", "照明设备": "消费",
        "服装家纺": "消费", "纺织制造": "消费", "饰品": "消费",
        "造纸": "消费", "包装印刷": "消费", "文娱用品": "消费",
        "家居用品": "消费", "美容护理": "消费", "旅游零售": "消费",
        "酒店餐饮": "消费", "旅游及景区": "消费", "体育": "消费",
        "教育": "消费", "专业服务": "消费",
        "银行": "金融/交通/基建", "证券": "金融/交通/基建", "保险": "金融/交通/基建",
        "多元金融": "金融/交通/基建",
        "铁路公路": "金融/交通/基建", "航运港口": "金融/交通/基建",
        "航空机场": "金融/交通/基建", "物流": "金融/交通/基建",
        "建筑装饰": "金融/交通/基建", "房地产": "金融/交通/基建",
        "房地产开发": "金融/交通/基建", "房地产服务": "金融/交通/基建",
    }

    # 按主题板块聚合涨跌幅
    theme_change = {}
    theme_count = {}
    for ind_name, change in industry_change.items():
        theme = INDUSTRY_MAP.get(ind_name)
        if theme:
            if theme not in theme_change:
                theme_change[theme] = 0
                theme_count[theme] = 0
            theme_change[theme] += change
            theme_count[theme] += 1
        # 模糊匹配：如果行业名包含主题关键词
        elif "半导体" in ind_name or "芯片" in ind_name or "PCB" in ind_name:
            theme = "半导体/芯片"
        elif "通信" in ind_name:
            theme = "AI/算力/通信"
        elif "计算机" in ind_name or "软件" in ind_name:
            theme = "AI/算力/通信"
        elif "电力" in ind_name or "能源" in ind_name:
            theme = "新能源/电力"
        elif "传媒" in ind_name or "游戏" in ind_name:
            theme = "传媒/游戏"
        elif "汽车" in ind_name or "机械" in ind_name:
            theme = "智能制造"
        elif "医药" in ind_name or "医疗" in ind_name:
            theme = "医药生物"
        elif "化工" in ind_name or "材料" in ind_name:
            theme = "化工/新材料"
        elif "银行" in ind_name or "证券" in ind_name or "保险" in ind_name:
            theme = "金融/交通/基建"
        elif "食品" in ind_name or "饮料" in ind_name or "家电" in ind_name:
            theme = "消费"
        else:
            continue
        if theme not in theme_change:
            theme_change[theme] = 0
            theme_count[theme] = 0
        theme_change[theme] += change
        theme_count[theme] += 1

    # 计算每个主题板块的平均涨跌幅
    sector_strength = {}
    for theme in theme_change:
        if theme_count[theme] > 0:
            avg = theme_change[theme] / theme_count[theme]
            # V6修正：收紧板块领涨阈值（回测显示+1分太弱），只给最强板块加分
            if avg > 3:
                sector_strength[theme] = 5
            elif avg > 1.5:
                sector_strength[theme] = 3
            elif avg > 0:
                sector_strength[theme] = 0  # 微涨不加分
            elif avg > -2:
                sector_strength[theme] = 0
            elif avg > -5:
                sector_strength[theme] = -3
            else:
                sector_strength[theme] = -5

    return market_regime, sector_strength

# ========== 主力资金净流向 ==========

def fetch_fund_flow(codes):
    """批量获取主力资金净流向，返回 {code: {main_net_5d, main_net_20d, inflow_rate, ...}}"""
    if _USE_DB:
        return _db.get_fund_flows(codes)
    import subprocess
    script = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
    results = {}

    def to_symbol(c):
        return f"sh{c}" if c.startswith(("6", "9")) else f"sz{c}"

    batch_size = 30
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        symbols = ",".join(to_symbol(c) for c in batch)
        cmd = ["node", script, "fund", "flow", symbols, "--raw"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode != 0:
                continue
            data = json.loads(res.stdout)
            if not isinstance(data, list):
                continue
            for item in data:
                sym = item.get("symbol", "")
                if not sym or len(sym) < 6:
                    continue
                code = sym[2:]
                try:
                    main_5d = float(item.get("MainNetFlow5D", 0))
                    main_20d = float(item.get("MainNetFlow20D", 0))
                    inflow_rate = float(item.get("MainInflowCircRate", 0))
                    jumbo = float(item.get("JumboNetFlow", 0))
                    main_today = float(item.get("MainNetFlow", 0))
                except (ValueError, TypeError):
                    continue
                results[code] = {
                    "main_net_5d": main_5d,
                    "main_net_20d": main_20d,
                    "inflow_rate": inflow_rate,
                    "jumbo_net": jumbo,
                    "main_net_today": main_today,
                }
        except Exception as e:
            print(f"  [WARN] 资金流获取异常: {e}")
        if (i+batch_size) % 50 == 0 or i+batch_size >= len(codes):
            print(f"  资金流: {min(i+batch_size, len(codes))}/{len(codes)}")
    return results

# ========== 风险因子：质押/解禁/商誉 ==========

def fetch_risk_factors(codes):
    """批量获取风险因子，返回 {code: {pledge_ratio, unlock_days, ...}}"""
    import subprocess
    script = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
    results = {}

    def to_symbol(c):
        return f"sh{c}" if c.startswith(("6", "9")) else f"sz{c}"

    batch_size = 30
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        symbols = ",".join(to_symbol(c) for c in batch)
        cmd = ["node", script, "risk", symbols, "--types", "pledge,unlock", "--raw"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode != 0: continue
            data = json.loads(res.stdout)
            items = data if isinstance(data, list) else data.get("data", [])
            if isinstance(items, list):
                for item in items:
                    sym = item.get("symbol", "")
                    if not sym or len(sym) < 6: continue
                    code = sym[2:]
                    if code not in results:
                        results[code] = {"pledge": 0, "unlock_days": 999}
                    pledge = item.get("pledgeRatio") or item.get("pledge_ratio") or 0
                    try: pledge = float(pledge)
                    except: pledge = 0
                    if pledge > results[code].get("pledge", 0):
                        results[code]["pledge"] = pledge
                    unlock_date = item.get("unlockDate") or item.get("unlock_date") or ""
                    if unlock_date:
                        try:
                            from datetime import datetime
                            ud = datetime.strptime(str(unlock_date)[:10], "%Y-%m-%d")
                            days = (ud - datetime.now()).days
                            if 0 <= days < results[code].get("unlock_days", 999):
                                results[code]["unlock_days"] = days
                        except: pass
        except: pass
        if (i+batch_size) % 50 == 0 or i+batch_size >= len(codes):
            print(f"  风险因子: {min(i+batch_size, len(codes))}/{len(codes)}")
    return results

# ========== 融资融券数据 ==========

def fetch_margin_data(codes):
    """获取融资余额日环比变化，返回 {code: {margin_dod, margin_total}}"""
    import subprocess
    script = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
    results = {}

    def to_symbol(c):
        return f"sh{c}" if c.startswith(("6", "9")) else f"sz{c}"

    for i, code in enumerate(codes):
        sym = to_symbol(code)
        cmd = ["node", script, "fund", "margin", sym, "--raw"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode != 0: continue
            data = json.loads(res.stdout)
            item = data.get("data", {})
            if not item: continue
            try:
                margin_dod = float(item.get("FinanceValueDOD", 0))
                margin_total = float(item.get("FinanceValue", 0))
            except (ValueError, TypeError):
                continue
            results[code] = {"margin_dod": margin_dod, "margin_total": margin_total}
        except: pass
        if (i+1) % 50 == 0:
            print(f"  融资融券: {i+1}/{len(codes)}")
    return results


def calc_ma(p,n):
    if len(p)<n: return None
    return sum(p[-n:])/n
def calc_ema(p,n):
    if len(p)<n: return None
    k=2/(n+1); ema=p[-n]
    for v in p[-n+1:]: ema=v*k+ema*(1-k)
    return ema
def calc_rsi(c,n=14):
    if len(c)<n+1: return 50
    g=sum(max(0,c[i]-c[i-1]) for i in range(-n,0))
    l=sum(max(0,c[i-1]-c[i]) for i in range(-n,0))
    if l==0: return 100
    return 100-100/(1+g/l)

# ========== SS with GLM 评分 ==========
def score_ss_enhanced(klines, idx, event_list=None, today_str=None, extra=None,
                      market_regime=None, sector_strength=None, fund_flow=None,
                      margin_data=None):
    """SS with GLM 评分引擎，返回评分 + 各维度分值 + 加减分因子明细 + 事件摘要"""
    if idx < 60: return None
    w = klines[:idx+1]
    c=[k['close'] for k in w]; v=[k['volume'] for k in w]
    h=[k['high'] for k in w]; l=[k['low'] for k in w]; o=[k['open'] for k in w]
    tech=50; capital=50; info=50
    factors = []  # [{"dim":"技术面","name":"均线多头排列","delta":"+15","detail":"MA5>MA10>MA20"}]

    def add(dim, name, delta, detail=""):
        """记录一个加减分因子"""
        factors.append({"dim":dim,"name":name,"delta":delta,"detail":detail})

    # ---- 技术面 ----
    ma5,ma10,ma20=calc_ma(c,5),calc_ma(c,10),calc_ma(c,20)
    rsi=calc_rsi(c)
    if ma5 and ma10 and ma20:
        if ma5>ma10>ma20:
            tech+=15
            add("技术面","均线多头排列",15,f"MA5({ma5:.2f})>MA10({ma10:.2f})>MA20({ma20:.2f})")
        elif ma5<ma10<ma20:
            tech-=10
            add("技术面","均线空头排列",-10,f"MA5({ma5:.2f})<MA10({ma10:.2f})<MA20({ma20:.2f})")
    dif=calc_ema(c,12); dea=calc_ema(c,26)
    # V7：MACD已与均线多头高度重叠(Jaccard 0.64)，降权
    if dif and dea and dif>dea and dif>0:
        if ma5 and ma10 and ma20 and ma5>ma10>ma20:
            tech += 2  # 均线多头已+15，MACD仅象征性加分
        else:
            tech += 5
            add("技术面","MACD金叉且多头",5,f"DIF({dif:.2f})>DEA({dea:.2f}),DIF>0")
    elif dif and dif<0:
        tech-=3
        add("技术面","MACD空头区域",-3,f"DIF({dif:.2f})<0")
    if 40<=rsi<=55:
        # V6修正：回测显示RSI 40-55是负超额(-1.03%)，不是买点
        tech -= 3
        add("技术面","RSI弱势区",-3,f"RSI={rsi:.1f}(40-55),回测跑输")
    elif 55<rsi<=70:
        # V7：RSI 55-70回测超额-1.01%，不再加分
        pass
    # V7: RSI极端(>80)与RSI强动量(>75)合并，只取最强
    if rsi>80:
        tech += 12
        add("技术面","RSI极端强势",12,f"RSI={rsi:.1f}(>80),动量延续")
    elif rsi>75:
        tech += 10
        add("技术面","RSI强动量区",10,f"RSI={rsi:.1f}(>75)")
    elif rsi<30:
        tech -= 8
        add("技术面","RSI超卖区",-8,f"RSI={rsi:.1f}(<30)")
    av5=sum(v[-6:-1])/5 if len(v)>=6 else v[-1]
    vr5=v[-1]/av5 if av5>0 else 1
    if c[-1]>c[-2] and vr5>1.5:
        tech+=8
        add("技术面","放量上涨",8,f"收盘>前收且量比5日={vr5:.2f}")
    elif c[-1]<c[-2]:
        # V3回测(120天): 缩量下跌=陷阱, 适度放量回调=洗盘, 巨量下跌=出货
        if vr5<0.7:
            tech-=10
            add("技术面","缩量下跌(陷阱)",-10,f"收盘<前收,量比5日={vr5:.2f}<0.7,回测显示跑输基线")
        elif 1.1<=vr5<=1.5:
            tech+=5
            add("技术面","放量回调(洗盘)",5,f"收盘<前收,量比5日={vr5:.2f},回测显示20日超额+5.8%")
        elif vr5>1.5:
            tech-=8
            add("技术面","巨量下跌(出货)",-8,f"收盘<前收且量比5日={vr5:.2f}>1.5")
    if ma20:
        dev=(c[-1]-ma20)/ma20*100
        if 2<dev<8:
            tech+=8
            add("技术面","适度偏离MA20",8,f"偏离MA20 +{dev:.1f}%")
        elif dev>15:
            tech-=8
            add("技术面","过度偏离MA20",-8,f"偏离MA20 +{dev:.1f}%")
        elif -5<dev<-2:
            # V6修正：回测显示回调至MA20是-2.55%超额，不是买点
            tech -= 5
            add("技术面","MA20下方弱势",-5,f"偏离MA20 {dev:.1f}%,回测跑输")
    if len(c)>=120:
        h52,l52=max(h[-120:]),min(l[-120:])
        pct52=(c[-1]-l52)/(h52-l52)*100 if h52>l52 else 50
        if pct52<30:
            tech-=8
            add("技术面","52周低位区",-8,f"52周位置{pct52:.0f}%,T+10回测夏普负胜率45%")
        elif pct52>90:
            tech-=5
            add("技术面","52周高位区",-5,f"52周位置{pct52:.0f}%")
    # V6修正：RSI>80封顶和MA5超涨封顶已移到上方动量加分，不再压制
    tech=max(5,min(95,int(tech)))

    # ---- 资金面 ----
    tp=[(hi+lo+cl)/3 for hi,lo,cl in zip(h,l,c)]
    pf=nf=0
    for i in range(-14,0):
        mf=tp[i]*v[i]
        if tp[i]>tp[i-1]: pf+=mf
        else: nf+=mf
    mfi=100-100/(1+pf/nf) if nf>0 else 50
    # 先算 CMF（在MFI判断前）
    mf_vol=[]
    for i in range(-20,0):
        if h[i]==l[i]: mf=0
        else: mf=((c[i]-l[i])-(h[i]-c[i]))/(h[i]-l[i])
        mf_vol.append(mf*v[i])
    cmf=sum(mf_vol)/sum(v[-20:]) if sum(v[-20:])>0 else 0
    if cmf>0.15:
        capital+=12
        add("资金面","CMF资金净流入",12,f"CMF={cmf:.2f}(>0.15)")
    elif cmf<-0.15:
        capital-=10
        add("资金面","CMF资金净流出",-10,f"CMF={cmf:.2f}(<-0.15)")

    if mfi<40:
        capital-=5
        add("资金面","MFI资金流出",-5,f"MFI={mfi:.1f}(<40)")

    # V7：MFI流入删除（回测-0.66%超额，且与CMF重叠）
    tv=sum(v[-20:])
    if tv>0: vwap20=sum(c[i]*v[i] for i in range(-20,0))/tv
    else: vwap20=c[-1]
    if c[-1]>vwap20*1.03:
        # V7：突破VWAP20与均线多头重叠(Jaccard 0.79)，降权；独立触发时给足
        if ma5 and ma10 and ma20 and ma5>ma10>ma20:
            capital += 1  # 均线多头已奖励，仅象征加分
        else:
            capital += 3
            add("资金面","突破VWAP20",3,f"收盘{c[-1]:.2f}>VWAP20 {vwap20:.2f}×1.03")
    if len(v)>=5:
        vt=sum(1 for i in range(-5,0) if v[i]>v[i-1])
        if vt>=4:
            capital+=10
            add("资金面","持续放量",10,f"近5日中{vt}天放量")
    if h[-1]>l[-1]:
        cs=(c[-1]-l[-1])/(h[-1]-l[-1])*100
        # V7：收盘强势+放量回测-0.34%，删除

    # ==== V4 新增：换手率异常（基于成交量）====
    if len(v) >= 21:
        v20_avg = sum(v[-21:-1]) / 20
        if v20_avg > 0:
            v20_ratio = v[-1] / v20_avg
            if v20_ratio > 3:
                if c[-1] > c[-2]:
                    capital -= 5
                    add("资金面","巨量突破",-5,f"量比20日={v20_ratio:.1f}(>3)收阳,T+10夏普0.032/VaR5%-23.8%")
                else:
                    capital -= 6
                    add("资金面","巨量出货",-6,f"量比20日={v20_ratio:.1f}(>3)收阴,资金撤离")
            elif v20_ratio > 2 and c[-1] > c[-2]:
                capital += 4
                add("资金面","显著放量上涨",4,f"量比20日={v20_ratio:.1f}(>2)收阳")

    # ==== V4 新增：振幅异常 ====
    if len(c) >= 21:
        ampl_today = (h[-1] - l[-1]) / c[-1] * 100
        ampl_hist = []
        for i in range(-21, -1):
            if h[i] != l[i] and c[i] != 0:
                ampl_hist.append((h[i] - l[i]) / c[i] * 100)
        if ampl_hist:
            ampl_avg = sum(ampl_hist) / len(ampl_hist)
            if ampl_today > ampl_avg * 2 and ampl_today > 5:
                if c[-1] > o[-1]:
                    capital += 5
                    add("资金面","高振幅收阳",5,f"振幅{ampl_today:.1f}%(>均{ampl_avg:.1f}%×2),博弈中多头占优")
                else:
                    capital -= 5
                    add("资金面","高振幅收阴",-5,f"振幅{ampl_today:.1f}%(>均{ampl_avg:.1f}%×2),博弈中空头占优")

    # ==== V4 新增：市值分层 ====
    if extra:
        mcap = extra.get("mcap", 0)
        if mcap > 100000000000:  # >1000亿
            capital += 8
            add("资金面","大市值稳定性",8,f"市值{mcap/1e8:.0f}亿,T+10夏普0.365第二高")
        elif mcap < 5000000000 and mcap > 0:  # <50亿
            # V6修正：小市值-4.91%超额，加大惩罚
            capital -= 8
            add("资金面","小市值高波动",-8,f"市值{mcap/1e8:.0f}亿,回测显著跑输")

    # ==== V6 新增：主力资金净流向 ====
    if fund_flow:
        ff = fund_flow
        main_5d = ff.get("main_net_5d", 0)
        main_20d = ff.get("main_net_20d", 0)
        inflow_rate = ff.get("inflow_rate", 0)
        jumbo = ff.get("jumbo_net", 0)

        # 5日主力持续净流入
        if main_5d > 0 and main_20d > 0:
            capital += 8
            add("资金面","主力持续流入",8,f"5日净流入{main_5d/1e4:.0f}万,20日净流入{main_20d/1e4:.0f}万")
        elif main_5d > 0 and inflow_rate > 0.5:
            capital += 5
            add("资金面","主力短期流入",5,f"5日净流入{main_5d/1e4:.0f}万,占流通{inflow_rate:.1f}%")

        # 主力持续净流出
        if main_5d < 0 and main_20d < 0:
            capital -= 8
            add("资金面","主力持续流出",-8,f"5日净流出{abs(main_5d)/1e4:.0f}万,20日净流出{abs(main_20d)/1e4:.0f}万")
        elif main_5d < 0:
            capital -= 5
            add("资金面","主力短期流出",-5,f"5日净流出{abs(main_5d)/1e4:.0f}万")

        # 超大单方向
        if jumbo > 0 and main_5d > 0:
            capital += 3
            add("资金面","超大单强势",3,f"超大单净流入{jumbo/1e4:.0f}万")

    # ==== V7 新增：风险因子 ====
    if extra:
        risk = extra.get("_risk", {})
        pledge = risk.get("pledge", 0)
        unlock_days = risk.get("unlock_days", 999)
        if pledge > 50:
            capital -= 15
            add("风险","高质押风险",-15,f"质押比例{pledge:.0f}%>50%")
        elif pledge > 30:
            capital -= 8
            add("风险","中等质押风险",-8,f"质押比例{pledge:.0f}%>30%")
        if unlock_days <= 7:
            capital -= 10
            add("风险","解禁临近",-10,f"{unlock_days}天内有限售股解禁")

    # ==== V7 新增：融资余额变化 ====
    if margin_data:
        mdod = margin_data.get("margin_dod", 0)
        if mdod > 10:
            capital -= 8
            add("风险","融资激增",-8,f"融资余额日增{mdod:.0f}%,过度杠杆风险")
        elif mdod < -5:
            capital -= 5
            add("风险","融资骤降",-5,f"融资余额日降{abs(mdod):.0f}%,资金撤离")

    capital=max(5,min(95,int(capital)))

    # ==== V5 新增：大盘环境 + 板块强度 ====
    sector = extra.get("_sector", "") if extra else ""
    if market_regime:
        md = market_regime.get("market_score_delta", 0)
        if md != 0:
            label = "大盘强势" if md > 0 else "大盘弱势"
            add("大盘", label, md, f"前10行业均涨幅判定市场为{market_regime.get('regime','')}")

    if sector_strength and sector and sector in sector_strength:
        ss = sector_strength[sector]
        if ss != 0:
            add("板块", f"板块{'领涨' if ss > 0 else '拖累'}", ss, f"{sector}板块当日强度分={ss:+d}")

    # ---- 信息面 ----
    r3=(c[-1]/c[-4]-1)*100 if len(c)>=4 else 0
    if r3>8:
        info+=15
        add("信息面","3日强势上涨",15,f"3日涨幅+{r3:.1f}%")
    elif r3<-8:
        info-=12
        add("信息面","3日大幅下跌",-12,f"3日涨幅{r3:.1f}%")
    gap=(o[-1]-c[-2])/c[-2]*100 if len(c)>=2 else 0
    if abs(gap)>3:
        if gap>0:
            # V6修正：跳空高开是回测第一因子(+6.20%超额)，加大分值
            info += 15
            add("信息面","跳空高开",15,f"缺口+{gap:.1f}%")
        else:
            info-=5
            add("信息面","跳空低开",-5,f"缺口{gap:.1f}%")
    av20=sum(v[-21:-1])/20 if len(v)>=21 else av5
    vs=v[-1]/av20 if av20>0 else 1
    if vs>3:
        info-=3
        add("信息面","巨量比",-3,f"量比={vs:.1f}(>3),T+10夏普0.007/VaR5%-22.4%")
    elif vs>2:
        info+=5
        add("信息面","大量比",5,f"量比={vs:.1f}(>2)")
    if len(c)>=3 and c[-1]>c[-2]>c[-3]:
        info+=8
        add("信息面","三连涨",8,f"连涨3日")
    if len(c)>=3 and c[-1]<c[-2] and c[-2]<c[-3]:
        pass  # V8：连跌2日反弹信号删除，T+10回测夏普低于未触发、胜率48.6%
    if gap>2 and c[-1]<o[-1]:
        info=min(info,60)
        add("信息面","冲高回落封顶",0,f"跳空+{gap:.1f}%但收阴,信息面封顶60")

    info=max(5,min(95,int(info)))

    # ==== 事件驱动评分（独立维度，不混入信息面）====
    event_summary = {}
    event_score = 0
    if event_list and today_str:
        event_score, event_summary = calc_event_score(event_list, today_str)
        for etype, (delta, title, edate) in sorted(event_summary.items(), key=lambda x: abs(x[1][0]), reverse=True):
            if delta > 0:
                add("事件", f"{etype}公告", delta, f"{edate} {title[:40]}")
            elif delta < 0:
                add("事件", f"{etype}公告", delta, f"{edate} {title[:40]}")
    event_norm = 25 + max(-25, min(25, event_score))  # 映射到 0-50

    # V9 回测驱动权重: 技术35% + 资金55% + 信息5% + 事件5% + 环境加成
    # 原 V8: 技术40% + 资金40% + 信息20%（信息面被回测证明是噪声 IC=0.045）
    env_adjust = 0
    sector = extra.get("_sector", "") if extra else ""
    if market_regime:
        md = market_regime.get("market_score_delta", 0)
        if md != 0:
            env_adjust += md
    if sector_strength and sector and sector in sector_strength:
        ss = sector_strength[sector]
        if ss != 0:
            env_adjust += ss

    # V9 回测驱动权重: 技术35% + 资金55% + 信息5% + 事件5% + 环境加成
    # 原 V8: 技术40% + 资金40% + 信息20%（信息面被回测证明是噪声 IC=0.045）
    final_score = round(tech*0.35 + capital*0.55 + info*0.05 + event_norm*0.05 + env_adjust)
    final_score = max(5, min(95, final_score))

    return {
        "score": final_score,
        "tech": tech, "capital": capital, "info": info,
        "factors": factors,
        "event_summary": event_summary,
    }

def get_suggestion(score):
    # V7校准：冗余合并+废项删除后分数下移，阈值同步下调
    if score>=75: return "🔥强烈买入","strong_buy"
    elif score>=70: return "🟢逢低买入","buy"
    elif score>=60: return "🟡持有","hold"
    elif score>=45: return "⚪观望","watch"
    else: return "🔴回避","avoid"

def get_suggestion_intraday(score):
    """盘中建议阈值（T+1回测校准版 2026-07-02）"""
    if score>=68: return "🔥强烈买入","strong_buy"
    elif score>=60: return "🟢逢低买入","buy"
    elif score>=50: return "🟡持有","hold"
    elif score>=35: return "⚪观望","watch"
    else: return "🔴回避","avoid"


# ========== 盘中评分引擎 (2:30 PM) ==========

def score_intraday(klines, idx, realtime, event_list=None, today_str=None, extra=None,
                   market_regime=None, sector_strength=None, fund_flow=None, margin_data=None):
    """
    盘中评分引擎 — 2:30 PM 专属 (T+1优化版 2026-07-02)
    权重: 资金40% + 信息30% + 技术20% + 盘中动量10%
    目标: 当日尾盘买入 → 次日收盘卖出 (T+1)
    """
    if idx < 60: return None
    w = klines[:idx+1]
    c=[k['close'] for k in w]; v=[k['volume'] for k in w]
    h=[k['high'] for k in w]; l=[k['low'] for k in w]; o=[k['open'] for k in w]

    rt_price = realtime.get("price", c[-1])
    rt_open = realtime.get("open", o[-1])
    rt_high = realtime.get("high", h[-1])
    rt_low = realtime.get("low", l[-1])
    rt_vol = realtime.get("volume", v[-1])
    rt_chg = realtime.get("change_pct", 0)
    rt_vol_ratio = realtime.get("vol_ratio", 1)

    tech = 50; capital = 50; info = 50; momentum = 50
    factors = []

    def add(dim, name, delta, detail=""):
        factors.append({"dim": dim, "name": name, "delta": delta, "detail": detail})

    # ======== 技术面 (20%) — T+1回测校准版 ========
    ma5, ma10, ma20 = calc_ma(c,5), calc_ma(c,10), calc_ma(c,20)
    rsi = calc_rsi(c)

    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            tech += 15
            add("技术面","均线多头排列",15,f"MA5({ma5:.2f})>MA10>MA20")
        elif ma5 < ma10 < ma20:
            tech -= 10
            add("技术面","均线空头排列",-10)

    dif = calc_ema(c,12); dea = calc_ema(c,26)
    if dif and dea and dif > dea and dif > 0:
        if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
            tech += 2
        else:
            tech += 5
            add("技术面","MACD金叉多头",5,f"DIF({dif:.2f})>DEA")
    elif dif and dif < 0:
        tech -= 3
        add("技术面","MACD空头",-3)

    if 40 <= rsi <= 55:
        tech -= 3
        add("技术面","RSI弱势区",-3,f"RSI={rsi:.1f}")
    if rsi > 80:
        tech += 12
        add("技术面","RSI极端强势",12,f"RSI={rsi:.1f}(>80)")
    elif rsi > 75:
        tech += 10
        add("技术面","RSI强动量",10,f"RSI={rsi:.1f}(>75)")
    elif rsi < 30:
        tech -= 10
        add("技术面","RSI超卖",-10,f"RSI={rsi:.1f}(<30),T+1回测-0.30%→加重")

    if ma20:
        dev = (rt_price - ma20) / ma20 * 100
        if 2 < dev < 8:
            tech += 8
            add("技术面","适度偏离MA20",8,f"偏离+{dev:.1f}%")
        elif dev > 15:
            tech += 5
            add("技术面","过度偏离MA20(动量)",5,f"偏离+{dev:.1f}%,T+1回测+0.32%→动量延续")
        elif -5 < dev < -2:
            tech += 8
            add("技术面","MA20下方弱势(反弹)",8,f"偏离{dev:.1f}%,T+1回测+0.84%→均值回归")

    # 52周位置 — T+1回测: 高位区+0.51%→加分, 低位区-0.02%→基本无效
    if len(c) >= 120:
        h52, l52 = max(h[-120:]), min(l[-120:])
        pct52 = (rt_price - l52) / (h52 - l52) * 100 if h52 > l52 else 50
        if pct52 > 90:
            tech += 8
            add("技术面","52周高位区(动量延续)",8,f"位置{pct52:.0f}%,T+1回测+0.51%超额→加分")
        elif pct52 < 30:
            tech += 1
            add("技术面","52周低位区",1,f"位置{pct52:.0f}%,T+1回测-0.02%无区分度")

    tech = max(5, min(95, int(tech)))

    # ======== 资金面 (40%) — T+1回测校准版 ========
    # 持续放量（近5日趋势）
    if len(v) >= 5:
        vt = sum(1 for i in range(-5, 0) if v[i] > v[i-1])
        if vt >= 4:
            capital += 10
            add("资金面","持续放量",10,f"近5日{vt}天放量")

    # 实时量比（盘中版 — T+1回测: 放量上涨-0.12%→翻转扣分, 放量出货保持扣分, 温和放量保持）
    if rt_vol_ratio > 3 and rt_chg > 0:
        capital -= 3
        add("资金面","盘中放量上涨(追高)",-3,f"量比={rt_vol_ratio:.1f},T+1回测-0.12%→次日回落")
    elif rt_vol_ratio > 3 and rt_chg < 0:
        capital -= 8
        add("资金面","盘中放量出货",-8,f"量比={rt_vol_ratio:.1f},跌{rt_chg:.1f}%")
    elif rt_vol_ratio > 2 and rt_chg > 0:
        capital += 4
        add("资金面","盘中温和放量涨",4,f"量比={rt_vol_ratio:.1f}")

    # 缩量（量比<0.5）— T+1回测: 无量上涨+0.36%→加分, 缩量下跌-0.07%→保持
    if rt_vol_ratio < 0.5:
        if rt_chg < 0:
            capital -= 8
            add("资金面","盘中缩量下跌",-8,"无人承接")
        else:
            capital += 3
            add("资金面","盘中无量上涨(吸筹)",3,f"量比={rt_vol_ratio:.1f},T+1回测+0.36%")

    # 市值分层
    if extra:
        mcap = extra.get("mcap", 0)
        if mcap > 100000000000:
            capital += 3
            add("资金面","大市值稳定性",3,f"市值{mcap/1e8:.0f}亿")
        elif mcap < 5000000000 and mcap > 0:
            capital -= 8
            add("资金面","小市值高波动",-8,f"市值{mcap/1e8:.0f}亿")

    # 主力资金（T-1日）
    if fund_flow:
        ff = fund_flow
        main_5d = ff.get("main_net_5d", 0)
        main_20d = ff.get("main_net_20d", 0)
        inflow_rate = ff.get("inflow_rate", 0)
        jumbo = ff.get("jumbo_net", 0)

        if main_5d > 0 and main_20d > 0:
            capital += 10
            add("资金面","主力持续流入",10,f"5日+{main_5d/1e4:.0f}万,20日+{main_20d/1e4:.0f}万")
        elif main_5d > 0 and inflow_rate > 0.5:
            capital += 6
            add("资金面","主力短期流入",6,f"5日+{main_5d/1e4:.0f}万")
        if main_5d < 0 and main_20d < 0:
            capital -= 15
            add("资金面","主力持续流出",-15,f"5日{main_5d/1e4:.0f}万,20日{main_20d/1e4:.0f}万,T+1回测-0.51%→加重")
        elif main_5d < 0:
            capital -= 6
            add("资金面","主力短期流出",-6)
        if jumbo > 0 and main_5d > 0:
            capital += 3
            add("资金面","超大单强势",3)

    # 风险因子
    if extra:
        risk = extra.get("_risk", {})
        pledge = risk.get("pledge", 0)
        unlock_days = risk.get("unlock_days", 999)
        if pledge > 50:
            capital -= 15
            add("风险","高质押风险",-15,f"质押{pledge:.0f}%>50%")
        elif pledge > 30:
            capital -= 8
            add("风险","中等质押风险",-8,f"质押{pledge:.0f}%>30%")
        if unlock_days <= 7:
            capital -= 10
            add("风险","解禁临近",-10,f"{unlock_days}天内解禁")

    if margin_data:
        mdod = margin_data.get("margin_dod", 0)
        if mdod > 10:
            capital -= 8
            add("风险","融资激增",-8,f"融资日增{mdod:.0f}%")
        elif mdod < -5:
            capital -= 5
            add("风险","融资骤降",-5,f"融资日降{abs(mdod):.0f}%")

    capital = max(5, min(95, int(capital)))

    # ======== 大盘 + 板块 ========
    sector = extra.get("_sector", "") if extra else ""
    if market_regime:
        md = market_regime.get("market_score_delta", 0)
        if md != 0:
            add("大盘","大盘强势" if md>0 else "大盘弱势", md,
                f"市场{market_regime.get('regime','')}")
    if sector_strength and sector and sector in sector_strength:
        ss = sector_strength[sector]
        if ss != 0:
            add("板块",f"板块{'领涨' if ss>0 else '拖累'}", ss, f"{sector}强度{ss:+d}")

    # ======== 信息面 (30%) — T+1回测校准版 ========
    r3 = (rt_price / c[-4] - 1) * 100 if len(c) >= 4 else 0
    if r3 > 8:
        info += 15; add("信息面","3日强势",15,f"+{r3:.1f}%")
    elif r3 < -8:
        info += 15; add("信息面","3日大跌(超跌反弹)",15,f"{r3:.1f}%,T+1回测+1.48%超额→系统最强因子")

    # 跳空（基于开盘 vs 昨日收盘）
    gap = (rt_open - c[-1]) / c[-1] * 100
    if abs(gap) > 3:
        if gap > 0:
            info += 15; add("信息面","跳空高开",15,f"缺口+{gap:.1f}%")
        else:
            info += 0  # 跳空低开 T+1超额-0.03%, 无预测力, 不扣

    # 事件驱动
    event_summary = {}
    if event_list and today_str:
        edelta, event_summary = calc_event_score(event_list, today_str)
        info += edelta
        for etype, (delta, title, edate) in sorted(event_summary.items(), key=lambda x: abs(x[1][0]), reverse=True):
            add("事件", f"{etype}公告", delta, f"{edate} {title[:40]}")

    info = max(5, min(95, int(info)))

    # ======== 盘中动量 (10%) — T+1回测校准版 (2026-07-02) ========
    # T+1回测结论: 动量高分区间超额为负(-0.101%), 大幅降权。仅保留有正向T+1预测力的子因子。
    # 1. 日内价格位置: 当前价 vs 日内均线(近似)
    intra_avg = (rt_high + rt_low + rt_price) / 3
    pos_vs_avg = (rt_price - intra_avg) / intra_avg * 100
    if pos_vs_avg < -1.5:
        momentum += 5
        add("动量","价格低于日内中枢(低吸)",5,f"低于均值{pos_vs_avg:.1f}%,T+1回测+0.57%超额")
    elif pos_vs_avg > 1:
        momentum -= 6
        add("动量","价格微高日内中枢",-6,f"高于均值{pos_vs_avg:+.1f}%,T+1回测-0.41%超额→加重")

    # 2. 日内走势形态 — T+1回测: 持续走弱+0.31%(反转), 持续拉升-0.06%(小幅负)
    o2p = (rt_price - rt_open) / rt_open * 100  # 开盘到当前
    if o2p > 3:
        momentum -= 3
        add("动量","盘中持续拉升(追高)",-3,f"开→现+{o2p:.1f}%,T+1回测-0.06%小幅拖累")
    elif o2p < -3:
        momentum += 5
        add("动量","盘中持续走弱(反弹)",5,f"开→现{o2p:.1f}%,T+1回测+0.31%超额→加分")
    elif o2p < -1:
        momentum += 3
        add("动量","盘中回调(低吸窗口)",3,f"开→现{o2p:.1f}%")

    # 3. 突破/跌破日内极值 — T+1回测: 逼近日内新高+0.49%(大幅正向!), 逼近日内新低+0.29%
    pct_from_high = (rt_price - rt_high) / rt_high * 100 if rt_high > 0 else 0
    pct_from_low = (rt_price - rt_low) / rt_low * 100 if rt_low > 0 else 0
    if pct_from_high > -0.3:
        momentum += 4
        add("动量","逼近日内新高(动量延续)",4,f"距高点{pct_from_high:+.1f}%,T+1回测+0.49%超额→加分")
    elif pct_from_low < 0.3 and o2p < 0:
        momentum += 3
        add("动量","逼近日内新低(支撑)",3,f"距低点{pct_from_low:+.1f}%,T+1回测+0.29%超额→加分")

    # 4. V型反转检测（效果不显著，降权）
    if rt_low > 0 and rt_price > rt_open:
        dip_pct = (rt_low - rt_open) / rt_open * 100
        recovery_pct = (rt_price - rt_low) / rt_low * 100
        if dip_pct < -2 and recovery_pct > 2:
            momentum += 4
            add("动量","V型反转",4,f"探底{dip_pct:.1f}%后反弹{recovery_pct:.1f}%")

    # 5. 开盘方向 — T+1回测: 跳空强势+0.03%(near zero), 高开回调+0.004%(zero), 低开高走-0.06%
    if rt_chg > 0 and gap > 0:
        momentum += 0  # 跳空强势延续 T+1超额仅+0.03%, 不扣分
    elif rt_chg < 0 and gap > 1:  # 跳空高开但走弱 → T+1超额几乎为零
        momentum += 1
        add("动量","高开回调(低吸机会)",1,f"跳空+{gap:.1f}%但现跌{rt_chg:.1f}%,T+1回测超额仅+0.004%")
    elif rt_chg > 0 and gap < -1:  # 低开高走
        momentum += 3
        add("动量","低开高走",3,f"跳空{gap:.1f}%但现涨{rt_chg:+.1f}%")

    # 6. 振幅信号 — T+1回测: 收阳+0.21%, 收阴+0.28%(surprisingly positive), 温和惩罚
    ampl = (rt_high - rt_low) / rt_open * 100 if rt_open > 0 else 0
    if ampl > 5 and o2p > 0:
        momentum += 6
        add("动量","高振幅收阳",6,f"振幅{ampl:.1f}%,多头博弈胜出")
    elif ampl > 5 and o2p < 0:
        momentum -= 3
        add("动量","高振幅收阴(反弹)",-3,f"振幅{ampl:.1f}%,T+1回测+0.28%超额仍偏正")

    # 7. 昨跌今涨（反弹确认）— T+1回测超额-0.11%，降权
    if len(c) >= 2:
        yest_chg = (c[-1] - c[-2]) / c[-2] * 100
        if yest_chg < -2 and rt_chg > 1:
            momentum += 3
            add("动量","昨日超跌反弹",3,f"昨跌{yest_chg:.1f}%,今涨{rt_chg:+.1f}%")

    momentum = max(5, min(95, int(momentum)))

    # ======== 加权合成 ========
    # T+1回测校准 (2026-07-02): 资金40%+信息30%+技术20%+动量10%
    score = round(tech * 0.20 + capital * 0.40 + info * 0.30 + momentum * 0.10)

    return {
        "score": score,
        "tech": tech, "capital": capital, "info": info, "momentum": momentum,
        "factors": factors,
        "event_summary": event_summary,
    }

# ========== 主题板块（10个，根据申万一级行业 + 主营估值逻辑）==========
THEME_SECTORS = {
    "半导体/芯片": ["半导体/芯片"],
    "AI/算力/通信": ["AI/算力/通信"],
    "新能源/电力": ["新能源/电力"],
    "传媒/游戏": ["传媒/游戏"],
    "智能制造": ["智能制造"],
    "电子/消费电子": ["电子/消费电子"],
    "化工/新材料": ["化工/新材料"],
    "消费": ["消费"],
    "医药生物": ["医药生物"],
    "金融/交通/基建": ["金融/交通/基建"],
}

# ========== 股票→主题板块映射（申万一级行业 → 估值板块，2026-07-01 重构）==========
STOCK_SECTOR = {
    "000034":"AI/算力/通信",
    "000333":"消费",
    "000519":"智能制造",
    "000524":"消费",
    "000559":"智能制造",
    "000628":"金融/交通/基建",
    "000636":"半导体/芯片",
    "000791":"新能源/电力",
    "000858":"消费",
    "000892":"传媒/游戏",
    "000938":"AI/算力/通信",
    "000977":"AI/算力/通信",
    "000988":"智能制造",
    "001300":"消费",
    "001309":"半导体/芯片",
    "001339":"AI/算力/通信",
    "002009":"智能制造",
    "002027":"传媒/游戏",
    "002028":"新能源/电力",
    "002050":"消费",
    "002108":"化工/新材料",
    "002112":"新能源/电力",
    "002121":"新能源/电力",
    "002130":"半导体/芯片",
    "002131":"智能制造",
    "002149":"化工/新材料",
    "002202":"新能源/电力",
    "002210":"新能源/电力",
    "002222":"半导体/芯片",
    "002229":"消费",
    "002261":"AI/算力/通信",
    "002270":"新能源/电力",
    "002272":"智能制造",
    "002281":"AI/算力/通信",
    "002284":"智能制造",
    "002292":"传媒/游戏",
    "002304":"消费",
    "002335":"新能源/电力",
    "002338":"智能制造",
    "002345":"消费",
    "002371":"半导体/芯片",
    "002384":"半导体/芯片",
    "002400":"传媒/游戏",
    "002407":"化工/新材料",
    "002414":"智能制造",
    "002415":"AI/算力/通信",
    "002425":"传媒/游戏",
    "002428":"化工/新材料",
    "002429":"消费",
    "002463":"半导体/芯片",
    "002472":"智能制造",
    "002475":"电子/消费电子",
    "002517":"传媒/游戏",
    "002536":"智能制造",
    "002555":"传媒/游戏",
    "002602":"传媒/游戏",
    "002630":"新能源/电力",
    "002826":"医药生物",
    "002837":"智能制造",
    "002851":"新能源/电力",
    "002896":"智能制造",
    "002916":"半导体/芯片",
    "002922":"电子/消费电子",
    "002927":"新能源/电力",
    "002931":"智能制造",
    "300010":"消费",
    "300014":"新能源/电力",
    "300054":"半导体/芯片",
    "300058":"传媒/游戏",
    "300093":"新能源/电力",
    "300096":"AI/算力/通信",
    "300124":"智能制造",
    "300133":"传媒/游戏",
    "300136":"电子/消费电子",
    "300182":"传媒/游戏",
    "300229":"AI/算力/通信",
    "300251":"传媒/游戏",
    "300272":"消费",
    "300274":"新能源/电力",
    "300285":"电子/消费电子",
    "300291":"传媒/游戏",
    "300302":"AI/算力/通信",
    "300308":"AI/算力/通信",
    "300315":"传媒/游戏",
    "300346":"半导体/芯片",
    "300353":"AI/算力/通信",
    "300364":"传媒/游戏",
    "300373":"半导体/芯片",
    "300394":"AI/算力/通信",
    "300408":"电子/消费电子",
    "300418":"传媒/游戏",
    "300424":"智能制造",
    "300433":"电子/消费电子",
    "300439":"医药生物",
    "300442":"AI/算力/通信",
    "300450":"新能源/电力",
    "300463":"医药生物",
    "300465":"AI/算力/通信",
    "300476":"半导体/芯片",
    "300481":"电子/消费电子",
    "300497":"医药生物",
    "300499":"智能制造",
    "300502":"AI/算力/通信",
    "300533":"传媒/游戏",
    "300567":"智能制造",
    "300570":"AI/算力/通信",
    "300580":"智能制造",
    "300620":"AI/算力/通信",
    "300624":"AI/算力/通信",
    "300642":"医药生物",
    "300643":"智能制造",
    "300655":"半导体/芯片",
    "300666":"半导体/芯片",
    "300738":"AI/算力/通信",
    "300750":"新能源/电力",
    "300757":"智能制造",
    "300781":"传媒/游戏",
    "300806":"化工/新材料",
    "300827":"新能源/电力",
    "301012":"新能源/电力",
    "301018":"智能制造",
    "301080":"医药生物",
    "301183":"半导体/芯片",
    "301209":"化工/新材料",
    "301231":"传媒/游戏",
    "301236":"AI/算力/通信",
    "301308":"半导体/芯片",
    "301358":"新能源/电力",
    "301377":"智能制造",
    "301396":"AI/算力/通信",
    "301408":"医药生物",
    "301486":"电子/消费电子",
    "301526":"化工/新材料",
    "301630":"半导体/芯片",
    "600021":"新能源/电力",
    "600032":"新能源/电力",
    "600089":"新能源/电力",
    "600109":"金融/交通/基建",
    "600171":"半导体/芯片",
    "600176":"化工/新材料",
    "600186":"消费",
    "600269":"金融/交通/基建",
    "600276":"医药生物",
    "600330":"半导体/芯片",
    "600351":"医药生物",
    "600518":"医药生物",
    "600519":"消费",
    "600550":"新能源/电力",
    "600584":"半导体/芯片",
    "600585":"化工/新材料",
    "600618":"化工/新材料",
    "600633":"传媒/游戏",
    "600674":"新能源/电力",
    "600703":"半导体/芯片",
    "600707":"电子/消费电子",
    "600801":"化工/新材料",
    "600892":"传媒/游戏",
    "600919":"金融/交通/基建",
    "600938":"新能源/电力",
    "600941":"AI/算力/通信",
    "601006":"金融/交通/基建",
    "601012":"新能源/电力",
    "601101":"新能源/电力",
    "601127":"智能制造",
    "601138":"电子/消费电子",
    "601208":"化工/新材料",
    "601216":"化工/新材料",
    "601225":"新能源/电力",
    "601318":"金融/交通/基建",
    "601328":"金融/交通/基建",
    "601398":"金融/交通/基建",
    "601689":"智能制造",
    "601869":"AI/算力/通信",
    "601872":"金融/交通/基建",
    "601919":"金融/交通/基建",
    "601921":"传媒/游戏",
    "601928":"传媒/游戏",
    "603019":"AI/算力/通信",
    "603040":"智能制造",
    "603083":"AI/算力/通信",
    "603119":"智能制造",
    "603200":"新能源/电力",
    "603220":"AI/算力/通信",
    "603228":"半导体/芯片",
    "603259":"医药生物",
    "603389":"消费",
    "603396":"新能源/电力",
    "603444":"传媒/游戏",
    "603607":"消费",
    "603618":"新能源/电力",
    "603619":"新能源/电力",
    "603626":"半导体/芯片",
    "603629":"半导体/芯片",
    "603663":"化工/新材料",
    "603667":"智能制造",
    "603685":"电子/消费电子",
    "603703":"电子/消费电子",
    "603728":"新能源/电力",
    "603738":"半导体/芯片",
    "603938":"化工/新材料",
    "603985":"新能源/电力",
    "603986":"半导体/芯片",
    "603993":"化工/新材料",
    "605006":"化工/新材料",
    "605277":"电子/消费电子",
    "605287":"金融/交通/基建",
    "688008":"半导体/芯片",
    "688012":"半导体/芯片",
    "688017":"智能制造",
    "688025":"智能制造",
    "688041":"半导体/芯片",
    "688072":"半导体/芯片",
    "688126":"半导体/芯片",
    "688146":"半导体/芯片",
    "688158":"AI/算力/通信",
    "688160":"智能制造",
    "688167":"半导体/芯片",
    "688183":"半导体/芯片",
    "688195":"半导体/芯片",
    "688256":"半导体/芯片",
    "688268":"半导体/芯片",
    "688347":"半导体/芯片",
    "688396":"半导体/芯片",
    "688400":"智能制造",
    "688498":"半导体/芯片",
    "688499":"新能源/电力",
    "688515":"半导体/芯片",
    "688521":"半导体/芯片",
    "688676":"新能源/电力",
    "688795":"半导体/芯片",
    "832571":"智能制造",
    "920808":"智能制造",
    # === 以下为2026-07-02补充分类（原"其他"归类，基于申万三级行业）===
    "000062":"电子/消费电子",  # 其他电子 → 电子
    "000063":"AI/算力/通信",   # 通信设备
    "000066":"AI/算力/通信",   # 计算机设备
    "000541":"消费",           # 照明设备
    "000651":"消费",           # 白色家电
    "002008":"智能制造",       # 自动化设备
    "002049":"半导体/芯片",   # 半导体
    "002054":"化工/新材料",   # 化学制品
    "002236":"AI/算力/通信",   # 计算机设备
    "002277":"消费",           # 一般零售
    "002396":"AI/算力/通信",   # 通信设备
    "002409":"半导体/芯片",   # 半导体
    "003031":"电子/消费电子", # 通信设备
    "300316":"新能源/电力",   # 光伏设备
    "300339":"AI/算力/通信",   # IT服务
    "301269":"AI/算力/通信",   # 软件开发(EDA)
    "301611":"半导体/芯片",   # 半导体设备
    "600100":"AI/算力/通信",   # 计算机设备
    "600183":"半导体/芯片",   # 元件(PCB)
    "600406":"新能源/电力",   # 电网设备
    "600522":"AI/算力/通信",   # 通信设备
    "600536":"AI/算力/通信",   # IT服务
    "600673":"医药生物",       # 综合(医药制造为主)
    "600895":"金融/交通/基建", # 房地产开发
    "601766":"智能制造",       # 轨交设备
    "603505":"化工/新材料",   # 化学制品(萤石资源)
    "603650":"化工/新材料",   # 橡胶
    "605589":"化工/新材料",   # 塑料(酚醛树脂)
    "688019":"半导体/芯片",   # 电子化学品(光刻胶)
    "688037":"半导体/芯片",   # 半导体(涂胶显影)
    "688082":"半导体/芯片",   # 半导体(清洗设备)
    "688107":"半导体/芯片",   # 半导体(FPGA)
    "688120":"半导体/芯片",   # 半导体(CMP)
    "688172":"半导体/芯片",   # 半导体(晶圆制造)
    "688187":"智能制造",       # 轨交设备(IGBT/电驱)
    "688206":"AI/算力/通信",   # 软件开发(EDA)
    "688409":"半导体/芯片",   # 半导体(精密零部件)
    "688525":"半导体/芯片",   # 半导体(存储封测)
    "688545":"半导体/芯片",   # 电子化学品(电子级化学品)
    "688630":"智能制造",       # 专用设备(LDI光刻)
    "688766":"半导体/芯片",   # 半导体(NOR Flash)
}

def get_theme(code):
    """直接返回预设主题板块（基于申万行业+主营估值）"""
    return STOCK_SECTOR.get(code, "其他")

# ========== 生成报告 ==========
def generate_report(results, today, output_dir, hist_scores=None):
    if hist_scores is None: hist_scores = {}
    
    def trend_str(code):
        """过去5个交易日的评分趋势，如 68→72→75→73→79 ↑"""
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return "-"
        # 取最近5天的评分
        recent = [int(s) for s in scores[-5:]]
        # 去重：连续的相同分数合并为一个
        deduped = []
        for s in recent:
            if not deduped or s != deduped[-1]:
                deduped.append(s)
        if len(deduped) < 2: return str(deduped[0]) if deduped else "-"
        # 箭头方向
        if deduped[-1] > deduped[-2]: arrow = "↑"
        elif deduped[-1] < deduped[-2]: arrow = "↓"
        else: arrow = "→"
        return "→".join(str(s) for s in deduped) + f" {arrow}"
    
    strong_buy=[r for r in results if r["sug_action"]=="strong_buy"]
    buy_list=[r for r in results if r["sug_action"]=="buy"]
    hold_list=[r for r in results if r["sug_action"]=="hold"]
    watch_list=[r for r in results if r["sug_action"]=="watch"]
    avoid_list=[r for r in results if r["sug_action"]=="avoid"]
    risky=[r for r in avoid_list if r["score"]<50]
    critical=[r for r in avoid_list if r["score"]<40]
    
    sectors={}
    for r in results:
        s=r.get("sector","其他"); sectors[s]=sectors.get(s,0)+1
    top_sectors=sorted(sectors.items(),key=lambda x:x[1],reverse=True)[:8]
    
    sector_groups={}
    for r in results:
        sec=r.get("sector","其他")
        if sec not in sector_groups: sector_groups[sec]=[]
        sector_groups[sec].append(r)
    sorted_sectors=sorted(sector_groups.items(),key=lambda x:max(r["score"] for r in x[1]),reverse=True)
    
    # 表格列（含收盘价）
    hdr="| 代码 | 名称 | 概念板块 | SS分 | 5日评分趋势 | 收盘价 | 日涨跌 | 5日涨跌 | 20日涨跌 | 建议 |"
    sep="|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|"
    rhdr="| 代码 | 名称 | 概念板块 | SS分 | 5日评分趋势 | 收盘价 | 日涨跌 | 5日涨跌 | 20日涨跌 | 风险 |"
    rsep="|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|"
    sec_hdr="| 代码 | 名称 | SS分 | 5日评分趋势 | 收盘价 | 日涨跌 | 5日涨跌 | 20日涨跌 | 建议 |"
    sec_sep="|------|------|:---:|:---:|:---:|:---:|:---:|:---:|------|"
    
    def row(r, cls=""):
        t=trend_str(r["code"])
        return f"| {r['code']} | {r['name']} | {r.get('sector','')} | **{r['score']}** | {t} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['ret_5d']:+.1f}% | {r['ret_20d']:+.1f}% | {r['suggestion']} |"
    
    md=[]
    md.append(f"# 📊 SS with GLM 股票评分日报")
    md.append(f"**日期**: {today} | **模型**: SS with GLM V8 | **股票池**: {len(results)}只A股")
    md.append(f"")
    md.append(f"## 📈 市场概况")
    md.append(f"- 🔥强烈买入(≥80): **{len(strong_buy)}只** | 🟢逢低买入(≥75): **{len(buy_list)}只** | 🟡持有(65-74): **{len(hold_list)}只** | ⚪观望(50-64): **{len(watch_list)}只** | 🔴回避(<50): **{len(avoid_list)}只**")
    md.append(f"- 🚨触发卖出(<40): **{len(critical)}只** | ⚠️持续低分预警: **{len(risky)}只**")
    md.append(f"- 板块覆盖: {len(sectors)}个 | 主力: {', '.join(f'{s}({n})' for s,n in top_sectors)}")
    md.append(f"")

    # ==== 重要事件摘要 ====
    event_stocks = []
    for r in results:
        es = r.get("event_summary", {})
        if es:
            tags_list = []
            for etype, (delta, title, edate) in sorted(es.items(), key=lambda x: abs(x[1][0]), reverse=True):
                emoji = "🔴" if delta < 0 else "🟢"
                tags_list.append(f"{emoji}{etype}({delta:+d})")
            event_stocks.append((r["code"], r["name"], r["score"], tags_list))
    if event_stocks:
        md.append(f"## 📰 重要事件（近两周公告）")
        md.append(f"")
        md.append(f"> 共 **{len(event_stocks)}只** 股票存在有评分影响的关键公告")
        md.append(f"")
        for code, name, score, tags in sorted(event_stocks, key=lambda x: x[2], reverse=True)[:30]:
            md.append(f"- **{code} {name}**({score}分) {' '.join(tags)}")
        if len(event_stocks) > 30:
            md.append(f"- ...共{len(event_stocks)}只有事件影响（详见 HTML 报告）")
        md.append(f"")

    # ===== 第一部分：TOP 5 + 快速变化 =====
    md.append(f"## 🏆 综合排名概览")
    md.append(f"")
    md.append(f"### 🔥 最佳 TOP 5")
    md.append(f"")
    md.append(hdr); md.append(sep)
    for r in results[:5]:
        md.append(row(r))
    md.append("")

    # 快速上升/快速下滑（基于历史评分变化）
    def score_delta(code):
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return 0
        return int(scores[-1]) - int(scores[-2])

    risers = sorted(results, key=lambda r: score_delta(r["code"]), reverse=True)[:5]
    fallers = sorted(results, key=lambda r: score_delta(r["code"]))[:5]

    md.append(f"### 🚀 快速上升 TOP 5（评分跳升）")
    md.append(f"")
    md.append(rhdr); md.append(rsep)
    for r in risers:
        d = score_delta(r["code"])
        icon = "🚀" if d >= 5 else "📈"
        t=trend_str(r["code"])
        md.append(f"| {r['code']} | {r['name']} | {r.get('sector','')} | **{r['score']}** | {t} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['ret_5d']:+.1f}% | {r['ret_20d']:+.1f}% | {icon} +{d} |")
    md.append("")

    md.append(f"### 🔻 快速下滑 TOP 5（评分急降）")
    md.append(f"")
    md.append(rhdr); md.append(rsep)
    for r in fallers:
        d = score_delta(r["code"])
        icon = "🔻" if d <= -5 else "📉"
        t=trend_str(r["code"])
        md.append(f"| {r['code']} | {r['name']} | {r.get('sector','')} | **{r['score']}** | {t} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['ret_5d']:+.1f}% | {r['ret_20d']:+.1f}% | {icon} {d} |")
    md.append("")
    
    # ===== 第二部分：全部股票按概念板块分组 =====
    md.append(f"## 📊 按概念板块排名（共{len(sorted_sectors)}个板块）")
    md.append(f"")
    for sec_name, sec_stocks in sorted_sectors:
        sec_best = max(r["score"] for r in sec_stocks)
        sec_worst = min(r["score"] for r in sec_stocks)
        sec_buy = sum(1 for r in sec_stocks if r["sug_action"] in ("strong_buy","buy"))
        sec_avoid = sum(1 for r in sec_stocks if r["sug_action"]=="avoid")
        flags = []
        if sec_buy > 0: flags.append(f"🟢推荐{sec_buy}只")
        if sec_avoid > 0: flags.append(f"🔴回避{sec_avoid}只")
        flag_str = " | ".join(flags)
        md.append(f"### {sec_name} ({len(sec_stocks)}只, 最高{sec_best}/最低{sec_worst}) {flag_str}")
        md.append(f"")
        md.append(sec_hdr); md.append(sec_sep)
        for r in sorted(sec_stocks, key=lambda x: x["score"], reverse=True):
            t=trend_str(r["code"])
            md.append(f"| {r['code']} | {r['name']} | **{r['score']}** | {t} | {r['price']:.2f} | {r['change_pct']:+.2f}% | {r['ret_5d']:+.1f}% | {r['ret_20d']:+.1f}% | {r['suggestion']} |")
        md.append("")
    
    md_path=os.path.join(output_dir,f"SS_with_GLM评分_{today}.md")
    with open(md_path,"w") as f: f.write("\n".join(md))
    
    # HTML 交互版（Tab切换，用于浏览器/聊天窗口预览）
    html = generate_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors)
    html_path=os.path.join(output_dir,f"SS_with_GLM评分_{today}.html")
    with open(html_path,"w") as f: f.write(html)
    
    # HTML 邮件版（全内联样式，无JS，分段表格）
    email_html = generate_email_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors)
    email_html_path=os.path.join(output_dir,f"SS_with_GLM评分_{today}_email.html")
    with open(email_html_path,"w") as f: f.write(email_html)
    
    return md_path, html_path, email_html_path


def generate_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors):
    """生成带Tab切换的HTML报告（斑马纹、固定列宽、板块分sheet）"""

    def trend_str(code):
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return "-"
        recent = [int(s) for s in scores[-5:]]
        deduped = []
        for s in recent:
            if not deduped or s != deduped[-1]:
                deduped.append(s)
        if len(deduped) < 2: return str(deduped[0]) if deduped else "-"
        if deduped[-1] > deduped[-2]: arrow = "↑"
        elif deduped[-1] < deduped[-2]: arrow = "↓"
        else: arrow = "→"
        return "→".join(str(s) for s in deduped) + f" {arrow}"

    def score_badge(score):
        """返回 (badge_html, suggestion_color)"""
        if score >= 75:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#fef3c7;color:#92400e;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#92400e"
        elif score >= 70:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#dcfce7;color:#166534;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#166534"
        elif score >= 60:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#dbeafe;color:#1e40af;font-weight:600;font-size:13px;text-align:center">{score}</span>', "#1e40af"
        elif score >= 45:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#f1f5f9;color:#475569;font-weight:600;font-size:13px;text-align:center">{score}</span>', "#475569"
        else:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#fee2e2;color:#991b1b;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#991b1b"

    def trend_color(t):
        if t.endswith("↑"): return "#16a34a"
        elif t.endswith("↓"): return "#dc2626"
        elif t.endswith("→"): return "#94a3b8"
        return "#333"

    def pct_color(val):
        if val > 0: return "color:#dc2626"
        elif val < 0: return "color:#16a34a"
        return "color:#666"

    # ---- 样式常量 ----
    TH_BASE = "padding:9px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;white-space:nowrap;position:sticky;top:0;z-index:1"
    TD_BASE = 'padding:7px 6px;font-size:13px;color:#333;white-space:nowrap'
    TD_NUM  = 'padding:7px 6px;font-size:13px;color:#333;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums'

    def make_factors_detail_html(r):
        """生成加减分因子明细HTML（用于展开行），含关键事件"""
        factors = r.get("factors", [])
        if not factors:
            return '<p style="color:#999;font-size:12px;margin:0">暂无分析数据</p>'
        pos = sorted([f for f in factors if f["delta"] > 0], key=lambda x: x["delta"], reverse=True)
        neg = sorted([f for f in factors if f["delta"] < 0], key=lambda x: x["delta"])
        caps = [f for f in factors if f["delta"] == 0]
        parts = []
        # 维度分值
        parts.append(f'<div style="margin-bottom:8px"><span style="font-size:12px;color:#666;margin-right:12px">技术面 <b style="color:#1a1a2e;font-size:14px">{r.get("tech",50)}</b></span><span style="font-size:12px;color:#666;margin-right:12px">资金面 <b style="color:#1a1a2e;font-size:14px">{r.get("capital",50)}</b></span><span style="font-size:12px;color:#666">信息面 <b style="color:#1a1a2e;font-size:14px">{r.get("info",50)}</b></span><span style="font-size:12px;color:#888;margin-left:12px">权重: 技术40% · 资金40% · 信息20%</span></div>')
        # 关键事件
        event_summary = r.get("event_summary", {})
        events = r.get("events", [])
        if event_summary or events:
            parts.append('<div style="margin-bottom:8px">')
            parts.append('<span style="font-size:12px;color:#7c3aed;font-weight:600">📰 近两周关键事件</span>')
            if event_summary:
                for etype, (delta, title, edate) in sorted(event_summary.items(), key=lambda x: abs(x[1][0]), reverse=True):
                    color = "#dc2626" if delta < 0 else "#16a34a"
                    bg = "#fef2f2" if delta < 0 else "#f0fdf4"
                    tag = "🔴" if delta < 0 else "🟢"
                    parts.append(f'<div style="margin:4px 0;padding:4px 10px;border-radius:6px;background:{bg};font-size:12px">{tag} <b style="color:{color}">{etype} {delta:+d}</b> <span style="color:#666;font-size:11px">[{edate}]</span> <span style="color:#333">{title[:80]}</span></div>')
            elif events:
                # 无评分事件，直接显示所有原始事件
                shown = 0
                for ev in events[:6]:
                    if shown >= 5: break
                    shown += 1
                    etag = "🔴" if (ev.get("base_score") or 0) < 0 else "⚪"
                    parts.append(f'<div style="margin:2px 0;font-size:11px;color:#666">{etag} [{ev.get("date","")}] {ev.get("title","")[:80]}</div>')
                if len(events) > 5:
                    parts.append(f'<div style="font-size:10px;color:#999">...共{len(events)}条公告</div>')
            parts.append('</div>')
        if pos:
            parts.append('<div style="margin-bottom:6px"><span style="font-size:12px;color:#166534;font-weight:600">🟢 加分项</span></div>')
            tags = []
            for f in pos:
                tags.append(f'<span style="display:inline-block;margin:2px 4px;padding:3px 10px;border-radius:6px;background:#dcfce7;color:#166534;font-size:12px">{f["name"]} <b>+{f["delta"]}</b><span style="color:#888;font-size:11px;margin-left:4px">{f["detail"]}</span></span>')
            parts.append(f'<div style="margin-bottom:8px">{"".join(tags)}</div>')
        if neg:
            parts.append('<div style="margin-bottom:6px"><span style="font-size:12px;color:#991b1b;font-weight:600">🔴 减分项</span></div>')
            tags = []
            for f in neg:
                tags.append(f'<span style="display:inline-block;margin:2px 4px;padding:3px 10px;border-radius:6px;background:#fee2e2;color:#991b1b;font-size:12px">{f["name"]} <b>{f["delta"]}</b><span style="color:#888;font-size:11px;margin-left:4px">{f["detail"]}</span></span>')
            parts.append(f'<div style="margin-bottom:8px">{"".join(tags)}</div>')
        if caps:
            parts.append('<div style="margin-bottom:6px"><span style="font-size:12px;color:#856404;font-weight:600">⚠️ 封顶/限制项</span></div>')
            tags = []
            for f in caps:
                tags.append(f'<span style="display:inline-block;margin:2px 4px;padding:3px 10px;border-radius:6px;background:#fff3cd;color:#856404;font-size:12px">{f["name"]}<span style="color:#888;font-size:11px;margin-left:4px">{f["detail"]}</span></span>')
            parts.append(f'<div>{"".join(tags)}</div>')
        return "".join(parts)

    def make_row_html(r, row_idx):
        """生成一行HTML（主行+展开分析行），含斑马纹"""
        t = trend_str(r["code"])
        tc = trend_color(t)
        badge, sug_color = score_badge(r["score"])
        bg = "#f8f9fc" if row_idx % 2 == 0 else "#ffffff"
        price = f'{r["price"]:.2f}'
        cp = pct_color(r["change_pct"])
        r5 = pct_color(r["ret_5d"])
        r20 = pct_color(r["ret_20d"])
        detail_id = f"detail-{r['code']}-{row_idx}"
        main_row = f"""<tr style="background:{bg};cursor:pointer" onclick="toggleDetail('{detail_id}')">
<td style="{TD_BASE};text-align:center;font-size:14px">📊</td>
<td style="{TD_BASE}">{r['code']}</td>
<td style="{TD_BASE};font-weight:500">{r['name']}</td>
<td style="{TD_BASE}">{r.get('sector','')}</td>
<td style="{TD_BASE};text-align:center">{badge}</td>
<td style="{TD_BASE};color:{tc};font-size:12px">{t}</td>
<td style="{TD_NUM}">{price}</td>
<td style="{TD_NUM};{cp}">{r['change_pct']:+.2f}%</td>
<td style="{TD_NUM};{r5}">{r['ret_5d']:+.1f}%</td>
<td style="{TD_NUM};{r20}">{r['ret_20d']:+.1f}%</td>
<td style="{TD_BASE};color:{sug_color};font-weight:600;font-size:12px">{r['suggestion']}</td>
</tr>"""
        detail_row = f"""<tr id="{detail_id}" style="display:none">
<td colspan="11" style="padding:12px 16px;background:#f8f9fc;border-bottom:2px solid #e8ecf1">
{make_factors_detail_html(r)}
</td></tr>"""
        return main_row + "\n" + detail_row

    def make_table_html(stocks):
        """生成一个完整表格的 tbody HTML"""
        sorted_stocks = sorted(stocks, key=lambda x: x["score"], reverse=True)
        rows = []
        for i, r in enumerate(sorted_stocks):
            rows.append(make_row_html(r, i))
        return "\n".join(rows)

    # ---- 按股票数量降序排列板块 ----
    sectors_by_count = sorted(sorted_sectors, key=lambda x: len(x[1]), reverse=True)

    # ---- 构建Tab列表：全部 + 各板块 ----
    tabs = []
    # Tab 0: 全部
    tabs.append(("全部", len(results), make_table_html(results)))
    # Tab 1..N: 各板块（按数量降序）
    for sec_name, sec_stocks in sectors_by_count:
        tabs.append((sec_name, len(sec_stocks), make_table_html(sec_stocks)))

    # ---- 生成Tab按钮HTML ----
    tab_buttons = []
    for i, (name, count, _) in enumerate(tabs):
        active = "tab-active" if i == 0 else ""
        tab_buttons.append(
            f'<button class="tab-btn {active}" onclick="switchTab({i})">'
            f'{name} <span class="tab-count">{count}</span></button>'
        )
    tab_buttons_html = "\n".join(tab_buttons)

    # ---- 生成各Tab内容面板HTML ----
    tab_panels = []
    colgroup = """<colgroup>
<col style="width:32px"><col style="width:62px"><col style="width:72px"><col style="width:110px"><col style="width:52px">
<col style="width:150px"><col style="width:64px"><col style="width:60px"><col style="width:56px"><col style="width:56px"><col style="width:80px">
</colgroup>"""
    thead = f"""<thead><tr>
<th style="{TH_BASE};text-align:center">分析</th>
<th style="{TH_BASE};text-align:center">代码</th>
<th style="{TH_BASE}">名称</th>
<th style="{TH_BASE}">概念板块</th>
<th style="{TH_BASE};text-align:center">SS分</th>
<th style="{TH_BASE}">5日评分趋势</th>
<th style="{TH_BASE};text-align:right">收盘价</th>
<th style="{TH_BASE};text-align:right">日涨跌</th>
<th style="{TH_BASE};text-align:right">5日涨跌</th>
<th style="{TH_BASE};text-align:right">20日涨跌</th>
<th style="{TH_BASE}">建议</th>
</tr></thead>"""

    for i, (name, count, tbody_html) in enumerate(tabs):
        display = "block" if i == 0 else "none"
        sec_best = max(r["score"] for r in results if r.get("sector") == name) if name != "全部" else max(r["score"] for r in results)
        sec_worst = min(r["score"] for r in results if r.get("sector") == name) if name != "全部" else min(r["score"] for r in results)
        sec_buy = sum(1 for r in results if r.get("sector") == name and r["sug_action"] in ("strong_buy","buy")) if name != "全部" else len(strong_buy)+len(buy_list)
        sec_avoid = sum(1 for r in results if r.get("sector") == name and r["sug_action"]=="avoid") if name != "全部" else len(avoid_list)

        tags = ""
        if sec_buy > 0:
            tags += f'<span class="sec-tag" style="background:#dcfce7;color:#166534">🟢 推荐{sec_buy}</span>'
        if sec_avoid > 0:
            tags += f'<span class="sec-tag" style="background:#fee2e2;color:#991b1b">🔴 回避{sec_avoid}</span>'

        tab_panels.append(f"""<div id="tab-panel-{i}" class="tab-panel" style="display:{display}">
<div class="sec-info"><span class="sec-name">📌 {name}</span><span class="sec-meta">{count}只 · 最高{sec_best}/最低{sec_worst}</span>{tags}</div>
<div class="table-wrap">
<table style="width:100%;border-collapse:collapse;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;table-layout:fixed">
{colgroup}{thead}<tbody>{tbody_html}</tbody></table></div></div>""")
    tab_panels_html = "\n".join(tab_panels)

    # ---- 统计卡片 ----
    stat_cards = [
        ("#fbbf24", "#fef3c7", "#92400e", len(strong_buy), "🔥 强烈买入 ≥80"),
        ("#4ade80", "#dcfce7", "#166534", len(buy_list), "🟢 逢低买入 ≥75"),
        ("#60a5fa", "#dbeafe", "#1e40af", len(hold_list), "🟡 持有 65-74"),
        ("#94a3b8", "#f1f5f9", "#475569", len(watch_list), "⚪ 观望 50-64"),
        ("#f87171", "#fee2e2", "#991b1b", len(avoid_list), "🔴 回避 <50"),
        ("#ef4444", "#fecaca", "#7f1d1d", len(critical), "🚨 触发卖出 <40"),
    ]
    card_html = ""
    for color, bg, text_color, count, label in stat_cards:
        card_html += f'<div style="background:{bg};border-radius:10px;padding:12px 8px;text-align:center;border:1px solid {color}33;flex:1;min-width:100px"><div style="font-size:24px;font-weight:800;color:{text_color};line-height:1.2">{count}</div><div style="font-size:11px;color:{text_color};margin-top:3px">{label}</div></div>'

    # ---- TOP5 / 快速上升 / 快速下滑 摘要 ----
    top5_summary = ""
    for r in results[:5]:
        top5_summary += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:#fef3c7;font-size:12px;color:#92400e;font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b></span>'

    def score_delta(code):
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return 0
        return int(scores[-1]) - int(scores[-2])

    risers = sorted(results, key=lambda r: score_delta(r["code"]), reverse=True)[:5]
    fallers = sorted(results, key=lambda r: score_delta(r["code"]))[:5]

    risers_summary = ""
    for r in risers:
        d = score_delta(r["code"])
        risers_summary += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:#dcfce7;font-size:12px;color:#166534;font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b> +{d}</span>'

    fallers_summary = ""
    for r in fallers:
        d = score_delta(r["code"])
        fallers_summary += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:#fef2f2;font-size:12px;color:#dc2626;font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b> {d}</span>'

    # ---- 完整HTML ----
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SS with GLM 股票评分日报 {today}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:0; background:#eef0f5; font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif; }}
.container {{ max-width:900px; margin:0 auto; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 2px 16px rgba(0,0,0,0.08); }}
.header {{ padding:24px 28px 14px; background:linear-gradient(135deg,#1a1a2e,#16213e); }}
.header h1 {{ margin:0 0 6px; font-size:20px; color:#e94560; }}
.header p {{ margin:0; font-size:13px; color:#94a3b8; }}
.header strong {{ color:#e6edf3; }}
.stats {{ display:flex; gap:8px; padding:16px 28px 8px; flex-wrap:wrap; }}
.stat-note {{ padding:0 28px 8px; font-size:12px; color:#888; }}
.summary {{ padding:8px 28px; }}
.summary h3 {{ margin:8px 0 6px; font-size:14px; }}
.top5 {{ margin-bottom:12px; }}
.risers {{ margin-bottom:12px; }}
.fallers {{}}
.tab-bar {{ display:flex; gap:4px; padding:8px 28px 0; flex-wrap:wrap; border-bottom:2px solid #e8ecf1; }}
.tab-btn {{ padding:7px 14px; border:none; border-radius:8px 8px 0 0; cursor:pointer; font-size:13px; font-weight:500; background:#f1f5f9; color:#64748b; transition:all .15s; font-family:inherit; }}
.tab-btn:hover {{ background:#e2e8f0; color:#334155; }}
.tab-btn.tab-active {{ background:#1a1a2e; color:#fff; font-weight:600; }}
.tab-btn .tab-count {{ display:inline-block; margin-left:3px; padding:1px 6px; border-radius:8px; background:rgba(0,0,0,0.12); font-size:11px; }}
.tab-btn.tab-active .tab-count {{ background:rgba(255,255,255,0.2); }}
.tab-panel {{ padding:12px 28px 20px; }}
.sec-info {{ margin-bottom:8px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
.sec-name {{ font-size:14px; font-weight:600; color:#1a1a2e; }}
.sec-meta {{ font-size:12px; color:#888; }}
.sec-tag {{ display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px; font-weight:500; }}
.table-wrap {{ border:1px solid #e2e8f0; border-radius:8px; overflow:auto; max-height:600px; }}
.table-wrap table {{ width:100%; border-collapse:collapse; }}
.table-wrap thead tr {{ position:sticky; top:0; z-index:1; }}
.table-wrap tbody tr:hover {{ background:#eef2ff !important; }}
.footer {{ padding:14px 28px 20px; text-align:center; border-top:1px solid #e8ecf1; }}
.footer p {{ margin:0; font-size:11px; color:#999; }}
</style>
<script>
function switchTab(idx) {{
  document.querySelectorAll('.tab-panel').forEach((p,i)=>{{ p.style.display = i===idx ? 'block' : 'none'; }});
  document.querySelectorAll('.tab-btn').forEach((b,i)=>{{ b.className = i===idx ? 'tab-btn tab-active' : 'tab-btn'; }});
}}
function toggleDetail(id) {{
  var el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}}
</script>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📊 SS with GLM 股票评分日报</h1>
    <p><strong>{today}</strong> &nbsp;|&nbsp; SS with GLM V8 &nbsp;|&nbsp; {len(results)}只A股 &nbsp;|&nbsp; 板块覆盖: {len(sectors)}个</p>
  </div>
  <div class="stats">{card_html}</div>
  <p class="stat-note">主力板块: {', '.join(f'{s}({n})' for s, n in top_sectors)}</p>
  <div class="summary">
    <div class="top5"><h3 style="color:#92400e">🔥 最佳 TOP 5</h3>{top5_summary}</div>
    <div class="risers"><h3 style="color:#166534">🚀 快速上升 TOP 5</h3>{risers_summary}</div>
    <div class="fallers"><h3 style="color:#dc2626">🔻 快速下滑 TOP 5</h3>{fallers_summary}</div>
  </div>
  <div class="tab-bar">{tab_buttons_html}</div>
  {tab_panels_html}
  <div class="footer">
    <p>自动生成 | SS with GLM V8 | 策略: ≥75强烈买入 · ≥70逢低买入 · ≥60持有 · ≥45观望 · <45回避 · <40触发卖出
    <p style="margin-top:4px">V6: 回测驱动权重修正+主力资金+板块阈值; V5: 大盘环境+板块强度</p>
  </div>
</div>
</body></html>"""

    return html


def generate_email_html_report(results, today, hist_scores, strong_buy, buy_list, hold_list, watch_list, avoid_list, critical, risky, sectors, top_sectors, sorted_sectors):
    """生成邮件兼容的HTML报告（全内联样式，无JS，分段表格，与交互版视觉一致）"""

    def trend_str(code):
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return "-"
        recent = [int(s) for s in scores[-5:]]
        deduped = []
        for s in recent:
            if not deduped or s != deduped[-1]:
                deduped.append(s)
        if len(deduped) < 2: return str(deduped[0]) if deduped else "-"
        if deduped[-1] > deduped[-2]: arrow = "↑"
        elif deduped[-1] < deduped[-2]: arrow = "↓"
        else: arrow = "→"
        return "→".join(str(s) for s in deduped) + f" {arrow}"

    def score_badge(score):
        if score >= 75:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#fef3c7;color:#92400e;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#92400e"
        elif score >= 70:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#dcfce7;color:#166534;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#166534"
        elif score >= 60:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#dbeafe;color:#1e40af;font-weight:600;font-size:13px;text-align:center">{score}</span>', "#1e40af"
        elif score >= 45:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#f1f5f9;color:#475569;font-weight:600;font-size:13px;text-align:center">{score}</span>', "#475569"
        else:
            return f'<span style="display:inline-block;min-width:28px;padding:2px 8px;border-radius:6px;background:#fee2e2;color:#991b1b;font-weight:700;font-size:13px;text-align:center">{score}</span>', "#991b1b"

    def trend_color(t):
        if t.endswith("↑"): return "#16a34a"
        elif t.endswith("↓"): return "#dc2626"
        elif t.endswith("→"): return "#94a3b8"
        return "#333"

    def pct_style(val):
        if val > 0: return "color:#dc2626"
        elif val < 0: return "color:#16a34a"
        return "color:#666"

    TH = "padding:9px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;white-space:nowrap;text-align:left"
    TH_R = "padding:9px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;white-space:nowrap;text-align:right"
    TH_C = "padding:9px 6px;font-weight:600;font-size:12px;background:#374151;color:#fff;white-space:nowrap;text-align:center"
    TD = 'padding:7px 6px;font-size:13px;color:#333;white-space:nowrap'
    TD_R = 'padding:7px 6px;font-size:13px;color:#333;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums'
    TD_C = 'padding:7px 6px;font-size:13px;color:#333;text-align:center;white-space:nowrap'
    TBL = "width:100%;border-collapse:collapse;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;table-layout:fixed"

    def make_factors_compact(r):
        """生成紧凑版加减分标签（用于邮件子行），含关键事件"""
        factors = r.get("factors", [])
        pos = sorted([f for f in factors if f["delta"] > 0], key=lambda x: x["delta"], reverse=True)[:2]
        neg = sorted([f for f in factors if f["delta"] < 0], key=lambda x: x["delta"])[:2]
        tags = []
        # 事件标签优先
        evs = r.get("event_summary", {})
        for etype, (delta, title, edate) in sorted(evs.items(), key=lambda x: abs(x[1][0]), reverse=True)[:3]:
            bg_c = "#fef2f2" if delta < 0 else "#f0fdf4"
            tc = "#dc2626" if delta < 0 else "#16a34a"
            tags.append(f'<span style="display:inline-block;margin:1px 3px;padding:1px 6px;border-radius:4px;background:{bg_c};color:{tc};font-size:11px">📰{etype}{delta:+d}</span>')
        for f in pos:
            tags.append(f'<span style="display:inline-block;margin:1px 3px;padding:1px 6px;border-radius:4px;background:#dcfce7;color:#166534;font-size:11px">{f["name"]}+{f["delta"]}</span>')
        for f in neg:
            tags.append(f'<span style="display:inline-block;margin:1px 3px;padding:1px 6px;border-radius:4px;background:#fee2e2;color:#991b1b;font-size:11px">{f["name"]}{f["delta"]}</span>')
        return "".join(tags)

    def make_row(r, row_idx, with_sector=True):
        t = trend_str(r["code"])
        tc = trend_color(t)
        badge, sug_color = score_badge(r["score"])
        bg = "#f8f9fc" if row_idx % 2 == 0 else "#ffffff"
        price = f'{r["price"]:.2f}'
        cp = pct_style(r["change_pct"])
        r5 = pct_style(r["ret_5d"])
        r20 = pct_style(r["ret_20d"])
        sector_cell = f'<td style="{TD}">{r.get("sector","")}</td>' if with_sector else ""
        colspan = "10" if with_sector else "9"
        factors_tags = make_factors_compact(r)
        factors_row = f"""<tr style="background:{bg}">
<td colspan="{colspan}" style="padding:2px 8px 6px;font-size:11px;color:#666;border-bottom:1px solid #e8ecf1">
<span style="color:#888;margin-right:4px">技术{r.get("tech",50)}/资金{r.get("capital",50)}/信息{r.get("info",50)}</span>{factors_tags}
</td></tr>""" if factors_tags else ""
        return f"""<tr style="background:{bg}">
<td style="{TD}">{r['code']}</td>
<td style="{TD};font-weight:500">{r['name']}</td>
{sector_cell}
<td style="{TD_C}">{badge}</td>
<td style="{TD};color:{tc};font-size:12px">{t}</td>
<td style="{TD_R}">{price}</td>
<td style="{TD_R};{cp}">{r['change_pct']:+.2f}%</td>
<td style="{TD_R};{r5}">{r['ret_5d']:+.1f}%</td>
<td style="{TD_R};{r20}">{r['ret_20d']:+.1f}%</td>
<td style="{TD};color:{sug_color};font-weight:600;font-size:12px">{r['suggestion']}</td>
</tr>{chr(10) + factors_row if factors_row else ''}"""

    def make_table(stocks, with_sector=True):
        sorted_stocks = sorted(stocks, key=lambda x: x["score"], reverse=True)
        rows = [make_row(r, i, with_sector) for i, r in enumerate(sorted_stocks)]
        sector_th = f'<th style="{TH}">概念板块</th>' if with_sector else ""
        col_w_sector = '<col style="width:110px">' if with_sector else ""
        col_w_others = 'width:72px' if with_sector else 'width:80px'
        return f"""<table style="{TBL}">
<colgroup>
<col style="width:62px"><col style="width:72px">{col_w_sector}<col style="width:52px">
<col style="width:150px"><col style="width:64px"><col style="width:60px"><col style="width:56px"><col style="width:56px"><col style="width:80px">
</colgroup>
<thead><tr>
<th style="{TH_C}">代码</th>
<th style="{TH}">名称</th>
{sector_th}
<th style="{TH_C}">SS分</th>
<th style="{TH}">5日评分趋势</th>
<th style="{TH_R}">收盘价</th>
<th style="{TH_R}">日涨跌</th>
<th style="{TH_R}">5日涨跌</th>
<th style="{TH_R}">20日涨跌</th>
<th style="{TH}">建议</th>
</tr></thead>
<tbody>{chr(10).join(rows)}</tbody>
</table>"""

    # ---- 按股票数量降序排列板块 ----
    sectors_by_count = sorted(sorted_sectors, key=lambda x: len(x[1]), reverse=True)

    # ---- 统计卡片 ----
    stat_cards = [
        ("#fef3c7", "#92400e", len(strong_buy), "🔥 强烈买入 ≥80"),
        ("#dcfce7", "#166534", len(buy_list), "🟢 逢低买入 ≥75"),
        ("#dbeafe", "#1e40af", len(hold_list), "🟡 持有 65-74"),
        ("#f1f5f9", "#475569", len(watch_list), "⚪ 观望 50-64"),
        ("#fee2e2", "#991b1b", len(avoid_list), "🔴 回避 <50"),
        ("#fecaca", "#7f1d1d", len(critical), "🚨 触发卖出 <40"),
    ]
    card_cells = ""
    for bg, text_color, count, label in stat_cards:
        card_cells += f'<td style="padding:6px;width:16.6%"><div style="background:{bg};border-radius:10px;padding:12px 6px;text-align:center;border:1px solid {text_color}22"><div style="font-size:24px;font-weight:800;color:{text_color};line-height:1.2">{count}</div><div style="font-size:11px;color:{text_color};margin-top:3px">{label}</div></div></td>'

    # ---- TOP5 / 快速上升 / 快速下滑 摘要 ----
    top5_tags = ""
    for r in results[:5]:
        top5_tags += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:#fef3c7;font-size:12px;color:#92400e;font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b></span>'

    def email_delta(code):
        scores = hist_scores.get(code, [])
        if len(scores) < 2: return 0
        return int(scores[-1]) - int(scores[-2])

    risers = sorted(results, key=lambda r: email_delta(r["code"]), reverse=True)[:5]
    fallers = sorted(results, key=lambda r: email_delta(r["code"]))[:5]

    risers_tags = ""
    for r in risers:
        d = email_delta(r["code"])
        risers_tags += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:#dcfce7;font-size:12px;color:#166534;font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b> +{d}</span>'

    fallers_tags = ""
    for r in fallers:
        d = email_delta(r["code"])
        fallers_tags += f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 10px;border-radius:8px;background:#fef2f2;font-size:12px;color:#dc2626;font-weight:600">{r["code"]} {r["name"]} <b>{r["score"]}</b> {d}</span>'

    # ---- 重要事件摘要 ----
    event_tags = ""
    for r in results:
        es = r.get("event_summary", {})
        if es:
            for etype, (delta, title, edate) in sorted(es.items(), key=lambda x: abs(x[1][0]), reverse=True):
                emoji = "🔴" if delta < 0 else "🟢"
                bg_c = "#fef2f2" if delta < 0 else "#f0fdf4"
                tc = "#dc2626" if delta < 0 else "#16a34a"
                event_tags += f'<span style="display:inline-block;margin:2px 4px;padding:2px 8px;border-radius:6px;background:{bg_c};font-size:11px;color:{tc}">{emoji}{r["code"]}{r["name"]}{etype}({delta:+d})</span>'
    event_section = ""
    if event_tags:
        event_section = f'<tr><td style="padding:8px 28px 4px"><h3 style="margin:8px 0 6px;font-size:14px;color:#7c3aed">📰 关键事件（近两周公告）</h3><p style="margin:0 0 10px;line-height:1.8">{event_tags}</p></td></tr>'

    # ---- 各板块分段表格 ----
    section_html_parts = []
    # 各板块
    for sec_name, sec_stocks in sectors_by_count:
        sec_best = max(r["score"] for r in sec_stocks)
        sec_worst = min(r["score"] for r in sec_stocks)
        sec_buy = sum(1 for r in sec_stocks if r["sug_action"] in ("strong_buy", "buy"))
        sec_avoid = sum(1 for r in sec_stocks if r["sug_action"] == "avoid")
        anchor = f"sec-{hash(sec_name)%99999}"
        tags = ""
        if sec_buy > 0:
            tags += f'<span style="display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:#dcfce7;color:#166534;font-weight:500">🟢 推荐{sec_buy}</span>'
        if sec_avoid > 0:
            tags += f'<span style="display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:#fee2e2;color:#991b1b;font-weight:500">🔴 回避{sec_avoid}</span>'

        section_html_parts.append(f"""<div style="margin-bottom:20px" id="{anchor}">
<div style="background:#f8f9fc;padding:10px 16px;border:1px solid #e2e8f0;border-bottom:none;border-radius:8px 8px 0 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
<span style="font-size:14px;font-weight:600;color:#1a1a2e">📌 {sec_name}</span>
<span style="font-size:12px;color:#888">{len(sec_stocks)}只 · 最高{sec_best}/最低{sec_worst}</span>
{tags}
</div>
<div style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;overflow-x:auto">
{make_table(sec_stocks, with_sector=False)}
</div>
</div>""")

    sections_html = chr(10).join(section_html_parts)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>SS with GLM 股票评分日报 {today}</title></head>
<body style="margin:0;padding:0;background:#eef0f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#eef0f5"><tr><td align="center" style="padding:20px 12px">
<table width="900" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,0.08)">
<tr><td style="padding:24px 28px 14px;background:linear-gradient(135deg,#1a1a2e,#16213e)">
<h1 style="margin:0 0 6px;font-size:20px;color:#e94560">📊 SS with GLM 股票评分日报</h1>
<p style="margin:0;font-size:13px;color:#94a3b8"><strong style="color:#e6edf3">{today}</strong> &nbsp;|&nbsp; SS with GLM V8 &nbsp;|&nbsp; {len(results)}只A股 &nbsp;|&nbsp; 板块覆盖: {len(sectors)}个</p>
</td></tr>
<tr><td style="padding:16px 28px 8px">
<table width="100%" cellpadding="0" cellspacing="0"><tr>{card_cells}</tr></table>
<p style="font-size:12px;color:#888;margin-top:10px">主力板块: {', '.join(f'{s}({n})' for s, n in top_sectors)}</p>
</td></tr>
<tr><td style="padding:8px 28px 4px">
<h3 style="margin:8px 0 6px;font-size:14px;color:#92400e">🔥 最佳 TOP 5</h3>
<p style="margin:0 0 10px">{top5_tags}</p>
<h3 style="margin:8px 0 6px;font-size:14px;color:#166534">🚀 快速上升 TOP 5</h3>
<p style="margin:0 0 10px">{risers_tags}</p>
<h3 style="margin:8px 0 6px;font-size:14px;color:#dc2626">🔻 快速下滑 TOP 5</h3>
<p style="margin:0 0 10px">{fallers_tags}</p>
</td></tr>
{event_section}
<tr><td style="padding:16px 28px 20px">
{sections_html}
</td></tr>
<tr><td style="padding:14px 28px 20px;text-align:center;border-top:1px solid #e8ecf1">
<p style="margin:0;font-size:11px;color:#999">自动生成 | SS with GLM V8 | 策略: ≥75强烈买入 · ≥70逢低买入 · ≥60持有 · ≥45观望 · <45回避 · <40触发卖出
<p style="margin:4px 0 0;font-size:11px;color:#999">V6:回测驱动权重修正+主力资金+板块阈值收紧; V5:大盘+板块</p>
</td></tr>
</table>
</td></tr></table>
</body></html>"""

    return html

# ========== 发送邮件（QQ邮箱 SMTP）==========
def send_email(md_path, email_html_path, html_path, today, recipient="914110627@qq.com"):
    """通过QQ邮箱SMTP发送报告邮件（正文+交互版HTML附件）"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders
    
    if recipient is None:
        recipient = "914110627@qq.com"

    smtp_config = {
        "host": os.environ.get("SMTP_HOST", "smtp.qq.com"),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
    }

    try:
        with open(md_path) as f: md_content = f.read()
        with open(email_html_path) as f: html_content = f.read()
        with open(html_path) as f: html_attach = f.read()
    except Exception as e:
        print(f"  [ERROR] 读取报告文件失败: {e}")
        return False
    
    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"📊 SS with GLM 股票评分日报 {today}"
    msg["From"] = smtp_config["user"]
    msg["To"] = recipient

    # 正文（alternative：纯文本 + HTML邮件版）
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(md_content, "plain", "utf-8"))
    body.attach(MIMEText(html_content, "html", "utf-8"))
    msg.attach(body)

    # 附件：交互版HTML（可在浏览器直接打开）
    attach = MIMEBase("application", "octet-stream")
    attach.set_payload(html_attach.encode("utf-8"))
    encoders.encode_base64(attach)
    fname = os.path.basename(html_path)
    attach.add_header("Content-Disposition", "attachment", filename=("utf-8", "", fname))
    msg.attach(attach)
    
    try:
        server = smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30)
        server.starttls()
        server.login(smtp_config["user"], smtp_config["password"])
        server.sendmail(smtp_config["user"], recipient, msg.as_string())
        server.quit()
        print(f"  ✅ 邮件已发送至 {recipient}")
        return True
    except Exception as e:
        print(f"  [ERROR] 邮件发送失败: {e}")
        return False

# ========== 集成 GLM 评分（锦上添花，Sharpe +71%）==========
import numpy as np

def _glm_ols(X, y, ridge=1.0, weights=None):
    n, p = X.shape
    W = np.diag(np.sqrt(weights)) if weights is not None else np.eye(n)
    Xw, yw = W @ X, W @ y
    XtX = Xw.T @ Xw + ridge * np.eye(p)
    try: return np.linalg.solve(XtX, Xw.T @ yw)
    except: return np.linalg.lstsq(Xw, yw, rcond=None)[0]

def _glm_features(klines, idx, extra=None):
    """提取15个原始连续特征"""
    w = klines[:idx+1]
    c = np.array([k['close'] for k in w], float)
    v = np.array([k['volume'] for k in w], float)
    h = np.array([k['high'] for k in w], float)
    l = np.array([k['low'] for k in w], float)
    o = np.array([k['open'] for k in w], float)
    f = []
    ma5, ma10, ma20 = calc_ma(c,5), calc_ma(c,10), calc_ma(c,20)
    f.append((ma5/ma10-1)*100 if (ma5 and ma10) else 0)
    f.append(1 if (ma5 and ma10 and ma20 and ma5>ma10>ma20) else 0)
    rsi = calc_rsi(c)
    f.append(rsi); f.append(rsi**2)
    dif, dea = calc_ema(c,12), calc_ema(c,26)
    f.append((dif-dea)/c[-1]*1000 if (dif and dea and c[-1]>0) else 0)
    av5 = np.mean(v[-6:-1]) if len(v)>=6 else v[-1]
    f.append(v[-1]/av5 if av5>0 else 1)
    f.append((c[-1]/c[-6]-1)*100 if len(c)>=6 else 0)
    f.append((c[-1]-ma20)/ma20*100 if ma20 else 0)
    if len(c)>=120:
        h52,l52 = max(h[-120:]), min(l[-120:])
        f.append((c[-1]-l52)/(h52-l52)*100 if h52>l52 else 50)
    else: f.append(50)
    tp = (h+l+c)/3
    pf = np.sum(np.where(tp[-14:]>np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 0
    nf_v = np.sum(np.where(tp[-14:]<=np.roll(tp,1)[-14:], tp[-14:]*v[-14:], 0)) if len(c)>=15 else 1
    f.append(100-100/(1+pf/nf_v) if nf_v>0 else 50)
    mf_v = [(c[i]-l[i]-(h[i]-c[i]))/(h[i]-l[i])*v[i] if h[i]!=l[i] else 0 for i in range(-20,0) if i>-len(c)]
    f.append(sum(mf_v)/sum(v[-20:]) if mf_v and sum(v[-20:])>0 else 0)
    f.append(sum(1 for i in range(-5,0) if v[i]>v[i-1]) if len(v)>=6 else 0)
    mcap = extra.get("mcap",0) if extra else 0
    f.append(math.log(mcap+1e8) if mcap>0 else 0)
    f.append((o[-1]-c[-2])/c[-2]*100 if len(c)>=2 and c[-2]>0 else 0)
    streak = 0
    for i in range(1, min(5,len(c)-1)):
        if c[-i]>c[-i-1]: streak+=1
        else: break
    f.append(float(streak))
    return np.array(f, float)

def _glm_expand(X):
    """添加平方项和关键交互项"""
    extra = []
    for i in range(X.shape[1]): extra.append(X[:,i]**2)
    pairs = [(2,5),(0,13),(10,6),(8,7),(5,6),(9,1),(11,6),(13,6)]
    for a,b in pairs:
        if a<X.shape[1] and b<X.shape[1]: extra.append(X[:,a]*X[:,b])
    return np.column_stack([X]+extra)

def score_ensemble_glm(codes, klines_all, extra_all, train_days=60):
    """
    集成 GLM 评分：训练3个 GLM（线性+多项式+时间加权），集成预测。
    返回 {code: glm_score}，0-100 分制。
    回测验证：Sharpe 3.66 (T+10)，vs 固定权重 2.14。
    """
    if len(klines_all) < 30:
        return {}

    # 收集训练数据
    train_X, train_Y = [], []
    for code in codes:
        kl = klines_all.get(code)
        if not kl or len(kl) < 60: continue
        ex = extra_all.get(code, {})
        # 用历史日期构建训练样本
        for i in range(max(30, len(kl)-train_days-10), len(kl)-10):
            try:
                fv = _glm_features(kl, i, extra=ex)
                y = (kl[i+10]['close']/kl[i]['close']-1)*100
                train_X.append(fv); train_Y.append(y)
            except: pass

    if len(train_X) < 100:
        return {}

    X_tr = np.array(train_X); Y_tr = np.array(train_Y)

    # Fit 3 GLMs
    beta_linear = _glm_ols(X_tr, Y_tr, 1.0)
    Xp_tr = _glm_expand(X_tr)
    beta_poly = _glm_ols(Xp_tr, Y_tr, 2.0)
    tw = np.exp(-0.02 * np.arange(len(Y_tr)-1, -1, -1))
    beta_tw = _glm_ols(Xp_tr, Y_tr, 2.0, tw)

    # Predict today
    result = {}
    raw_vals = []
    code_order = []
    for code in codes:
        kl = klines_all.get(code)
        if not kl or len(kl) < 30: continue
        ex = extra_all.get(code, {})
        try:
            fv = _glm_features(kl, len(kl)-1, extra=ex)
            p_linear = float(fv @ beta_linear)
            p_poly = float(_glm_expand(fv.reshape(1,-1))[0] @ beta_poly)
            p_tw = float(_glm_expand(fv.reshape(1,-1))[0] @ beta_tw)
            raw = (p_linear + p_poly + p_tw) / 3
            raw_vals.append(raw)
            code_order.append(code)
        except: pass

    # Normalize: use robust Z-score + sigmoid to 5-95
    if len(raw_vals) < 10:
        return {}
    raw = np.array(raw_vals, dtype=float)
    mu = np.median(raw)  # robust center
    sigma = max(0.5, np.std(raw))  # prevent collapse
    z = (raw - mu) / sigma
    # sigmoid-like mapping to 5-95
    for code, rv, zv in zip(code_order, raw_vals, z):
        # range zv from ~-3 to ~+3, map to 5-95
        normalized = max(-3, min(3, zv))  # clip extreme outliers
        score = int(5 + (normalized + 3) / 6 * 90)  # [-3,3] → [5,95]
        result[code] = score

    return result

# ========== 主流程 ==========
def run_daily_scoring(stock_codes, output_dir="/workspace", send_mail=True, recipient=None, use_cache=False):
    """SS with GLM 每日评分主流程。
    
    use_cache=True: 从 output/_cache_{date}.json 加载已抓取数据，跳过所有HTTP/子进程调用，
                    直接评分 + 生成报告。适用于改分类/改权重等无需刷新数据的场景。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"╔══════════════════════════════════════════╗")
    print(f"║  SS with GLM 每日评分 [{today}]  ║")
    print(f"╚══════════════════════════════════════════╝")
    print(f"股票池: {len(stock_codes)} 只")
    
    # 加载历史评分 {code: {date: score}}
    hist_path = os.path.join(output_dir, "ss_score_history.json")
    hist_data = {}
    if os.path.exists(hist_path):
        try:
            with open(hist_path) as f: hist_data = json.load(f)
        except: pass
    
    # 转换为 {code: [score_d5, score_d4, ...]} 供趋势展示
    hist_scores = {}
    for code, date_scores in hist_data.items():
        hist_scores[code] = [s for d, s in sorted(date_scores.items())]
    
    cache_path = os.path.join(output_dir, f"_cache_{today}.json")
    
    if use_cache and os.path.exists(cache_path):
        print("\n♻️  [缓存模式] 加载已缓存数据，跳过所有数据抓取...")
        with open(cache_path, "r") as f:
            cache = json.load(f)
        klines = cache.get("klines", {})
        extra = cache.get("extra", {})
        market_regime = cache.get("market_regime", {"regime": "neutral", "market_score_delta": 0})
        sector_strength = cache.get("sector_strength", {})
        news_events = cache.get("news_events", {})
        fund_flow = cache.get("fund_flow", {})
        risk_data = cache.get("risk_data", {})
        print(f"  加载: {len(klines)} K线, {len(extra)} 行情, {sum(len(v) for v in news_events.values())} 事件")
    else:
        print("\n[1/4] 获取行情数据...")
        klines = fetch_kline_batch(stock_codes)
        extra = fetch_extra_info(stock_codes)
        print(f"  K线: {len(klines)} | 行情: {len(extra)}")

        print("\n[2/4] 获取板块大盘环境...")
        market_regime, sector_strength = fetch_sector_context()
        if market_regime.get("regime") != "neutral":
            print(f"  大盘: {market_regime['regime']} ({market_regime['market_score_delta']:+d})")
        print(f"  板块强度: {len(sector_strength)}个板块有信号")

        # 预计算每只股票的主题板块
        for code in extra:
            extra[code]["_sector"] = get_theme(code)

        print("\n[3/4] 并行获取主力资金/风险因子/公告事件...")
        with ThreadPoolExecutor(max_workers=3) as executor:
            f_fund = executor.submit(fetch_fund_flow, stock_codes)
            f_risk = executor.submit(fetch_risk_factors, stock_codes)
            f_events = executor.submit(fetch_news_events, stock_codes)
            fund_flow = f_fund.result()
            risk_data = f_risk.result()
            news_events = f_events.result()

        ff_codes = sum(1 for v in fund_flow.values() if v.get("main_net_5d", 0) != 0)
        print(f"  资金流数据: {ff_codes}只")

        risk_count = sum(1 for v in risk_data.values() if v.get("pledge",0) > 30 or v.get("unlock_days",999) <= 7)
        print(f"  风险信号: {risk_count}只有质押/解禁风险")

        ev_codes = sum(1 for v in news_events.values() if v)
        ev_total = sum(len(v) for v in news_events.values())
        print(f"  有事件股票: {ev_codes}只 | 事件总数: {ev_total}条")

        # 保存中间数据缓存（当天可复用）
        with open(cache_path, "w") as f:
            json.dump({"klines": klines, "extra": extra, "market_regime": market_regime,
                       "sector_strength": sector_strength, "news_events": news_events,
                       "fund_flow": fund_flow, "risk_data": risk_data}, f,
                      ensure_ascii=False)

    for code in extra:
        extra[code]["_risk"] = risk_data.get(code, {"pledge": 0, "unlock_days": 999})
    for code in extra:
        if "_sector" not in extra[code]:
            extra[code]["_sector"] = get_theme(code)
    
    print("\n[4/5] 评分中...")
    results = []
    for code in stock_codes:
        kl = klines.get(code)
        if not kl or len(kl) < 60: continue
        ex = extra.get(code, {})
        ev_list = news_events.get(code, [])
        ff = fund_flow.get(code, {})
        s = score_ss_enhanced(kl, len(kl)-1, event_list=ev_list, today_str=today, extra=ex,
                              market_regime=market_regime, sector_strength=sector_strength,
                              fund_flow=ff)
        if s is None: continue
        ret_5d = (kl[-1]['close']/kl[-6]['close']-1)*100 if len(kl)>=6 else 0
        ret_20d = (kl[-1]['close']/kl[-21]['close']-1)*100 if len(kl)>=21 else 0
        sug_label, sug_action = get_suggestion(s["score"])
        results.append({
            "code":code,"name":ex.get("name",""),"price":ex.get("price",0),
            "change_pct":ex.get("change_pct",0),"ret_5d":ret_5d,"ret_20d":ret_20d,
            "sector": get_theme(code),
            "score":s["score"],"tech":s["tech"],"capital":s["capital"],"info":s["info"],
            "suggestion":sug_label,"sug_action":sug_action,
            "factors":s.get("factors",[]),
            "events": ev_list,
            "event_summary": s.get("event_summary", {}),
        })
        
        # 按日期存储评分（去重：同一天只保留最新）
        if code not in hist_data: hist_data[code] = {}
        hist_data[code][today] = s["score"]
        # 只保留最近15天
        sorted_dates = sorted(hist_data[code].keys())
        if len(sorted_dates) > 15:
            for old_date in sorted_dates[:-15]:
                del hist_data[code][old_date]
    
    results.sort(key=lambda x: x["score"], reverse=True)
    
    # 转换为趋势格式
    hist_scores = {}
    for code, date_scores in hist_data.items():
        hist_scores[code] = [s for d, s in sorted(date_scores.items())]
    
    # 保存历史评分
    with open(hist_path, "w") as f: json.dump(hist_data, f, ensure_ascii=False)
    
    # ==== 集成 GLM 评分（锦上添花） ====
    print(f"\n[GLM] 集成 GLM 评分中...")
    glm_scores = score_ensemble_glm(stock_codes, klines, extra, train_days=60)
    if glm_scores:
        for r in results:
            gs = glm_scores.get(r["code"])
            if gs is not None:
                r["glm_score"] = gs
        glm_top = sorted(results, key=lambda x: x.get("glm_score", 0), reverse=True)[:5]
        print(f"  GLM Top5: " + " | ".join(f"{r['code']} {r['name']}: G{r.get('glm_score','?')}" for r in glm_top))
    else:
        print(f"  ⚠️ GLM 训练数据不足，跳过")
    
    print(f"\n[5/5] 生成报告...")
    sb=sum(1 for r in results if r["sug_action"]=="strong_buy")
    buy=sum(1 for r in results if r["sug_action"]=="buy")
    hold=sum(1 for r in results if r["sug_action"]=="hold")
    watch=sum(1 for r in results if r["sug_action"]=="watch")
    avoid=sum(1 for r in results if r["sug_action"]=="avoid")
    critical=sum(1 for r in results if r["score"]<40)
    print(f"  🔥强烈买入:{sb} 🟢逢低买入:{buy} 🟡持有:{hold} ⚪观望:{watch} 🔴回避:{avoid} 🚨触发卖出:{critical}")
    
    md_path, html_path, email_html_path = generate_report(results, today, output_dir, hist_scores)
    
    json_path = os.path.join(output_dir, f"SS增强版评分_{today}.json")
    with open(json_path, "w") as f:
        json.dump({"date":today,"model":"SS with GLM V10","total":len(results),"results":results},f,ensure_ascii=False,indent=2)
    
    if send_mail:
        print("\n[邮件] 发送报告...")
        send_email(md_path, email_html_path, html_path, today, recipient)
    
    print(f"\n{'='*50}")
    for r in results[:5]:
        if r["sug_action"] in ("strong_buy","buy"):
            print(f"  {r['code']} {r['name']}: {r['score']}分 {r['suggestion']}")
    
    return results

if __name__ == "__main__":
    # 项目目录（脚本所在目录）
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = os.path.join(PROJECT_DIR, "output")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --regen-only：跳过数据抓取，仅从缓存重新评分和生成报告
    use_cache = "--regen-only" in sys.argv
    flags = {"--regen-only", "--no-mail", "--mail"}
    args = [a for a in sys.argv[1:] if a not in flags]

    # 股票代码文件：优先命令行参数，其次项目目录
    default_codes = os.path.join(PROJECT_DIR, "uploaded-stock-codes.txt")
    codes_file = args[0] if args else default_codes
    with open(codes_file) as f: codes = [line.strip() for line in f if line.strip()]

    # 收件人：命令行第二个参数
    recipient = args[1] if len(args) > 1 else None

    # --no-mail：不发送邮件（regen-only 默认不发）
    send_mail = not use_cache and "--no-mail" not in sys.argv
    if use_cache and "--mail" in sys.argv:
        send_mail = True

    # 输出目录：项目下的 output/
    run_daily_scoring(codes, output_dir=OUTPUT_DIR, recipient=recipient,
                      send_mail=send_mail, use_cache=use_cache)
