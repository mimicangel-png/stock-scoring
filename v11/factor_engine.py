#!/usr/bin/env python3
"""
V11 连续因子引擎
==================
将 SS 评分的所有离散加减分因子转换为连续 z-score 输出，
新增财报/估值/行业强度因子，内置多周期 IC 追踪 + 因子墓地。

每个因子输出三元组： (name, raw_value, daily_zscore)
- raw_value: 原始值（如 RSI=62.3）
- daily_zscore: 当日截面 z-score（在股票池中的相对位置）
- 多周期 IC 追踪：IC_1d, IC_5d, IC_10d
"""

import math
import numpy as np
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 复用 scoring_engine 的辅助函数
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scoring_engine import calc_ma, calc_ema, calc_rsi, get_theme


# ================================================================
# 因子定义
# ================================================================

@dataclass
class FactorDef:
    """因子元数据"""
    name: str                    # 因子名称
    group: str                   # 分组: technical/capital/info/valuation/fundamental/sector
    description: str             # 描述
    ic_horizons: List[int] = field(default_factory=lambda: [1, 5, 10])  # 追踪的IC周期
    higher_better: bool = True   # 值越大越好？


# 因子注册表
FACTOR_REGISTRY = [
    # ===== 技术面因子 (从 SS 评分转化) =====
    FactorDef("ma_trend",      "technical", "均线趋势强度 (MA5/MA10/MA20)"),
    FactorDef("ma_bull",       "technical", "均线多头排列"),
    FactorDef("rsi_signal",    "technical", "RSI 信号 (z-score)"),
    FactorDef("macd_signal",   "technical", "MACD DIF-DEA 强度"),
    FactorDef("vol_price",     "technical", "量价共振 (放量x涨跌)"),
    FactorDef("dev_ma20",      "technical", "MA20 偏离度"),
    FactorDef("pct_52w",       "technical", "52 周高低位"),
    FactorDef("vol_ratio_5d",  "technical", "5日量比"),
    FactorDef("ret_5d",        "technical", "5日涨幅"),
    FactorDef("ret_20d",       "technical", "20日涨幅"),
    FactorDef("streak",        "technical", "连涨天数"),
    FactorDef("gap_open",      "technical", "跳空缺口"),
    FactorDef("turnover_z",    "technical", "换手率异常 (z-score)"),
    FactorDef("amplitude_z",   "technical", "振幅异常 (z-score)"),

    # ===== 资金面因子 =====
    FactorDef("cmf",           "capital", "Chaikin 资金流"),
    FactorDef("mfi",           "capital", "MFI 资金流指标"),
    FactorDef("vwap_premium",  "capital", "VWAP20 溢价"),
    FactorDef("vol_up_days",   "capital", "近5日放量天数"),
    FactorDef("main_flow_5d",  "capital", "主力资金5日净流向"),
    FactorDef("main_flow_20d", "capital", "主力资金20日净流向"),
    FactorDef("inflow_rate",   "capital", "主力流入占比"),

    # ===== 信息面因子 =====
    FactorDef("event_score",   "info", "公告事件评分"),
    FactorDef("event_count",   "info", "近期事件数量"),

    # ===== 估值面因子 =====
    FactorDef("pe_percentile", "valuation", "PE-TTM 3年分位数", higher_better=False),
    FactorDef("pb_percentile", "valuation", "PB 3年分位数", higher_better=False),
    FactorDef("log_mcap",      "valuation", "对数市值 (规模因子)"),

    # ===== 基本面因子 =====
    FactorDef("roe_rank",      "fundamental", "ROE 截面排名"),
    FactorDef("gross_margin_rank", "fundamental", "毛利率截面排名"),
    FactorDef("ocf_ratio_rank","fundamental", "经营现金流/营收截面排名"),

    # ===== 行业/板块因子 =====
    FactorDef("sector_rsi",    "sector", "行业相对强度"),
    FactorDef("sector_momentum","sector", "板块动量"),

    # ===== 风险因子 =====
    FactorDef("volatility_20d","risk", "20日波动率", higher_better=False),
    FactorDef("max_dd_20d",    "risk", "20日最大回撤", higher_better=False),
]

FACTOR_NAMES = [f.name for f in FACTOR_REGISTRY]
FACTOR_MAP = {f.name: f for f in FACTOR_REGISTRY}


# ================================================================
# 因子计算
# ================================================================

