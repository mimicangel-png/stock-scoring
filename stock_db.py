#!/usr/bin/env python3
"""
股票数据本地 SQLite 缓存模块。
================================
一次性解决三个痛点：
  1. 重复抓取 — K线/资金流/事件每次回测都重拉，浪费时间和 Token
  2. 失败遗漏 — 单只股票 API 失败静默跳过，后续分析缺数据
  3. 多脚本复用 — scoring_engine、backtest_* 各自独立抓取，无共享

用法（替换原有 fetch_ 函数）：
  from stock_db import StockDB
  db = StockDB()                          # 自动建库建表
  klines = db.get_klines(codes, days=130) # 先查本地，再补增量
  extra  = db.get_extra_info(codes)       # 按日期缓存的快照
  flows  = db.get_fund_flows(codes)       # 主力资金流
  events = db.get_events(codes, days=14)  # 公告事件

失败重试：
  db.retry_failed()                       # 重试所有 fetch_log 中失败的请求
  db.stats()                              # 打印缓存储量统计
"""

import os, json, sqlite3, urllib.request, subprocess
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# 数据库文件放在项目 output/ 目录
DB_PATH = None  # 延迟初始化


def _get_db_path():
    global DB_PATH
    if DB_PATH is None:
        import __main__
        script_dir = os.path.dirname(os.path.abspath(__main__.__file__)) if hasattr(__main__, '__file__') else os.getcwd()
        output_dir = os.path.join(script_dir, "output")
        os.makedirs(output_dir, exist_ok=True)
        DB_PATH = os.path.join(output_dir, "stock_cache.db")
    return DB_PATH


