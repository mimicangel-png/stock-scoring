#!/usr/bin/env python3
"""
V11 组合级交易模拟器
=====================
完整模拟"买入→持仓→卖出"全流程，输出每笔交易的详细信息。

买入信号：截面排名 top N + 不在已有持仓 + 不在卖出信号中
卖出信号：止损 / 信号走弱 / 时间退出 / 止盈
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import numpy as np


# ================================================================
# 数据结构
# ================================================================

@dataclass
class Trade:
    """一笔完整的交易"""
    code: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str              # stop_loss / signal_decay / time_exit / take_profit
    return_pct: float
    holding_days: int
    entry_percentile: float       # 买入时的截面排名百分位
    entry_ensemble_z: float       # 买入时的 ensemble z-score
    exit_percentile: Optional[float] = None


@dataclass
class Position:
    """持仓"""
    code: str
    entry_date: str
    entry_price: float
    entry_percentile: float
    shares: int = 100             # 简化：固定100股


# ================================================================
# 交易模拟器
# ================================================================

class TradeSimulator:
    """
    交易模拟器：逐日推进，根据评分生成买卖信号。

    Args:
        top_pct: 买入的截面排名阈值 (0.15 = top 15%)
        max_positions: 最大持仓数
        stop_loss_pct: 止损阈值 (固定止损模式)
        take_profit_pct: 止盈阈值 (正数，如 15 表示涨15%止盈)
        max_hold_days: 最长持有天数
        signal_decay_pct: 排名跌出此百分位触发卖出 (0.5 = 跌出前50%)
        stop_mode: "fixed" (统一止损) 或 "scored" (分级止损)
            scored 分级：top10%=12%, 10-20%=8%, 20-30%=5%
    """

    def __init__(
        self,
        top_pct: float = 0.15,
        max_positions: int = 20,
        stop_loss_pct: float = -8.0,
        take_profit_pct: float = 15.0,
        max_hold_days: int = 20,
        signal_decay_pct: float = 0.5,
        stop_mode: str = "scored",
    ):
        self.top_pct = top_pct
        self.max_positions = max_positions
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_hold_days = max_hold_days
        self.signal_decay_pct = signal_decay_pct
        self.stop_mode = stop_mode

    def run(
        self,
        daily_scores: Dict[str, Dict[str, Dict]],     # {date: {code: {percentile, ensemble_z, ...}}}
        daily_prices: Dict[str, Dict[str, float]],     # {date: {code: close_price}}
        start_date: str = None,
        end_date: str = None,
    ) -> Tuple[List[Trade], List[Dict]]:
        """
        运行交易模拟。

        Returns:
            trades: 完成的交易列表
            daily_nav: 每日净值 [{date, nav, cash, positions_value, n_positions}]
        """
        positions: Dict[str, Position] = {}
        trades: List[Trade] = []
        daily_nav: List[Dict] = []

        all_dates = sorted(daily_scores.keys())
        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]

        cash = 1_000_000  # 初始资金
        initial_capital = cash

        for date in all_dates:
            prices = daily_prices.get(date, {})
            scores = daily_scores.get(date, {})

            if not scores:
                continue

            # ====== 1. 检查卖出 ======
            for code in list(positions.keys()):
                pos = positions[code]
                exit_signal = self._check_exit(pos, date, scores, prices)
                if exit_signal:
                    exit_price = prices.get(code, pos.entry_price)
                    holding_days = self._days_between(pos.entry_date, date)
                    return_pct = (exit_price / pos.entry_price - 1) * 100

                    trades.append(Trade(
                        code=code,
                        entry_date=pos.entry_date,
                        entry_price=pos.entry_price,
                        exit_date=date,
                        exit_price=exit_price,
                        exit_reason=exit_signal,
                        return_pct=round(return_pct, 2),
                        holding_days=holding_days,
                        entry_percentile=pos.entry_percentile,
                        entry_ensemble_z=0.0,
                        exit_percentile=scores.get(code, {}).get("percentile"),
                    ))

                    # 回笼资金
                    cash += pos.shares * exit_price
                    del positions[code]

            # ====== 2. 生成买入信号 ======
            buy_candidates = self._generate_buy_signals(scores, positions, prices)
            available_slots = self.max_positions - len(positions)

            for code, sr in buy_candidates[:available_slots]:
                buy_price = prices.get(code)
                if buy_price is None or buy_price <= 0:
                    continue
                if cash < buy_price * 100:
                    continue

                positions[code] = Position(
                    code=code,
                    entry_date=date,
                    entry_price=buy_price,
                    entry_percentile=sr.get("percentile", 0),
                )
                cash -= 100 * buy_price

            # ====== 3. 计算当日净值 ======
            positions_value = sum(
                pos.shares * prices.get(code, pos.entry_price)
                for code, pos in positions.items()
            )
            daily_nav.append({
                "date": date,
                "nav": cash + positions_value,
                "cash": cash,
                "positions_value": positions_value,
                "n_positions": len(positions),
            })

        return trades, daily_nav

    def _generate_buy_signals(
        self,
        scores: Dict[str, Dict],
        positions: Dict[str, Position],
        prices: Dict[str, float],
    ) -> List[tuple]:
        """生成买入候选"""
        candidates = []
        for code, sr in scores.items():
            if code in positions:
                continue
            percentile = sr.get("percentile", 1.0)
            if percentile < self.top_pct:
                # 有可用价格才纳入候选
                if prices.get(code, 0) > 0:
                    candidates.append((code, sr))

        # 按排名升序（排名越好越靠前）
        candidates.sort(key=lambda x: x[1].get("percentile", 1.0))
        return candidates

    def _check_exit(
        self,
        pos: Position,
        date: str,
        scores: Dict[str, Dict],
        prices: Dict[str, float],
    ) -> Optional[str]:
        """检查是否触发退出信号"""
        current_price = prices.get(pos.code)
        if current_price is None or current_price <= 0:
            return None

        loss_pct = (current_price / pos.entry_price - 1) * 100

        # 优先级1：止损（按模式）
        if self.stop_mode == "scored":
            # 分级止损 — 评分越高的股票容忍度越大
            pct = pos.entry_percentile
            if pct < 0.10:
                stop = -12
            elif pct < 0.20:
                stop = -8
            else:
                stop = -5
            if loss_pct <= stop:
                return "stop_loss"
        else:
            # 固定止损
            if loss_pct <= self.stop_loss_pct:
                return "stop_loss"

        # 优先级2：止盈
        if loss_pct >= self.take_profit_pct:
            return "take_profit"

        # 优先级3：信号走弱
        sr = scores.get(pos.code, {})
        if sr.get("percentile", 0) > self.signal_decay_pct:
            return "signal_decay"

        # 优先级4：时间退出
        days_held = self._days_between(pos.entry_date, date)
        if days_held >= self.max_hold_days:
            return "time_exit"

        return None

    @staticmethod
    def _days_between(d1: str, d2: str) -> int:
        """计算两个日期之间的天数"""
        try:
            dt1 = datetime.strptime(d1, "%Y-%m-%d")
            dt2 = datetime.strptime(d2, "%Y-%m-%d")
            return (dt2 - dt1).days
        except Exception:
            return 0
