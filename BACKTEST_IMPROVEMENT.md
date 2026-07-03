# 回测改进分析：如何做一个更靠谱的回测模型

> 基于现有 `stock-scoring` 代码库（V10）的深度分析，2026-07-03

---

## 一、现有回测的 7 个偏差来源

### 🔴 P0：GLM 训练边界的前瞻偏差（critical）

**位置**：`scoring_engine.py:2445`

```python
def score_ensemble_glm(codes, klines_all, extra_all, train_days=60):
    for i in range(max(30, len(kl)-train_days-10), len(kl)-10):
        fv = _glm_features(kl, i, extra=ex)
        y = (kl[i+10]['close']/kl[i]['close']-1)*100  # ← 目标用到最新10天
```

**问题**：GLM 的训练集自动滑到了最新一天。当回测调用这个函数时，如果传入的 `klines_all` 包含了测试日之后的数据，GLM 就看到了不该看到的未来。

**验证方法**：打印 `train_X` 中最后一个样本对应的日期 → 是否在回测日期之后。

**影响**：GLM 的 Sharpe 3.66 可能被高估 20-40%。

---

### 🔴 P0：SS 评分系统的手工过拟合（data snooping）

**代码证据**：`scoring_engine.py` 中的多次迭代调参注释

```
V6修正：回测显示RSI 40-55是负超额，不是买点 → 扣3分
V7修正：RSI 55-70回测超额-1.01%，不再加分
V7修正：MACD与均线多头重叠(Jaccard 0.64)，降权
V7修正：突破VWAP20与均线多头重叠(Jaccard 0.79)，降权
V6修正：小市值-4.91%超额，加大惩罚
V6修正：RSI>80封顶和MA5超涨封顶已移到上方动量加分
```

**问题**：每一次"修正"都是因为"回测显示XX"，但使用的是同一份回测数据。不停根据同一轮回测结果调整参数，本质上是在拟合噪声。这是量化领域最经典的陷阱。

**改进方向**：需要严格的训练/验证/测试三期切分，参数调优只能在验证集上做，最终评估只用测试集。

---

### 🟡 P1：幸存者偏差

**现象**：所有回测都基于当前 137 只股票池。如果某只股票在过去表现不好被移除，回测不会反映这种损失。

**影响量级**：A股小盘股票池的回测通常高估年化收益 2-5%（学术文献共识）。

**改进方向**：回测时应在每个时间点只使用该时间点"已知"的股票池，或使用全市场截面来避免选择偏差。

---

### 🟡 P1：SS 评分离散化带来的信息损失

SS 评分是离散加减分（+15/−10 等），各维度封顶到 [5, 95]。这种规则引擎有三个问题：

1. **边界效应**：60 分和 59 分可以差一个 RSI 判断，但本质上只是噪音
2. **权重不可学**：技术35%/资金55% 的权重是手工设定的，没有数据验证
3. **因子共线性未处理**：代码中通过 Jaccard 系数手动检测（"均线多头与 MACD 重叠"），但只处理了两对重叠，没有系统化的因子冗余消解

**改进方向**：将 SS 的因子输出为连续的 `z-score`，然后用 GLM 统一学习权重，而不是手工硬编码加权。

---

### 🟡 P1：回测中 SS 评分使用的额外信息

在 `backtest_glm_comprehensive.py:219`：

```python
s = score_ss_enhanced(kl, idx, extra=ex)
```

但 `extra` 包含 PE、PB、市值等"实时"快照。回测时如果拿到了当天的 extra 信息，这本身不算偏差。但问题是 `extra` 是通过 `fetch_extra_info` 获取的**今日**快照（PE/PB 是当前值），在回测中全部统一使用了当前值，这意味着回测评分中隐含了"现在知道的历史 PE"——这是轻微的前瞻偏差。

---

### 🟡 P1：报告中的阈值来自样本内

`get_combined_suggestion()`（第859行）中的阈值（SS≥75 强烈买入、GLM≥75 强烈看多）是从哪里来的？如果是根据回测结果"选出"的最佳阈值，那这个阈值本身就包含了样本内偏差。

---

### 🟢 P2：数据可用性偏差

`fetch_kline_batch` 中对于请求失败的股票静默跳过（`except: pass`），这意味着回测样本不包含数据缺失的交易日。如果某天某只股票的数据缺失是因为退市/停牌，那么回测就会漏掉潜在的负收益。

---

## 二、改进方案：零偏差回测体系

### 思路

与其在现有代码上打补丁，不如设计一套**新的回测框架**，核心原则：

> **每天只使用当天及之前已知的信息做决策，严格按时间切分训练/验证/测试。**

### 架构

```
┌──────────────────────────────────────────────────────────┐
│                    数据层 (Data Layer)                     │
│  K线DB (SQLite) → 按日期切片的快照构建器                    │
│  每天的快照 = {特征矩阵, 评分结果, 前向收益}                 │
└──────────────────────────────────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
   ┌──────────┐       ┌──────────────┐     ┌──────────────┐
   │ 信号生成  │       │  回测引擎      │     │  统计分析     │
   │ Signals  │       │  Backtest     │     │  Statistics   │
   └──────────┘       └──────────────┘     └──────────────┘
    SS连续因子         时间序列组合回测         Bootstrap置信区间
    GLM自适应权重       Walk-Forward验证       IC时序分析
    买卖信号阈值        风险归因              Sharpe稳定性检验
```