# ================================================================
# Schema
# ================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    code TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS extra_info (
    code TEXT NOT NULL, date TEXT NOT NULL,
    name TEXT, price REAL, change_pct REAL,
    pe_ttm REAL, pb REAL, mcap REAL, turnover REAL, vol_ratio REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS fund_flows (
    code TEXT NOT NULL, date TEXT NOT NULL,
    main_net_5d REAL, main_net_20d REAL,
    inflow_rate REAL, jumbo_net REAL, main_net_today REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS events (
    code TEXT NOT NULL, date TEXT NOT NULL,
    title TEXT NOT NULL, event_type TEXT, base_score REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (code, date, title)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    code TEXT NOT NULL, date TEXT NOT NULL,
    data_type TEXT NOT NULL,
    status TEXT DEFAULT 'failed', retry_count INTEGER DEFAULT 0,
    error_msg TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (code, date, data_type)
);

CREATE INDEX IF NOT EXISTS idx_klines_code ON klines(code);
CREATE INDEX IF NOT EXISTS idx_klines_date ON klines(date);
CREATE INDEX IF NOT EXISTS idx_events_code ON events(code);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
CREATE INDEX IF NOT EXISTS idx_fetch_log_status ON fetch_log(status);
"""


# ================================================================
# StockDB class
# ================================================================

class StockDB:
    def __init__(self, db_path=None):
        self.db_path = db_path or _get_db_path()
        self._init_db()
        # Node.js 脚本路径
        self._node_script = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"
        # 并发线程数
        self._workers = 20

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @staticmethod
    def _to_symbol(code):
        return f"sh{code}" if code.startswith(("6","9")) else f"sz{code}"

    # ================================================================
    # K-line
    # ================================================================

    def get_klines(self, codes, days=130):
        """
        获取K线数据（带本地缓存 + 增量更新）。
        返回格式与 fetch_kline_batch 完全兼容：
          {code: [{date, open, close, high, low, volume}, ...]}
        """
        today = datetime.now().strftime("%Y-%m-%d")
        all_klines = {}
        missing = []

        # 判断最新交易日（周末回退到周五）
        now = datetime.now()
        wd = now.weekday()  # 0=Mon ... 6=Sun
        if wd == 5:  # Sat -> Fri
            latest_td = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        elif wd == 6:  # Sun -> Fri
            latest_td = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        else:
            latest_td = today

        # 1. 先从本地读
        with self._connect() as conn:
            for code in codes:
                rows = conn.execute(
                    "SELECT date, open, high, low, close, volume FROM klines WHERE code=? ORDER BY date",
                    (code,)
                ).fetchall()
                if len(rows) >= days:
                    # 足够多，但需检查是否缺最新交易日
                    last_date = rows[-1][0]
                    if last_date >= latest_td:
                        # 缓存已是最新，直接用
                        all_klines[code] = [
                            {"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                            for r in rows[-days:]
                        ]
                    else:
                        # 缓存虽够130条，但缺最近交易日 → 增量抓取
                        all_klines[code] = [
                            {"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                            for r in rows
                        ]
                        missing.append(code)
                elif len(rows) > 0:
                    # 有部分数据，记录最后日期
                    last_date = rows[-1][0]
                    all_klines[code] = [
                        {"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                        for r in rows
                    ]
                    missing.append(code)
                else:
                    missing.append(code)

        # 2. 并发抓取缺失的
        if missing:
            self._fetch_klines_batch(missing, days, today, all_klines)

        # 3. 裁剪到 days 长度
        for code in list(all_klines.keys()):
            if len(all_klines[code]) > days:
                all_klines[code] = all_klines[code][-days:]

        return all_klines

    def _fetch_klines_batch(self, codes, days, today, all_klines):
        """并发抓取K线并写入DB"""
        print(f"  [DB] 增量抓取 {len(codes)} 只K线...")

        def fetch_one(code):
            sym = self._to_symbol(code)
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read().decode("utf-8"))
                klines = data.get("data",{}).get(sym,{}).get("qfqday",[]) or \
                         data.get("data",{}).get(sym,{}).get("day",[])
                return code, klines, None
            except Exception as e:
                return code, None, str(e)[:200]

        completed = 0
        with ThreadPoolExecutor(max_workers=self._workers) as ex:
            futures = {ex.submit(fetch_one, c): c for c in codes}
            for f in as_completed(futures):
                code, kls, err = f.result()
                completed += 1
                if completed % 30 == 0:
                    print(f"  K线: {completed}/{len(codes)}")

                if kls:
                    parsed = [{"date":k[0],"open":float(k[1]),"close":float(k[2]),
                               "high":float(k[3]),"low":float(k[4]),
                               "volume":float(k[5]) if len(k)>5 else 0} for k in kls]
                    # Merge with existing
                    existing = {r["date"]: r for r in all_klines.get(code, [])}
                    for r in parsed:
                        existing[r["date"]] = r
                    all_klines[code] = sorted(existing.values(), key=lambda x: x["date"])
                    # Write to DB
                    self._save_klines(code, parsed)
                    self._log_fetch(code, today, "klines", "ok")
                else:
                    self._log_fetch(code, today, "klines", "failed", err)

    def _save_klines(self, code, klines):
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO klines(code,date,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)",
                [(code, k["date"], k["open"], k["high"], k["low"], k["close"], k["volume"]) for k in klines]
            )

    # ================================================================
    # Extra Info (实时快照)
    # ================================================================

    def get_extra_info(self, codes, force_refresh=False):
        """获取实时行情快照

        Args:
            codes: 股票代码列表
            force_refresh: True=强制重新抓取（同一天重跑时用），False=按日期缓存
        """
        today = datetime.now().strftime("%Y-%m-%d")
        result = {}
        need_fetch = []

        if force_refresh:
            need_fetch = list(codes)
        else:
            with self._connect() as conn:
                for code in codes:
                    row = conn.execute(
                        "SELECT name,price,change_pct,pe_ttm,pb,mcap,turnover,vol_ratio FROM extra_info WHERE code=? AND date=?",
                        (code, today)
                    ).fetchone()
                    if row:
                        result[code] = {
                            "name": row[0], "price": row[1], "change_pct": row[2],
                            "pe_ttm": row[3], "pb": row[4], "mcap": row[5],
                            "turnover": row[6], "vol_ratio": row[7],
                        }
                    else:
                        need_fetch.append(code)

        if need_fetch:
            fetched = self._fetch_extra_info_batch(need_fetch, today)
            result.update(fetched)

        return result

    def _fetch_extra_info_batch(self, codes, today):
        """批量抓取实时行情并写入DB"""
        result = {}
        prefixed = [self._to_symbol(c) for c in codes]

        for i in range(0, len(prefixed), 60):
            batch = prefixed[i:i+60]
            url = "https://qt.gtimg.cn/q=" + ",".join(batch)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                resp = urllib.request.urlopen(req, timeout=10)
                for line in resp.read().decode("gbk").strip().split(";"):
                    if "=" not in line or '"' not in line: continue
                    vals = line.split('"')[1].split("~")
                    if len(vals) < 55: continue
                    code = line.split("=")[0].split("_")[-1][2:]
                    info = {
                        "name": vals[1], "price": float(vals[3]) if vals[3] else 0,
                        "change_pct": float(vals[32]) if vals[32] else 0,
                        "pe_ttm": float(vals[39]) if vals[39] else 0,
                        "pb": float(vals[46]) if vals[46] else 0,
                        "mcap": float(vals[44])*1e8 if vals[44] else 0,
                        "turnover": float(vals[38]) if vals[38] else 0,
                        "vol_ratio": float(vals[49]) if vals[49] else 0,
                    }
                    result[code] = info
                    self._save_extra(code, today, info)
                    self._log_fetch(code, today, "extra", "ok")
            except Exception as e:
                for c in batch:
                    self._log_fetch(c.strip("shsz"), today, "extra", "failed", str(e)[:200])

        return result

    def _save_extra(self, code, date, info):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO extra_info(code,date,name,price,change_pct,pe_ttm,pb,mcap,turnover,vol_ratio) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (code, date, info["name"], info["price"], info["change_pct"],
                 info["pe_ttm"], info["pb"], info["mcap"], info["turnover"], info["vol_ratio"])
            )

    # ================================================================
    # Fund Flows
    # ================================================================

    def get_fund_flows(self, codes):
        """获取主力资金流（缓存当天，日频不变）"""
        today = datetime.now().strftime("%Y-%m-%d")
        result = {}
        need_fetch = []

        with self._connect() as conn:
            for code in codes:
                row = conn.execute(
                    "SELECT main_net_5d,main_net_20d,inflow_rate,jumbo_net,main_net_today FROM fund_flows WHERE code=? AND date=?",
                    (code, today)
                ).fetchone()
                if row:
                    result[code] = dict(zip(
                        ["main_net_5d","main_net_20d","inflow_rate","jumbo_net","main_net_today"], row
                    ))
                else:
                    need_fetch.append(code)

        if need_fetch:
            fetched = self._fetch_fund_flows_batch(need_fetch, today)
            result.update(fetched)

        return result

    def _fetch_fund_flows_batch(self, codes, today):
        result = {}
        batch_size = 30
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i+batch_size]
            symbols = ",".join(self._to_symbol(c) for c in batch)
            cmd = ["node", self._node_script, "fund", "flow", symbols, "--raw"]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if res.returncode != 0: continue
                data = json.loads(res.stdout)
                if not isinstance(data, list): continue
                for item in data:
                    sym = item.get("symbol","")
                    if len(sym) < 6: continue
                    code = sym[2:]
                    try:
                        info = {
                            "main_net_5d": float(item.get("MainNetFlow5D",0)),
                            "main_net_20d": float(item.get("MainNetFlow20D",0)),
                            "inflow_rate": float(item.get("MainInflowCircRate",0)),
                            "jumbo_net": float(item.get("JumboNetFlow",0)),
                            "main_net_today": float(item.get("MainNetFlow",0)),
                        }
                    except (ValueError, TypeError): continue
                    result[code] = info
                    self._save_fund(code, today, info)
                    self._log_fetch(code, today, "fund_flow", "ok")
            except Exception as e:
                for c in batch:
                    self._log_fetch(c, today, "fund_flow", "failed", str(e)[:200])
        return result

    def _save_fund(self, code, date, info):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fund_flows(code,date,main_net_5d,main_net_20d,inflow_rate,jumbo_net,main_net_today) "
                "VALUES(?,?,?,?,?,?,?)",
                (code, date, info["main_net_5d"], info["main_net_20d"],
                 info["inflow_rate"], info["jumbo_net"], info["main_net_today"])
            )

    # ================================================================
    # Events
    # ================================================================

    def get_events(self, codes, days=14):
        """获取公告事件（按股票缓存，增量拉取）"""
        today = datetime.now().strftime("%Y-%m-%d")
        # 从今天往前 days 天
        cut_date = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
        result = defaultdict(list)
        need_fetch = set()

        with self._connect() as conn:
            for code in codes:
                rows = conn.execute(
                    "SELECT date,title,event_type,base_score FROM events WHERE code=? AND date>=?",
                    (code, cut_date)
                ).fetchall()
                if rows:
                    result[code] = [{"date":r[0],"title":r[1],"event_type":r[2],"base_score":r[3]} for r in rows]
                # 检查是否今天已经取过
                today_count = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE code=? AND date>=?",
                    (code, today)
                ).fetchone()[0]
                if today_count == 0:
                    need_fetch.add(code)

        if need_fetch:
            fetched = self._fetch_events_batch(list(need_fetch), days, today)
            for code, evts in fetched.items():
                result[code].extend(evts)

        return dict(result)

    def _fetch_events_batch(self, codes, days, today):
        import re
        result = defaultdict(list)
        # 事件关键词匹配（同 scoring_engine.py）
        EVENT_RULES = [
            ("减持", ["减持"], -15), ("预减", ["预减","亏损","净利润下降"], -18),
            ("解禁", ["解禁"], -10), ("增发", ["增发","配股","定增"], -6),
            ("关联交易", ["关联交易"], -3),
            ("预增", ["预增","扭亏为盈","业绩增长"], 20),
            ("重大合同", ["重大合同","中标","签订","框架协议"], 15),
            ("增持", ["增持"], 12), ("回购", ["回购"], 10),
            ("扩产", ["扩产","投产","产能"], 8), ("股权激励", ["股权激励","员工持股"], 6),
            ("分红", ["分红","权益分派","派息"], 5),
        ]

        batch_size = 30
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i+batch_size]
            symbols = ",".join(self._to_symbol(c) for c in batch)
            cmd = ["node", self._node_script, "notice", "list", symbols, "--limit", "20", "--raw"]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if res.returncode != 0: continue
                data = json.loads(res.stdout)
                items = data if isinstance(data, list) else data.get("sections", [[]])[0]
                for item in items:
                    sym = item.get("symbol","")
                    if len(sym) < 6: continue
                    code = sym[2:]
                    title = item.get("title","")
                    ts = item.get("time","")
                    if not title or not ts: continue
                    edate = ts[:10]
                    # 分类
                    event_type, base_score = None, 0
                    for et, kws, sc in EVENT_RULES:
                        if any(kw in title for kw in kws):
                            event_type, base_score = et, sc
                            break
                    result[code].append({"date":edate,"title":title,"event_type":event_type,"base_score":base_score})
                    self._save_event(code, edate, title, event_type, base_score)
                for c in batch:
                    self._log_fetch(c, today, "events", "ok")
            except Exception as e:
                for c in batch:
                    self._log_fetch(c, today, "events", "failed", str(e)[:200])
        return dict(result)

    def _save_event(self, code, date, title, event_type, base_score):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO events(code,date,title,event_type,base_score) VALUES(?,?,?,?,?)",
                (code, date, title, event_type, base_score)
            )

    # ================================================================
    # Fetch Log & Retry
    # ================================================================

    def _log_fetch(self, code, date, data_type, status, error_msg=None):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log(code,date,data_type,status,error_msg,updated_at) "
                "VALUES(?,?,?,?,?,datetime('now'))",
                (code, date, data_type, status, error_msg)
            )

    def retry_failed(self):
        """重试所有失败的抓取"""
        with self._connect() as conn:
            failed = conn.execute(
                "SELECT code, date, data_type, retry_count FROM fetch_log WHERE status='failed' AND retry_count<3"
            ).fetchall()

        if not failed:
            print("  ✅ 没有需要重试的失败记录")
            return

        print(f"  🔄 重试 {len(failed)} 条失败记录...")
        by_type = defaultdict(list)
        for code, date, dtype, retry in failed:
            by_type[dtype].append(code)

        for dtype, codes in by_type.items():
            codes = list(set(codes))
            today = datetime.now().strftime("%Y-%m-%d")
            if dtype == "klines":
                self._fetch_klines_batch(codes, 130, today, {})
            elif dtype == "extra":
                self._fetch_extra_info_batch(codes, today)
            elif dtype == "fund_flow":
                self._fetch_fund_flows_batch(codes, today)
            elif dtype == "events":
                self._fetch_events_batch(codes, 14, today)

        # 标记重试
        with self._connect() as conn:
            conn.execute(
                "UPDATE fetch_log SET retry_count=retry_count+1, updated_at=datetime('now') WHERE status='failed'"
            )

    def force_refresh(self, codes):
        """
        强制重新拉取指定股票的 K线 + 实时行情（不走缓存）。
        用于数据不完整时的重试补拉。
        返回 (fetched_klines, fetched_extra) 各为成功拉取的 code set。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        fetched_k = set()
        fetched_e = set()

        if codes:
            # 强制拉K线
            dummy = {}
            self._fetch_klines_batch(list(codes), 130, today, dummy)
            # 检查哪些成功写入了今天的数据
            with self._connect() as conn:
                for c in codes:
                    r = conn.execute(
                        "SELECT 1 FROM klines WHERE code=? AND date>=? LIMIT 1",
                        (c, today)
                    ).fetchone()
                    # K线可能今天没交易（停牌），看fetch_log是否ok
                    log = conn.execute(
                        "SELECT status FROM fetch_log WHERE code=? AND date=? AND data_type='klines' ORDER BY updated_at DESC LIMIT 1",
                        (c, today)
                    ).fetchone()
                    if log and log[0] == "ok":
                        fetched_k.add(c)

            # 强制拉行情
            self._fetch_extra_info_batch(list(codes), today)
            with self._connect() as conn:
                for c in codes:
                    r = conn.execute(
                        "SELECT 1 FROM extra_info WHERE code=? AND date=? LIMIT 1",
                        (c, today)
                    ).fetchone()
                    if r:
                        fetched_e.add(c)

        return fetched_k, fetched_e

    def check_data_freshness(self, codes):
        """
        检查所有股票的 K线 和 实时行情 是否为最新交易日数据。
        返回 dict:
          kline_latest: DB中K线最新日期
          extra_latest: DB中实时行情最新日期
          expected_td: 预期最新交易日（周末回退到周五）
          missing_klines: 缺最新K线的 code 列表
          missing_extra: 缺最新行情的 code 列表
          kline_counts: {date: count} 最近5个K线日期分布
          total_codes: 总股票数
          fresh: bool 是否全部新鲜
        """
        now = datetime.now()
        wd = now.weekday()
        if wd == 5:
            expected_td = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        elif wd == 6:
            expected_td = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        else:
            expected_td = now.strftime("%Y-%m-%d")

        with self._connect() as conn:
            # K线最新日期
            kline_latest = conn.execute("SELECT MAX(date) FROM klines").fetchone()[0] or "-"
            # 实时行情最新日期
            extra_latest = conn.execute("SELECT MAX(date) FROM extra_info").fetchone()[0] or "-"
            # K线日期分布（最近5天）
            rows = conn.execute(
                "SELECT date, COUNT(DISTINCT code) FROM klines GROUP BY date ORDER BY date DESC LIMIT 5"
            ).fetchall()
            kline_counts = {r[0]: r[1] for r in rows}
            # 缺最新K线的股票
            missing_klines = [
                c for c in codes
                if conn.execute(
                    "SELECT 1 FROM klines WHERE code=? AND date=? LIMIT 1", (c, kline_latest)
                ).fetchone() is None
            ]
            # 缺最新行情的股票
            missing_extra = [
                c for c in codes
                if conn.execute(
                    "SELECT 1 FROM extra_info WHERE code=? AND date=? LIMIT 1", (c, extra_latest)
                ).fetchone() is None
            ]

        fresh = (kline_latest >= expected_td) and len(missing_klines) == 0 and len(missing_extra) == 0
        return {
            "kline_latest": kline_latest,
            "extra_latest": extra_latest,
            "expected_td": expected_td,
            "missing_klines": missing_klines,
            "missing_extra": missing_extra,
            "kline_counts": kline_counts,
            "total_codes": len(codes),
            "fresh": fresh,
        }

    def stats(self):
        """打印缓存储量统计"""
        with self._connect() as conn:
            kc = conn.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
            ec = conn.execute("SELECT COUNT(*) FROM extra_info").fetchone()[0]
            fc = conn.execute("SELECT COUNT(*) FROM fund_flows").fetchone()[0]
            evc = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            fok = conn.execute("SELECT COUNT(*) FROM fetch_log WHERE status='ok'").fetchone()[0]
            ffail = conn.execute("SELECT COUNT(*) FROM fetch_log WHERE status='failed'").fetchone()[0]

            # 股票数
            stocks = conn.execute("SELECT COUNT(DISTINCT code) FROM klines").fetchone()[0]
            last_kline = conn.execute("SELECT MAX(date) FROM klines").fetchone()[0] or "-"

        print(f"\n  📊 StockDB 缓存储量:")
        print(f"     K线: {kc:,} 条 ({stocks}只股票, 最新 {last_kline})")
        print(f"     快照: {ec:,} 条")
        print(f"     资金流: {fc:,} 条")
        print(f"     事件: {evc:,} 条")
        print(f"     抓取成功: {fok:,} | 失败: {ffail} ({'需重试' if ffail > 0 else '全部OK'})")

        db_size = os.path.getsize(self.db_path) / (1024*1024)
        print(f"     DB文件: {db_size:.1f} MB")


# ================================================================
# Convenience: drop-in replacement
# ================================================================

def build_db():
    """创建或获取全局 StockDB 实例"""
    return StockDB()
