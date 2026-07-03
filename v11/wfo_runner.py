#!/usr/bin/env python3
"""
V11 Purge Gap Walk-Forward 回测执行器
=======================================
严格的时间切分 + Purge 间隔 + 多轮滚动验证。

窗口配置：
  train: 252天 (1年)
  purge: 5天  (防止 T+10 标签泄露)
  test:  126天 (半年)
  step:  126天

每轮：train → 训练 GLM → test → 因子评分 → 交易模拟
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from typing import Dict, List
from collections import defaultdict

from v11.factor_engine import (
    compute_all_factors, compute_factor_ic,
    FactorICTracker, FactorGraveyard, FACTOR_NAMES,
)
from v11.glm_model import MultiPeriodGLM, GLM_CONFIG
from v11.trade_sim import TradeSimulator, Trade


# ================================================================
# 窗口生成
# ================================================================

def generate_wfo_windows(
    all_dates: List[str],
    train_size: int = 252,
    test_size: int = 126,
    purge_gap: int = 5,
    min_train: int = 60,
) -> List[Dict]:
    """
    生成 Walk-Forward 窗口列表。

    Returns:
        [{train: [...], purge: [...], test: [...]}, ...]
    """
    windows = []
    start = 0
    while start + train_size + purge_gap + test_size <= len(all_dates):
        train_end = start + train_size
        test_start = train_end + purge_gap
        test_end = test_start + test_size

        windows.append({
            "train": all_dates[start:train_end],
            "purge": all_dates[train_end:test_start],
            "test": all_dates[test_start:test_end],
            "window_idx": len(windows),
        })

        start += test_size  # 每次前进一个测试窗口

    return windows


# ================================================================
# WFO Runner
# ================================================================

class WFORunner:
    """
    Purge Gap WFO 回测执行器。

    Usage:
        runner = WFORunner()
        report = runner.run(snapshots, forward_returns, codes)
    """

    def __init__(
        self,
        train_size: int = 252,
        test_size: int = 126,
        purge_gap: int = 5,
        top_pct: float = 0.15,
        max_positions: int = 20,
    ):
        self.train_size = train_size
        self.test_size = test_size
        self.purge_gap = purge_gap
        self.top_pct = top_pct
        self.max_positions = max_positions

        # 追踪器
        self.factor_trackers: Dict[str, FactorICTracker] = {
            name: FactorICTracker(name) for name in FACTOR_NAMES
        }
        self.graveyard = FactorGraveyard()
        self.glm = MultiPeriodGLM()
        self.simulator = TradeSimulator(
            top_pct=top_pct,
            max_positions=max_positions,
        )

        # 结果
        self.all_trades: List[Trade] = []
        self.all_daily_nav: List[Dict] = []
        self.window_results: List[Dict] = []
        self.factor_ic_history: Dict[str, List[Dict]] = defaultdict(list)

    def run(
        self,
        klines_all: Dict[str, List[dict]],
        extra_all: Dict[str, dict],
        fund_flow_all: Dict[str, dict] = None,
        event_all: Dict[str, list] = None,
        sector_strength: Dict[str, dict] = None,
        forward_returns: Dict[str, Dict[str, Dict[int, float]]] = None,
    ) -> Dict:
        """
        执行完整 WFO 回测。

        Args:
            klines_all: {code: [kline_dicts]}
            extra_all: {code: {行情快照}}
            fund_flow_all: {code: {资金流}}
            event_all: {code: [公告]}
            sector_strength: {sector: {rsi, momentum}}
            forward_returns: {date: {code: {horizon: ret}}} (预计算)

        Returns:
            完整回测报告
        """
        if fund_flow_all is None:
            fund_flow_all = {}
        if event_all is None:
            event_all = {}
        if sector_strength is None:
            sector_strength = {}

        # 收集所有日期
        all_dates_set = set()
        for klines in klines_all.values():
            for k in klines:
                all_dates_set.add(k["date"])
        all_dates = sorted(all_dates_set)

        # 生成窗口
        windows = generate_wfo_windows(
            all_dates,
            train_size=self.train_size,
            test_size=self.test_size,
            purge_gap=self.purge_gap,
            min_train=60,
        )
        print(f"[WFO] 共 {len(windows)} 个验证窗口")
        print(f"  日期范围: {all_dates[0]} ~ {all_dates[-1]}")

        # ====== 逐窗口执行 ======
        for w in windows:
            print(f"\n{'='*50}")
            print(f"[WFO] 窗口 {w['window_idx']+1}/{len(windows)}")
            print(f"  Train: {w['train'][0]} ~ {w['train'][-1]}")
            print(f"  Purge: {w['purge'][0]} ~ {w['purge'][-1]}")
            print(f"  Test:  {w['test'][0]} ~ {w['test'][-1]}")

            result = self._run_window(
                w, klines_all, extra_all, fund_flow_all,
                event_all, sector_strength, forward_returns,
            )
            self.window_results.append(result)

        # ====== 汇总 ======
        report = self._build_report(windows)
        return report

    def _run_window(self, window, klines_all, extra_all,
                    fund_flow_all, event_all, sector_strength, forward_returns):
        """执行单个 WFO 窗口"""

        # 1. 在 train 窗口计算每日因子值
        print("  [1/5] 计算 Train 窗口因子...")
        train_factors: Dict[str, Dict[str, Dict[str, float]]] = {}
        for date in window["train"]:
            # 构建截至该日的快照
            snapshot_klines = self._build_snapshot(date, klines_all)
            if len(snapshot_klines) < 30:
                continue
            factors = compute_all_factors(
                snapshot_klines, extra_all, fund_flow_all,
                event_all, sector_strength, today_str=date,
            )
            if factors:
                train_factors[date] = factors

        # 2. 记录各因子 IC
        print("  [2/5] 记录因子 IC...")
        for date in window["train"]:
            fvals = train_factors.get(date, {})
            fwd = forward_returns.get(date, {}) if forward_returns else {}

            for horizon in [1, 5, 10]:
                ics = compute_factor_ic(fvals, fwd, horizon=horizon)
                for fname, ic in ics.items():
                    if fname in self.factor_trackers:
                        self.factor_trackers[fname].record_ic(horizon, ic)

        # 3. 因子墓地评估
        removed = self.graveyard.evaluate(self.factor_trackers)
        if removed:
            print(f"  ⚠️ 因子移入墓地: {removed}")
        active_factors = self.graveyard.get_active_names()
        print(f"  活跃因子: {len(active_factors)}/{len(FACTOR_NAMES)}")

        # 4. 训练 GLM
        print("  [3/5] 训练 GLM...")
        train_end_date = window["test"][0]  # test开始的前一天作为训练截止
        glm_results = self.glm.train(
            train_factors, forward_returns or {},
            train_end_date, active_factors,
        )
        for period, res in glm_results.items():
            print(f"    {period}: n={res['train_n']}, IC={res['train_ic']:.4f}")

        # 5. 在 test 窗口评分 + 模拟交易
        print("  [4/5] Test 窗口评分 + 交易模拟...")
        test_factors = {}
        test_scores = {}
        test_prices = {}

        for date in window["test"]:
            snapshot_klines = self._build_snapshot(date, klines_all)
            if len(snapshot_klines) < 30:
                continue

            factors = compute_all_factors(
                snapshot_klines, extra_all, fund_flow_all,
                event_all, sector_strength, today_str=date,
            )
            if not factors:
                continue

            test_factors[date] = factors
            scores = self.glm.predict(factors)
            test_scores[date] = scores

            # 价格
            prices = {}
            for code, klines in snapshot_klines.items():
                if klines:
                    prices[code] = klines[-1]["close"]
            test_prices[date] = prices

        # 运行交易模拟
        trades, nav = self.simulator.run(
            test_scores, test_prices,
            start_date=window["test"][0],
            end_date=window["test"][-1],
        )

        self.all_trades.extend(trades)
        self.all_daily_nav.extend(nav)

        # 统计
        win_trades = [t for t in trades if t.return_pct > 0]
        win_rate = len(win_trades) / len(trades) if trades else 0
        avg_return = np.mean([t.return_pct for t in trades]) if trades else 0

        print(f"  [5/5] 结果: {len(trades)}笔交易, 胜率{win_rate:.1%}, 平均{trades and avg_return:.1f}%")

        return {
            "window_idx": window["window_idx"],
            "train_dates": [window["train"][0], window["train"][-1]],
            "test_dates": [window["test"][0], window["test"][-1]],
            "n_trades": len(trades),
            "win_rate": win_rate,
            "avg_return": avg_return,
            "active_factors": len(active_factors),
            "glm_results": glm_results,
        }

    def _build_snapshot(self, date: str, klines_all: Dict[str, List]) -> Dict[str, List]:
        """构建截至某日的K线快照（只用 <= date 的数据）"""
        snapshot = {}
        for code, klines in klines_all.items():
            filtered = [k for k in klines if k["date"] <= date]
            if filtered:
                snapshot[code] = filtered
        return snapshot

    def _build_report(self, windows) -> Dict:
        """构建汇总报告"""
        if not self.all_trades:
            return {"error": "no trades executed"}

        returns = [t.return_pct for t in self.all_trades]
        wins = [r for r in returns if r > 0]

        # 组合收益曲线
        if self.all_daily_nav:
            navs = [n["nav"] for n in self.all_daily_nav]
            initial_nav = self.all_daily_nav[0]["nav"]
            daily_returns = [navs[i] / navs[i-1] - 1 for i in range(1, len(navs))]

            annual_return = np.mean(daily_returns) * 252 if daily_returns else 0
            sharpe = np.mean(daily_returns) / max(0.001, np.std(daily_returns)) * np.sqrt(252) if daily_returns else 0

            # 最大回撤
            peak = np.maximum.accumulate(navs)
            dd = (navs - peak) / peak
            max_dd = float(np.min(dd))

            calmar = annual_return / abs(max_dd) if max_dd != 0 else 0
        else:
            annual_return = sharpe = max_dd = calmar = 0

        return {
            "summary": {
                "n_windows": len(windows),
                "total_trades": len(self.all_trades),
                "win_rate": round(len(wins) / len(self.all_trades), 4) if self.all_trades else 0,
                "avg_return": round(np.mean(returns), 2),
                "median_return": round(np.median(returns), 2),
                "profit_factor": round(sum(r for r in returns if r > 0) / abs(sum(r for r in returns if r < 0)), 2) if any(r < 0 for r in returns) else 99,
                "annual_return": round(annual_return, 4),
                "sharpe": round(sharpe, 2),
                "max_drawdown": round(max_dd, 4),
                "calmar": round(calmar, 2),
            },
            "window_results": self.window_results,
            "factor_status": self.graveyard.status_report(),
            "factor_icir": {
                name: tracker.get_weighted_icir()
                for name, tracker in self.factor_trackers.items()
            },
            "trades": [
                {
                    "code": t.code,
                    "entry_date": t.entry_date,
                    "exit_date": t.exit_date,
                    "return_pct": t.return_pct,
                    "holding_days": t.holding_days,
                    "exit_reason": t.exit_reason,
                    "entry_percentile": t.entry_percentile,
                }
                for t in self.all_trades
            ],
            "daily_nav": self.all_daily_nav,
        }