### 改进点 1：SS 评分因子化（离散 → 连续）

把 SS 的加减分逻辑改成输出**连续 z-score**：

```python
def score_ss_continuous(klines, idx, extra=None):
    """返回连续因子向量（不做离散加减分，不做手工加权）"""
    factors = {}
    
    # 技术面因子（z-score）
    factors["ma_trend"] = (ma5/ma10 - 1) * 100       # 均线趋势强度
    factors["ma_bull"] = 1 if ma5>ma10>ma20 else 0    # 多头排列
    factors["rsi_z"] = (rsi - 50) / 15                  # RSI偏离中性
    factors["macd_signal"] = (dif - dea) / close        # MACD信号强度
    factors["vol_price"] = vr5 * (1 if c[-1]>c[-2] else -1)  # 量价共振
    factors["dev_ma20"] = (close - ma20) / ma20 * 100
    factors["pct_52w"] = (close - l52) / (h52 - l52) * 100
    
    # 资金面因子
    factors["cmf"] = cmf
    factors["mfi"] = (mfi - 50) / 25
    factors["vwap_premium"] = (close/vwap20 - 1) * 100
    factors["vol_trend"] = vol_up_days / 5  # 放量比例
    factors["log_mcap"] = math.log(mcap + 1e8)
    
    # 信息面因子
    factors["gap"] = (open - prev_close) / prev_close * 100
    factors["streak"] = streak / 5.0  # 连涨比例
    # ... 更多连续因子
    
    return factors
```

这样做的好处：
- GLM 可以真正学到每个因子的最优权重
- 不再需要手工调试"加几分、减几分"
- 因子间的冗余通过 Ridge 正则化自动处理

### 改进点 2：严格的三期时间切分

```python
# 时间切分：不允许任何跨期泄露
total_days = len(all_dates)
train_end = int(total_days * 0.6)    # 前 60% 用于训练
val_end = int(total_days * 0.8)       # 中间 20% 用于验证
# 后 20% 用于最终测试（只能跑一次）

train_dates = all_dates[:train_end]
val_dates = all_dates[train_end:val_end]
test_dates = all_dates[val_end:]

# 规则：
# 1. SS 因子的参数调优 → 只能在 train  + val 上做
# 2. GLM 的 Ridge 超参数选择 → 在 train 上拟合，在 val 上选
# 3. 最终 Sharpe/IC 评估 → 只在 test 上算
# 4. test 只能跑一次，如果跑完又回去调参数，等于污染了 test
```

### 改进点 3：Walk-Forward Purged Cross-Validation

标准的 Walk-Forward 只切一次。改进为**滚动窗口**，多轮验证：

```
时间轴 ─────────────────────────────────────────────▶
[train₁: D1-D60]   [test₁: D61-D80]
      [train₂: D21-D80]   [test₂: D81-D100]
            [train₃: D41-D100]   [test₃: D101-D120]
                  ...
```

每轮：
- 训练窗口 = 前 N 天
- 测试窗口 = 后 M 天
- **Purge 间隔**：训练窗口和测试窗口之间留一个 gap（比如 5 天），防止 T+10 标签泄露

最终报告所有测试窗口的 Sharpe/IC 的**均值、标准差、最小值**。如果标准差很大，说明策略在不同市场环境下不稳定。

### 改进点 4：组合级回测（而非因子级）

当前回测是"因子 vs 前向收益"的 IC 分析。更靠谱的做法是模拟真实交易：

```python
def portfolio_backtest(signals, klines, capital=1000000, max_positions=20):
    """
    组合级回测，模拟真实交易约束
    
    signals: {date: {code: ss_score, glm_score, ...}}
    """
    positions = {}    # {code: {shares, cost, entry_date}}
    cash = capital
    daily_nav = []    # [{"date": ..., "nav": ...}]
    
    for date in sorted(signals.keys()):
        # 1. 先处理持仓：止盈/止损检查
        for code in list(positions.keys()):
            if should_exit(positions[code], date, signals):
                cash += positions[code]["shares"] * get_price(code, date)
                del positions[code]
        
        # 2. 再处理买入：从信号中选Top-N
        buy_candidates = select_top_signals(signals[date], max_positions - len(positions))
        for code, signal in buy_candidates:
            if signal passes filters:
                shares = calculate_position_size(cash, signal, max_positions)
                positions[code] = {"shares": shares, "cost": price, "entry_date": date}
                cash -= shares * price
        
        daily_nav.append({"date": date, "nav": cash + mark_to_market(positions, date)})
    
    return daily_nav
```

从组合级回测可以算出：
- 年化收益 / Sharpe / 最大回撤 / Calmar（真实的，不是因子 IC）
- 胜率 / 盈亏比（真实的交易次数和结果）
- 换手率 / 交易成本影响
- 在不同市场环境下（牛市/熊市/震荡）的表现分解

