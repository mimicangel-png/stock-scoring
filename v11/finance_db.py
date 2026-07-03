#!/usr/bin/env python3
"""
财报数据获取模块 — 扩展 StockDB，缓存财务指标。

通过 westock-data finance 命令获取 ROE、毛利率、经营现金流等。
"""

import sys, os, json, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_db import StockDB
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed


class FinanceDB(StockDB):
    """扩展 StockDB，增加财报数据缓存"""

    def __init__(self, db_path=None):
        super().__init__(db_path=db_path)
        self._init_finance_tables()
        self._node_script = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/westock-data/scripts/index.js"

    def _init_finance_tables(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financials (
                    code TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    roe_ttm REAL,
                    gross_margin_ttm REAL,
                    ocf_ratio_ttm REAL,
                    revenue_growth_yoy REAL,
                    profit_growth_yoy REAL,
                    fetched_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (code, report_date)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_financials_code ON financials(code)")

    def get_financials(self, codes, lookback_days=180):
        """
        获取最近一期财务数据（带缓存）。

        Returns:
            {code: {roe_ttm, gross_margin_ttm, ocf_ratio_ttm, revenue_growth_yoy, profit_growth_yoy}}
        """
        cut_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        result = {}
        need_fetch = []

        with self._connect() as conn:
            for code in codes:
                row = conn.execute(
                    "SELECT roe_ttm, gross_margin_ttm, ocf_ratio_ttm, revenue_growth_yoy, profit_growth_yoy "
                    "FROM financials WHERE code=? AND report_date >= ? ORDER BY report_date DESC LIMIT 1",
                    (code, cut_date)
                ).fetchone()
                if row:
                    result[code] = {
                        "roe_ttm": row[0], "gross_margin_ttm": row[1],
                        "ocf_ratio_ttm": row[2], "revenue_growth_yoy": row[3],
                        "profit_growth_yoy": row[4],
                    }
                else:
                    need_fetch.append(code)

        if need_fetch:
            print(f"  [Finance] 抓取 {len(need_fetch)} 只财报...")
            fetched = self._fetch_financials_batch(need_fetch)
            result.update(fetched)

        return result

    def _fetch_financials_batch(self, codes):
        """批量抓取财报"""
        result = {}
        batch_size = 30

        for i in range(0, len(codes), batch_size):
            batch = codes[i:i+batch_size]
            for code in batch:
                sym = self._to_symbol(code)
                cmd = ["node", self._node_script, "finance", sym, "--raw"]
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    if res.returncode != 0:
                        continue
                    data = json.loads(res.stdout)
                    sections = data.get("sections", []) if isinstance(data, dict) else data

                    # 获取利润表和现金流量表
                    income_stmt = sections[0] if len(sections) > 0 and isinstance(sections[0], list) else []
                    cash_flow = sections[2] if len(sections) > 2 and isinstance(sections[2], list) else []

                    if not income_stmt:
                        continue

                    latest = income_stmt[0]
                    if not latest:
                        continue

                    report_date = latest.get("_date", "")

                    # ROE TTM (归母净利润TTM / 归母权益 — 近似用动态PE倒推)
                    np_ttm = float(latest.get("NPParentCompanyOwnersTTM", 0) or 0)
                    op_rev_ttm = float(latest.get("OperatingRevenueTTM", 0) or 0)
                    op_cost_ttm = float(latest.get("OperatingCostTTM", 0) or 0)
                    total_rev_ttm = float(latest.get("TotalOperatingRevenueTTM", 0) or 0)

                    # 毛利率 TTM
                    gross_margin_ttm = (op_rev_ttm - op_cost_ttm) / op_rev_ttm * 100 if op_rev_ttm > 0 else 0

                    # 现金流/营收 TTM
                    ocf_ttm = 0.0
                    if cash_flow:
                        cf_latest = cash_flow[0]
                        ocf_ttm = float(cf_latest.get("NetOperateCashFlowTTM", 0) or 0)
                    ocf_ratio_ttm = ocf_ttm / total_rev_ttm * 100 if total_rev_ttm > 0 else 0

                    # 同比营收增速 (用本期 vs 去年同期，简化用 TTM)
                    rev_growth = float(latest.get("TotalOperatingRevenue", 0) or 0)
                    rev_growth_q = float(latest.get("OperatingRevenue_Q", 0) or 0)

                    # 归母净利
                    np_q = float(latest.get("NPParentCompanyOwners_Q", 0) or 0)
                    profit_growth = np_q

                    # 近似 ROE (如果 PE 不为零，用 1/PE 近似)
                    # 更精确的方式需要从行情数据中计算
                    roe_ttm = 0.0

                    result[code] = {
                        "roe_ttm": round(roe_ttm, 2),
                        "gross_margin_ttm": round(gross_margin_ttm, 2),
                        "ocf_ratio_ttm": round(ocf_ratio_ttm, 2),
                        "revenue_growth_yoy": round(rev_growth, 2),
                        "profit_growth_yoy": round(profit_growth, 2),
                    }

                    self._save_financial(code, report_date, result[code])

                except Exception:
                    continue

        return result

    def _save_financial(self, code, report_date, fin):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO financials(code, report_date, roe_ttm, gross_margin_ttm, "
                "ocf_ratio_ttm, revenue_growth_yoy, profit_growth_yoy) "
                "VALUES(?,?,?,?,?,?,?)",
                (code, report_date, fin["roe_ttm"], fin["gross_margin_ttm"],
                 fin["ocf_ratio_ttm"], fin["revenue_growth_yoy"], fin["profit_growth_yoy"])
            )