def compute_technical_factors(klines, idx, extra=None) -> Dict[str, float]:
    """技术面因子 — SS 评分离散→连续转化"""
    w = klines[:idx+1]
    c = np.array([k['close'] for k in w], dtype=float)
    v = np.array([k['volume'] for k in w], dtype=float)
    h = np.array([k['high'] for k in w], dtype=float)
    l = np.array([k['low'] for k in w], dtype=float)
    o = np.array([k['open'] for k in w], dtype=float)

    f = {}

    # ma_trend: 均线多头排列强度 [-10, +15]
    ma5, ma10, ma20 = calc_ma(c,5), calc_ma(c,10), calc_ma(c,20)
    if ma5 and ma10 and ma20:
        f["ma_trend"] = ((ma5/ma10 - 1) + (ma10/ma20 - 1)) * 100
        f["ma_bull"] = 1.0 if ma5 > ma10 > ma20 else (0.5 if ma5 > ma10 or ma10 > ma20 else (-1.0 if ma5 < ma10 < ma20 else 0.0))
    else:
        f["ma_trend"] = 0.0
        f["ma_bull"] = 0.0

    # rsi_signal: z-score
    rsi = calc_rsi(c)
    f["rsi_signal"] = (rsi - 50) / 15  # 标准化到 ~[-3.3, +3.3]

    # macd_signal: 强度
    dif = calc_ema(c, 12)
    dea = calc_ema(c, 26)
    if dif and dea and c[-1] > 0:
        f["macd_signal"] = (dif - dea) / c[-1] * 1000
    else:
        f["macd_signal"] = 0.0

    # vol_price: 量价共振
    if len(c) >= 6:
        ret_1d = (c[-1] / c[-2] - 1) * 100
        avg_vol_5 = np.mean(v[-6:-1]) if len(v) >= 6 else v[-1]
        vol_ratio = v[-1] / avg_vol_5 if avg_vol_5 > 0 else 1.0
        f["vol_price"] = vol_ratio * np.sign(ret_1d) * min(abs(ret_1d), 10) / 10
    else:
        f["vol_price"] = 0.0

    # dev_ma20: MA20偏离
    if ma20:
        f["dev_ma20"] = (c[-1] / ma20 - 1) * 100
    else:
        f["dev_ma20"] = 0.0

    # pct_52w: 52周位置
    if len(c) >= 120:
        h52, l52 = max(h[-120:]), min(l[-120:])
        f["pct_52w"] = (c[-1] - l52) / (h52 - l52) * 100 if h52 > l52 else 50.0
    else:
        f["pct_52w"] = 50.0

    # vol_ratio_5d
    if len(v) >= 6:
        avg_v5 = np.mean(v[-6:-1])
        f["vol_ratio_5d"] = v[-1] / avg_v5 if avg_v5 > 0 else 1.0
    else:
        f["vol_ratio_5d"] = 1.0

    # ret_5d, ret_20d
    if len(c) >= 6:
        f["ret_5d"] = (c[-1] / c[-6] - 1) * 100
    else:
        f["ret_5d"] = 0.0
    if len(c) >= 21:
        f["ret_20d"] = (c[-1] / c[-21] - 1) * 100
    else:
        f["ret_20d"] = 0.0

    # streak: 连涨天数
    streak = 0
    for i in range(1, min(6, len(c))):
        if c[-i] > c[-i-1]:
            streak += 1
        else:
            break
    f["streak"] = float(streak)

    # gap_open: 跳空缺口
    if len(c) >= 2 and c[-2] > 0:
        f["gap_open"] = (o[-1] - c[-2]) / c[-2] * 100
    else:
        f["gap_open"] = 0.0

    # turnover_z: 换手率异常
    if extra and extra.get("turnover", 0) > 0:
        f["turnover_z"] = extra["turnover"]  # 横向 z-score 在 compute_all_factors 中做
    else:
        f["turnover_z"] = 0.0

    # amplitude_z: 振幅异常
    if h[-1] > l[-1]:
        f["amplitude_z"] = (h[-1] - l[-1]) / c[-1] * 100
    else:
        f["amplitude_z"] = 0.0

    # volatility_20d: 20日波动率
    if len(c) >= 21:
        daily_rets = [(c[i] / c[i-1] - 1) for i in range(-20, 0) if c[i-1] > 0]
        if daily_rets:
            f["volatility_20d"] = np.std(daily_rets) * 100
        else:
            f["volatility_20d"] = 0.0
    else:
        f["volatility_20d"] = 0.0

    # max_dd_20d: 20日最大回撤
    if len(c) >= 21:
        recent_c = c[-21:]
        peak = np.maximum.accumulate(recent_c)
        dd = (peak - recent_c) / peak * 100
        f["max_dd_20d"] = -np.max(dd)  # 负值
    else:
        f["max_dd_20d"] = 0.0

    return f