### 改进点 5：信号 → 买卖点联动

回测的目的不只是验证信号好，更是要找到**最优的买卖执行方案**。目前的 SS 评分直接映射到 5 档建议，缺少对买点和卖点的精细设计。

**买入信号设计**：

| 条件 | 含义 | 示例 |
|------|------|------|
| 共识强买 + SS 分创新高 | 两模型同时确认且趋势向上 | 买入信号 A（高置信度） |
| 共识强买但 SS 分非新高 | 确认看多但不在强势期 | 买入信号 B（等待回踩） |
| GLM 建议买入 + SS 观望 | GLM 比 SS 更前瞻 | 买入信号 C（逆势试探） |
| SS 建议买入 + GLM 观望 | SS 规则触发但 GLM 不认同 | 不买（分歧信号） |

**卖出信号设计**：

| 条件 | 含义 | 动作 |
|------|------|------|
| 共识卖出 | 两模型同时看空 | 全部清仓 |
| SS 评分连续3天下降 + 破60 | 趋势转变 | 减仓 50% |
| GLM 评分连续3天下降 | 数据驱动看空 | 减仓 30% |
| 单一模型卖出 + 另一模型中性 | 轻度分歧 | 设止损，不加仓 |
| 达到止盈位（如20日涨幅>15%） | 止盈 | 分步减仓 |

### 改进点 6：统计分析

回测不应只报告"Sharpe 3.66"，还要：

```python
# Bootstrap 置信区间
def bootstrap_sharpe(returns, n_bootstrap=1000):
    sharpes = []
    for _ in range(n_bootstrap):
        sample = np.random.choice(returns, size=len(returns), replace=True)
        sharpes.append(compute_sharpe(sample))
    return {
        "mean": np.mean(sharpes),
        "ci_95": (np.percentile(sharpes, 2.5), np.percentile(sharpes, 97.5)),
        "prob_positive": np.mean(np.array(sharpes) > 0),
    }

# 在不同市场环境下的分段表现
def regime_analysis(returns, market_regimes):
    """按大盘状态分段统计"""
    for regime in ["牛市", "熊市", "震荡"]:
        regime_rets = returns[market_regimes == regime]
        sharpe = compute_sharpe(regime_rets)
        ...

# IC 衰减曲线
def ic_decay(signals, forward_returns):
    """IC 在不同持有期的衰减"""
    for horizon in [1, 3, 5, 7, 10, 15, 20]:
        ic = spearmanr(signals, forward_returns[:, horizon])
        ...
```

---

## 三、实施优先级

| 优先级 | 改进项 | 工作量 | 提升置信度的幅度 |
|--------|--------|--------|:---:|
| P0 | SS 因子连续化 + 消除手工过拟合 | 2-3小时 | ***** |
| P0 | 严格三期时间切分 + 单次 test 评估 | 1小时 | **** |
| P1 | Walk-Forward Purged CV（多轮滚动） | 2小时 | **** |
| P1 | 组合级回测（替代因子 IC） | 3小时 | ***** |
| P1 | 信号→买卖点联动规则设计 | 2小时 | *** |
| P2 | Bootstrap 置信区间 + 环境分解 | 1.5小时 | *** |
| P2 | 数据完整性检查 + 缺失处理 | 1小时 | ** |
| P3 | 行业中性 / 市值中性归因 | 2小时 | ** |

建议顺序：**先做 P0（因子连续化 + 时间切分），跑一次新回测，对比新老 Sharpe，确认偏差量级。然后做组合级回测，把买卖点联动规则嵌进去。最后补统计分析。**

---

## 四、关于买卖点的核心思路

一个评分系统好不好，最终体现在**买在哪个位置能赚钱、卖在哪个位置不亏钱**。目前的双模型共识/分歧建议是一个好的起点，但缺少两点：

### 4.1 评分变化 > 评分数值

股票的评分从 45 → 65 比一直稳定在 65 更值得买入。因为：
- 评分趋势反映了"市场是否正在确认我们的逻辑"
- 稳定的高分可能只是"大家都看好的白马"，反而没有超额

**改进**：在信号中增加 `score_momentum = ss_today - ss_5d_ago`，将其作为买入的加分条件。

### 4.2 不同评分段的不同策略

| 评分区间 | 策略类型 | 买入逻辑 | 卖出逻辑 |
|---------|---------|---------|---------|
| SS>75 且 GLM>75 | 趋势跟踪 | 突破新高加仓 | 趋势走弱卖出 |
| SS 60-75 且 GLM>SS | 均值回归 | 股价回踩MA20买入 | SS破60或GLM走弱卖出 |
| SS 45-60 且 GLM>60 | 逆向布局 | GLM持续上升时分批建仓 | SS跌破45止损 |
| SS<45 且 GLM<45 | 不做多 | — | 持有→立即卖出 |

---

## 五、一句话总结

**当前回测最大的问题不是代码错误，而是"手工过拟合 + 单次回测 + 因子 IC 而非组合回测"三层偏差叠加。** 改进的核心是把 SS 规则引擎因子化、GLM 学习权重、组合级回测验证买卖信号——三者打通，形成闭环。
