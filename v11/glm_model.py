#!/usr/bin/env python3
"""
V11 多周期 GLM 模型
====================
三个周期的 Walk-Forward Ridge 回归：
  GLM_short → 预测 T+5 (权重侧重动量/资金流)
  GLM_mid   → 预测 T+10 (权重侧重质量/估值)
  GLM_long  → 预测 T+20 (权重侧重基本面)

集成评分 = 0.3 * GLM_short + 0.5 * GLM_mid + 0.2 * GLM_long

训练使用严格的时间切分：只用 train_end 之前的数据。
"""

import numpy as np
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v11.factor_engine import FACTOR_NAMES, FACTOR_REGISTRY
from typing import Dict, List, Tuple, Optional


# ================================================================
# 多周期 GLM 配置
# ================================================================

GLM_CONFIG = {
    "short": {
        "horizon": 5,
        "weight": 0.3,
        "ridge": 1.0,
        "description": "T+5 短期预测，侧重动量/资金流",
    },
    "mid": {
        "horizon": 10,
        "weight": 0.5,
        "ridge": 1.0,
        "description": "T+10 中期预测，侧重质量/估值",
    },
    "long": {
        "horizon": 20,
        "weight": 0.2,
        "ridge": 2.0,  # 长期预测噪音大，加大正则化
        "description": "T+20 长期预测，侧重基本面",
    },
}


# ================================================================
# GLM 核心
# ================================================================

def _glm_ols(X: np.ndarray, y: np.ndarray, ridge: float = 1.0,
             weights: Optional[np.ndarray] = None) -> np.ndarray:
    """Ridge 回归求解"""
    n, p = X.shape
    W = np.diag(np.sqrt(weights)) if weights is not None else np.eye(n)
    Xw, yw = W @ X, W @ y
    XtX = Xw.T @ Xw + ridge * np.eye(p)
    try:
        return np.linalg.solve(XtX, Xw.T @ yw)
    except np.linalg.LinAlgError:
        return np.linalg.lstsq(Xw, yw, rcond=None)[0]


class MultiPeriodGLM:
    """
    多周期 GLM 集成模型。

    Usage:
        glm = MultiPeriodGLM()
        glm.train(factor_values_by_date, forward_returns, train_end_date)
        scores = glm.predict(factor_values_today)
    """

    def __init__(self):
        self.betas: Dict[str, Optional[np.ndarray]] = {
            "short": None,
            "mid": None,
            "long": None,
        }
        self.active_factors: Optional[List[str]] = None

    def train(
        self,
        factor_history: Dict[str, Dict[str, Dict[str, float]]],  # {date: {code: {factor: value}}}
        forward_returns: Dict[str, Dict[str, Dict[int, float]]],   # {date: {code: {horizon: ret}}}
        train_end_date: str,
        active_factors: List[str] = None,
        train_days: int = 60,
    ) -> Dict[str, Dict]:
        """
        训练三个周期的 GLM（只用 train_end_date 之前的数据）。

        Args:
            factor_history: 所有日期的因子值
            forward_returns: 所有日期的前向收益
            train_end_date: 训练截止日期（不含）
            active_factors: 活跃因子列表（排除墓地中的）
            train_days: 训练窗口天数

        Returns:
            {period: {beta: array, train_n: int, train_ic: float}}
        """
        if active_factors is None:
            active_factors = FACTOR_NAMES
        self.active_factors = active_factors

        # 收集训练数据
        all_dates = sorted(factor_history.keys())
        train_dates = [d for d in all_dates if d < train_end_date][-train_days:]

        if len(train_dates) < 30:
            return {p: {"beta": None, "train_n": 0, "train_ic": 0} for p in GLM_CONFIG}

        results = {}
        for period, config in GLM_CONFIG.items():
            horizon = config["horizon"]
            X_list, y_list = [], []

            for date in train_dates:
                factors = factor_history.get(date, {})
                fwd = forward_returns.get(date, {})

                for code, fvals in factors.items():
                    fret = fwd.get(code, {}).get(horizon)
                    if fret is None:
                        continue
                    # 收集该因子的值
                    x_row = [fvals.get(fname, 0.0) for fname in active_factors]
                    if any(np.isnan(v) for v in x_row):
                        continue
                    X_list.append(x_row)
                    y_list.append(fret)

            if len(X_list) < 50:
                results[period] = {"beta": None, "train_n": len(X_list), "train_ic": 0}
                continue

            X = np.array(X_list, dtype=float)
            y = np.array(y_list, dtype=float)

            # 时间加权（近期样本权重更高）
            time_weights = np.exp(-0.02 * np.arange(len(y) - 1, -1, -1))

            beta = _glm_ols(X, y, ridge=config["ridge"], weights=time_weights)

            # 计算训练 IC
            preds = X @ beta
            train_ic = float(np.corrcoef(preds, y)[0, 1]) if len(y) > 1 else 0

            results[period] = {
                "beta": beta,
                "train_n": len(X_list),
                "train_ic": train_ic,
            }

            self.betas[period] = beta

        return results

    def predict(self, factor_values: Dict[str, Dict[str, float]]) -> Dict[str, Dict]:
        """
        预测当日评分。

        Args:
            factor_values: {code: {factor_name: zscore}}

        Returns:
            {code: {short_score, mid_score, long_score, ensemble_score, rank}}
        """
        if self.active_factors is None:
            self.active_factors = FACTOR_NAMES

        scores = {}
        raw_ensemble = []

        for code, fvals in factor_values.items():
            x = np.array([fvals.get(fname, 0.0) for fname in self.active_factors], dtype=float)

            period_scores = {}
            for period in ["short", "mid", "long"]:
                beta = self.betas.get(period)
                if beta is not None and len(beta) == len(x):
                    period_scores[f"{period}_score"] = float(x @ beta)
                else:
                    period_scores[f"{period}_score"] = 0.0

            # 集成
            ensemble = (
                period_scores["short_score"] * 0.3 +
                period_scores["mid_score"] * 0.5 +
                period_scores["long_score"] * 0.2
            )

            scores[code] = {**period_scores, "ensemble_score": round(ensemble, 4)}
            raw_ensemble.append((code, ensemble))

        # 截面标准化 + 排名
        if raw_ensemble:
            vals = np.array([v[1] for v in raw_ensemble])
            mu, sigma = np.mean(vals), max(0.01, np.std(vals))
            z_scores = (vals - mu) / sigma

            sorted_idx = np.argsort(vals)[::-1]
            n = len(sorted_idx)

            for rank, si in enumerate(sorted_idx):
                code = raw_ensemble[si][0]
                scores[code]["ensemble_z"] = round(float(z_scores[si]), 4)
                scores[code]["percentile"] = round(rank / n, 4)

        return scores

    def get_factor_weights(self, period: str = "mid") -> Dict[str, float]:
        """返回各因子的学习权重"""
        beta = self.betas.get(period)
        if beta is None or self.active_factors is None:
            return {}
        return {name: float(beta[i]) for i, name in enumerate(self.active_factors)}