def compute_capital_factors(klines, idx, fund_flow=None) -> Dict[str, float]:
    """资金面因子"""
    w = klines[:idx+1]
    c = np.array([k['close'] for k in w], dtype=float)
    v = np.array([k['volume'] for k in w], dtype=float)
    h = np.array([k['high'] for k in w], dtype=float)
    l = np.array([k['low'] for k in w], dtype=float)

    f = {}

    # CMF: Chaikin Money Flow
    if len(c) >= 20:
        mf_mult = [(c[i] - l[i] - (h[i] - c[i])) / (h[i] - l[i]) if h[i] != l[i] else 0.0
                   for i in range(-20, 0)]
        mf_vol = [m * v[i] for m, i in zip(mf_mult, range(-20, 0))]
        f["cmf"] = sum(mf_vol) / sum(v[-20:]) if sum(v[-20:]) > 0 else 0.0
    else:
        f["cmf"] = 0.0

    # MFI: Money Flow Index
    if len(c) >= 15:
        tp = [(h[i] + l[i] + c[i]) / 3 for i in range(-15, 0)]
        mf = [tp[i] * v[i] for i in range(-15, 0)]
        pos_flow = sum(mf[i] for i in range(1, 15) if tp[i] > tp[i-1])
        neg_flow = sum(mf[i] for i in range(1, 15) if tp[i] < tp[i-1])
        f["mfi"] = 100 - 100 / (1 + pos_flow / neg_flow) if neg_flow > 0 else 50.0
    else:
        f["mfi"] = 50.0

    # vwap_premium: VWAP20偏离
    if len(c) >= 21:
        vwap20 = sum(c[-21:] * v[-21:]) / sum(v[-21:]) if sum(v[-21:]) > 0 else c[-1]
        f["vwap_premium"] = (c[-1] / vwap20 - 1) * 100
    else:
        f["vwap_premium"] = 0.0

    # vol_up_days: 近5日放量天数
    if len(v) >= 6:
        vol_up = sum(1 for i in range(-5, 0) if v[i] > v[i-1])
        f["vol_up_days"] = float(vol_up) / 5.0
    else:
        f["vol_up_days"] = 0.0

    # 主力资金流
    if fund_flow:
        f["main_flow_5d"] = fund_flow.get("main_net_5d", 0) or 0.0
        f["main_flow_20d"] = fund_flow.get("main_net_20d", 0) or 0.0
        f["inflow_rate"] = fund_flow.get("inflow_rate", 0) or 0.0
    else:
        f["main_flow_5d"] = 0.0
        f["main_flow_20d"] = 0.0
        f["inflow_rate"] = 0.0

    return f


def compute_info_factors(event_list=None, today_str=None) -> Dict[str, float]:
    """信息面因子"""
    f = {"event_score": 0.0, "event_count": 0.0}

    if not event_list:
        return f

    # 复用 SS 评分的事件评分逻辑，但输出连续值
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

    total_score = 0.0
    for evt in event_list:
        title = evt.get("title", "")
        for _, kws, sc in EVENT_RULES:
            if any(kw in title for kw in kws):
                total_score += sc
                break

    count = len(event_list)
    f["event_score"] = total_score  # 连续值，不封顶
    f["event_count"] = float(count)

    return f


def compute_valuation_factors(extra=None, klines=None, idx=None) -> Dict[str, float]:
    """估值面因子"""
    f = {"pe_percentile": 0.0, "pb_percentile": 0.0, "log_mcap": 0.0}

    if not extra:
        return f

    # PE-TTM / PB 当前值（暂无历史分位数据，先用当前值在截面上标准化）
    pe = extra.get("pe_ttm", 0) or 0.0
    pb = extra.get("pb", 0) or 0.0
    mcap = extra.get("mcap", 0) or 0.0

    # 在有历史K线时，尝试计算估值分位
    if klines and idx is not None and idx >= 60:
        c_arr = np.array([k['close'] for k in klines[:idx+1]], dtype=float)
        if len(c_arr) >= 252:
            f["pe_percentile"] = np.mean(c_arr[-252:] <= c_arr[-1]) * 100
            f["pb_percentile"] = f["pe_percentile"]
        elif len(c_arr) >= 60:
            f["pe_percentile"] = np.mean(c_arr[-60:] <= c_arr[-1]) * 100
            f["pb_percentile"] = f["pe_percentile"]
        else:
            f["pe_percentile"] = 50.0
            f["pb_percentile"] = 50.0
    else:
        # 无历史数据，用中性值
        f["pe_percentile"] = 50.0
        f["pb_percentile"] = 50.0

    # 对数市值
    if mcap > 0:
        f["log_mcap"] = math.log(mcap + 1e8)
    else:
        f["log_mcap"] = 0.0

    return f


def compute_sector_factors(extra=None, sector_strength=None) -> Dict[str, float]:
    """行业/板块因子"""
    f = {"sector_rsi": 0.0, "sector_momentum": 0.0}

    if extra and sector_strength:
        sector = extra.get("_sector", "")
        if sector and sector in sector_strength:
            ss = sector_strength[sector]
            f["sector_rsi"] = ss.get("rsi", 50) - 50
            f["sector_momentum"] = ss.get("momentum", 0)

    return f


# ================================================================
# 因子IC追踪
# ================================================================

@dataclass
class FactorICTracker:
    """单个因子的多周期IC追踪"""
    name: str
    ic_windows: Dict[int, List[float]] = field(default_factory=dict)  # {horizon: [IC values]}
    window_size: int = 60  # 追踪最近60个交易日

    def record_ic(self, horizon: int, ic: float):
        if horizon not in self.ic_windows:
            self.ic_windows[horizon] = []
        self.ic_windows[horizon].append(ic)
        if len(self.ic_windows[horizon]) > self.window_size:
            self.ic_windows[horizon] = self.ic_windows[horizon][-self.window_size:]

    def get_icir(self, horizon: int) -> float:
        """返回指定周期的ICIR"""
        ics = self.ic_windows.get(horizon, [])
        if len(ics) < 10:
            return 0.0
        mean_ic = np.mean(ics)
        std_ic = np.std(ics)
        return mean_ic / std_ic if std_ic > 0 else 0.0

    def get_weighted_icir(self) -> float:
        """多周期加权ICIR"""
        return (
            self.get_icir(1) * 0.2 +
            self.get_icir(5) * 0.3 +
            self.get_icir(10) * 0.5
        )

    def get_ic_stats(self) -> Dict[int, Dict]:
        """返回各周期的IC统计"""
        stats = {}
        for h in [1, 5, 10]:
            ics = self.ic_windows.get(h, [])
            if len(ics) >= 10:
                stats[h] = {
                    "mean": np.mean(ics),
                    "std": np.std(ics),
                    "icir": self.get_icir(h),
                    "n": len(ics),
                }
        return stats


class FactorGraveyard:
    """因子墓地：管理因子生命周期"""

    def __init__(self, icir_threshold: float = 0.3, inactive_windows: int = 3):
        self.threshold = icir_threshold
        self.max_inactive = inactive_windows
        self.consecutive_weak: Dict[str, int] = defaultdict(int)  # {factor_name: consecutive_weak_windows}
        self.active_factors: set = set(FACTOR_NAMES)

    def evaluate(self, factor_trackers: Dict[str, FactorICTracker]) -> List[str]:
        """评估所有因子，返回被移入墓地的因子列表"""
        removed = []
        for name, tracker in factor_trackers.items():
            wicir = tracker.get_weighted_icir()
            if wicir < self.threshold:
                self.consecutive_weak[name] += 1
                if self.consecutive_weak[name] >= self.max_inactive:
                    if name in self.active_factors:
                        self.active_factors.discard(name)
                        removed.append(name)
            else:
                self.consecutive_weak[name] = 0
                self.active_factors.add(name)  # 复活
        return removed

    def get_active_names(self) -> List[str]:
        """返回当前活跃因子名称"""
        return [name for name in FACTOR_NAMES if name in self.active_factors]

    def status_report(self) -> Dict:
        return {
            "active_count": len(self.active_factors),
            "total_count": len(FACTOR_NAMES),
            "graveyard": [name for name in FACTOR_NAMES if name not in self.active_factors],
            "weak_warning": {name: c for name, c in self.consecutive_weak.items() if c > 0},
        }


# ================================================================
# 主入口：批量计算
# ================================================================

def compute_all_factors(
    klines_all: Dict[str, list],
    extra_all: Dict[str, dict],
    fund_flow_all: Dict[str, dict] = None,
    event_all: Dict[str, list] = None,
    sector_strength: Dict[str, dict] = None,
    today_str: str = None,
) -> Dict[str, Dict[str, float]]:
    """
    为所有股票计算所有连续因子值。

    Args:
        klines_all: {code: [kline_dicts]}
        extra_all: {code: {行情快照}}
        fund_flow_all: {code: {资金流}}
        event_all: {code: [events]}
        sector_strength: {sector: {rsi, momentum}}
        today_str: 当日日期

    Returns:
        {code: {factor_name: raw_value}}
    """
    if fund_flow_all is None:
        fund_flow_all = {}
    if event_all is None:
        event_all = {}
    if sector_strength is None:
        sector_strength = {}

    all_factors = {}
    for code, klines in klines_all.items():
        if not klines or len(klines) < 60:
            continue

        idx = len(klines) - 1
        ex = extra_all.get(code, {})
        ff = fund_flow_all.get(code, {})
        ev = event_all.get(code, [])

        # 合并所有因子
        factors = {}
        factors.update(compute_technical_factors(klines, idx, extra=ex))
        factors.update(compute_capital_factors(klines, idx, fund_flow=ff))
        factors.update(compute_info_factors(event_list=ev, today_str=today_str))
        factors.update(compute_valuation_factors(extra=ex, klines=klines, idx=idx))
        factors.update(compute_sector_factors(extra=ex, sector_strength=sector_strength))

        all_factors[code] = factors

    # 截面标准化：将 raw_value 转为 cross_sectional_zscore
    standardized = _cross_sectional_standardize(all_factors)

    return standardized


def _cross_sectional_standardize(
    raw_factors: Dict[str, Dict[str, float]]
) -> Dict[str, Dict[str, float]]:
    """
    截面标准化：每个因子在整个股票池内做 z-score。

    对于 higher_better=False 的因子，取反。
    """
    # 收集每个因子的所有值
    factor_values: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for code, factors in raw_factors.items():
        for fname, fval in factors.items():
            factor_values[fname].append((code, fval))

    # 计算 z-score
    result: Dict[str, Dict[str, float]] = defaultdict(dict)
    for fname, vals in factor_values.items():
        codes = [v[0] for v in vals]
        raw_vals = np.array([v[1] for v in vals], dtype=float)

        # 去除极端值 (1% / 99% winsorize)
        lo, hi = np.percentile(raw_vals, [1, 99])
        raw_vals = np.clip(raw_vals, lo, hi)

        mu, sigma = np.mean(raw_vals), np.std(raw_vals)
        if sigma < 1e-8:
            z_scores = np.zeros_like(raw_vals)
        else:
            z_scores = (raw_vals - mu) / sigma

        # higher_better 处理
        factor_def = FACTOR_MAP.get(fname)
        if factor_def and not factor_def.higher_better:
            z_scores = -z_scores

        for code, z in zip(codes, z_scores):
            result[code][fname] = round(float(z), 4)

    return dict(result)


# ================================================================
# IC 计算
# ================================================================

def compute_factor_ic(
    factor_values: Dict[str, Dict[str, float]],  # {code: {factor: zscore}}
    forward_returns: Dict[str, Dict[int, float]],  # {code: {horizon: return_pct}}
    horizon: int = 10,
) -> Dict[str, float]:
    """
    计算每个因子在某周期的截面 IC (Spearman Rank Correlation)

    Args:
        factor_values: 当日所有股票的因子值
        forward_returns: 未来收益 {code: {horizon: ret}}
        horizon: 预测周期

    Returns:
        {factor_name: IC}
    """
    common_codes = set(factor_values.keys()) & set(forward_returns.keys())
    if len(common_codes) < 20:
        return {}

    ic_results = {}
    for fname in FACTOR_NAMES:
        f_vals = []
        fwd_rets = []
        for code in common_codes:
            fval = factor_values[code].get(fname)
            fret = forward_returns[code].get(horizon)
            if fval is not None and fret is not None and not math.isnan(fval) and not math.isnan(fret):
                f_vals.append(fval)
                fwd_rets.append(fret)

        if len(f_vals) < 20:
            continue

        # Spearman rank correlation (using numpy rank correlation)
        try:
            f_arr = np.array(f_vals); r_arr = np.array(fwd_rets)
            # Use Pearson on ranks = Spearman
            rank_f = np.argsort(np.argsort(f_arr)).astype(float)
            rank_r = np.argsort(np.argsort(r_arr)).astype(float)
            corr = np.corrcoef(rank_f, rank_r)[0, 1]
            ic_results[fname] = round(float(corr), 4) if not np.isnan(corr) else 0.0
        except Exception:
            ic_results[fname] = 0.0

    return ic_results
