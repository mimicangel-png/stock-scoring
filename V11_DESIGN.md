# V11 回测架构改造方案

> 综合 BACKTEST_IMPROVEMENT + SCORE_CALIBRATION + BENCHMARK_RESEARCH 三个分析得出的一体化设计

---

## 一、核心判断：回测不应该测评分，应该测交易

### 当前 V10 的逻辑

```
评分引擎 → 打出一个分数 → 算这个分数和 T+10 收益的 IC → IC 高 = 好
```

**问题**：IC 高不代表你按分数买卖能赚钱。因为你不知道什么时候买、什么时候卖、持有多久。

### V11 的逻辑

```
评分引擎 → 打出一个分数
    ↓
根据分数触发买卖规则 (买在回踩、卖在破位、持有 N 天等)
    ↓
模拟完整交易，记录每笔交易的买价/卖价/持有天数/盈亏
    ↓
按分数段统计：75 分的胜率是 X%，平均收益是 Y%
    ↓
这个统计结果就是标尺 — 分数终于有了实际意义
```

---

## 二、改造总览：五个模块替换

```
                     V10                             V11
              ┌──────────────┐              ┌──────────────────┐
数据层         │ API 实时抓取   │      →      │ Point-in-Time DB  │
              └──────────────┘              │ 每日期望快照      │
                                            └──────────────────┘
              ┌──────────────┐              ┌──────────────────┐
因子层         │ SS 离散加减分   │      →      │ 连续 z-score      │
              │ +3/-8/+5...   │              │ + IC 追踪列      │
              └──────────────┘              │ + 因子墓地        │
                                            └──────────────────┘
              ┌──────────────┐              ┌──────────────────┐
评分层         │ 固定加权求和    │      →      │ Walk-Forward GLM  │
              │ 手工阈值       │              │ 学习权重         │
              └──────────────┘              │ 截面排序         │
                                            └──────────────────┘
              ┌──────────────┐              ┌──────────────────┐
回测层         │ 因子 IC 分析    │      →      │ 组合级交易模拟    │
              │ 单次 WFO       │              │ Purge Gap WFO   │
              └──────────────┘              │ 多窗口滚动        │
                                            └──────────────────┘
              ┌──────────────┐              ┌──────────────────┐
分析层         │ IC/Sharpe      │      →      │ 分数→胜率标定表   │
              │               │              │ 环境分段评估      │
              └──────────────┘              │ 参数稳定性检验    │
                                            └──────────────────┘
```

---

## 三、模块设计

### 模块 1：PoT 数据层 — 杜绝前瞻偏差

**设计目标**：回测的每一天，只暴露该天及之前的数据。

```python
class PoTSnapshot:
    """某一日的全量数据快照"""
    date: str                            # "2025-06-15"
    klines: Dict[str, List[Kline]]       # 截至该日的历史K线（不含该日之后的）
    prices: Dict[str, float]             # 该日收盘价
    events: Dict[str, List[Event]]       # 该日之前公布的公告
    fund_flows: Dict[str, FlowData]      # 该日可获取的资金流
    # 关键：以上数据都不包含 date 之后的信息
```

**实现方式**：
- 复用现有的 `stock_db.py` SQLite，但做一次性的全量构建
- 把 K 线 DB 中所有日期的 K 线拉出来，按日期索引
- `PoTSnapshot(date)` = 查询所有 `klines.date <= date` 的记录
- 一次构建，多次复用（避免回测中逐日重拉 API）

**防前瞻偏差的关键检查**：
```python
def validate_snapshot(snapshot, test_date):
    """确保快照中没有测试日之后的数据"""
    for code, klines in snapshot.klines.items():
        max_kline_date = max(k['date'] for k in klines)
        assert max_kline_date <= test_date, f"前瞻偏差! {code}: {max_kline_date} > {test_date}"
```

### 模块 2：连续因子层 — 离散加减分 → z-score

**设计目标**：保留 SS 的因子计算逻辑，但输出连续值而非离散加减分。

SS 当前的每个加减分：
```python
# 当前：离散加减
if rsi > 75: tech += 10; factors.append({"name":"RSI强动量","delta":+10})

# 改造：连续 z-score
rsi_z = (rsi - 50) / 15  # RSI偏离中性的标准差
factors.append({"name":"rsi_signal", "raw": rsi, "zscore": rsi_z, "ic_history": [...]})
```

**所有因子统一为 3 元组**：`(name, raw_value, z_score)`

SS 因子的连续化映射：

| 当前因子 | 当前逻辑 | 连续化 |
|---------|---------|--------|
| 均线多头排列 | +15 if MA5>MA10>MA20 else -10 | `ma_trend = (MA5/MA10-1)*100 + (MA10/MA20-1)*100` |
| RSI | 多档加减 | `rsi_z = (RSI-50)/15` |
| MACD | 金叉=+5, 死叉=-10 | `macd_signal = (DIF-DEA)/close*100` |
| 量价关系 | 四档加减 | `vol_price = vol_ratio * sign(daily_return)` |
| MA20偏离 | 分档处理 | `dev_ma20 = (close/MA20-1)*100` |
| 52周位置 | 分档处理 | `pct_52w = (close-low52)/(high52-low52)*100` |
| CMF | — | `cmf` (已是连续值) |
| MFI | — | `mfi_z = (MFI-50)/25` |
| 事件评分 | 离散加分 | `event_z = rank(event_score)` |

**每个因子附带 IC 追踪**：
```python
@dataclass
class FactorState:
    name: str
    ic_window: List[float]        # 最近20天的日IC
    icir: float                    # IC / IC_std
    active: bool = True            # ICIR < 0.3 连续3周期 → False
    weight: float = 1.0            # 当前权重（由GLM学习或固定）
```

### 模块 3：评分层 — Walk-Forward GLM + 截面排序

**设计目标**：每天运行一次 GLM 训练（只用当天之前的数据），输出评分后做截面排序。

```python
def daily_scoring_pipeline(snapshot: PoTSnapshot) -> Dict[str, ScoreResult]:
    """
    单日评分流程（严格无前瞻偏差）
    """
    scores = {}
    
    # Step 1: 计算所有因子的连续值
    for code in snapshot.universe:
        factors = compute_continuous_factors(snapshot.klines[code], snapshot.extra[code])
        factor_vector[code] = factors
    
    # Step 2: 截面标准化（z-score）
    for factor_name in factor_names:
        cross_section = [factor_vector[c][factor_name].z_score for c in stocks]
        mu, sigma = np.mean(cross_section), np.std(cross_section)
        for c in stocks:
            factor_vector[c][factor_name].cross_z = (factor_vector[c][factor_name].z_score - mu) / sigma
    
    # Step 3: Walk-Forward GLM 训练
    # 只用 snapshot.date 之前的数据
    training_dates = [d for d in all_dates if d < snapshot.date]
    if len(training_dates) >= 60:
        glm_weights = train_glm(training_dates, factor_vector_history, returns_history)
    else:
        glm_weights = DEFAULT_WEIGHTS  # 冷启动用固定权重
    
    # Step 4: 评分 = Σ(weight_i * cross_z_i)
    for code in stocks:
        score = sum(glm_weights[i] * factor_vector[code][name].cross_z 
                    for i, name in enumerate(factor_names))
        scores[code] = score
    
    # Step 5: 截面排序 → 排名（而非绝对分数）
    sorted_codes = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    for rank, (code, score) in enumerate(sorted_codes):
        scores[code] = ScoreResult(
            code=code,
            raw_score=score,
            percentile=rank / len(sorted_codes),  # 0.0 = 最好, 1.0 = 最差
            rank=rank,
            glm_weights=glm_weights,
        )
    
    return scores
```

**关键变化**：
- 输出从"绝对分数 0-100"变为"截面排名百分位"
- 这样做避开了阈值调参问题——不再需要定"75 分是买点"
- 信号逻辑变为"排名前 20% 的股票"或"排名从一个 decile 跳到另一个"

### 模块 4：回测层 — 组合级交易模拟 + Purge Gap WFO

**设计目标**：模拟真实的买入→持有→卖出过程，统计交易级别的胜率/赔率。

```python
class Trade:
    """一笔完整的交易"""
    code: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str         # "signal_exit" | "stop_loss" | "take_profit" | "time_exit"
    return_pct: float
    holding_days: int
    entry_score: float       # 买入时的评分
    entry_percentile: float  # 买入时的截面排名

class TradeSimulator:
    """
    交易模拟器：
    每天根据当日评分生成信号 → 买入符合条件的股票 → 持仓管理 → 根据信号卖出
    """
    
    def __init__(self, exit_rules: List[ExitRule], max_positions=20):
        self.exit_rules = exit_rules
        self.max_positions = max_positions
    
    def run(self, daily_scores, daily_prices, start_date, end_date):
        """
        逐日模拟交易
        
        daily_scores: {date: {code: ScoreResult}}
        daily_prices: {date: {code: float}}
        """
        positions = {}      # {code: Position}
        trades = []         # 完成的交易
        daily_nav = []      # 每日净值
        
        for date in sorted(daily_scores.keys()):
            # 1. 检查持仓：是否有满足退出条件的
            for code in list(positions.keys()):
                pos = positions[code]
                exit_signal = self.check_exit(pos, date, daily_scores, daily_prices)
                if exit_signal:
                    trade = self.close_position(pos, exit_signal, date, daily_prices)
                    trades.append(trade)
                    del positions[code]
            
            # 2. 生成买入信号
            today_scores = daily_scores[date]
            buy_candidates = self.generate_buy_signals(today_scores, positions)
            
            # 3. 执行买入（受仓位限制）
            available_slots = self.max_positions - len(positions)
            for code in buy_candidates[:available_slots]:
                positions[code] = self.open_position(code, date, daily_prices)
            
            # 4. 计算当日净值
            daily_nav.append(self.calculate_nav(positions, date, daily_prices))
        
        return trades, daily_nav
    
    def generate_buy_signals(self, scores, current_positions):
        """
        买入信号规则（可配置）：
        - 截面排名前 20%（percentile < 0.2）
        - 且不在已有持仓中
        - 且 GLM 权重中的 momentum 因子为正（排除逆势）
        """
        candidates = []
        for code, sr in scores.items():
            if code in current_positions: continue
            if sr.percentile < 0.2:  # 排名前20%
                candidates.append((code, sr))
        # 按排名排序
        candidates.sort(key=lambda x: x[1].percentile)
        return [c[0] for c in candidates]
    
    def check_exit(self, position, date, scores, prices):
        """
        退出信号（优先级从高到低）：
        1. 止损：跌幅超过 stop_loss_pct
        2. 信号走弱：截面排名跌破阈值
        3. 时间退出：持有超过 max_hold 天
        """
        # 规则 1：止损
        current_price = prices[date][position.code]
        loss_pct = (current_price / position.entry_price - 1) * 100
        if loss_pct < -8:
            return ExitSignal("stop_loss", f"跌幅 {loss_pct:.1f}%")
        
        # 规则 2：信号走弱（排名跌出前50%）
        sr = scores[date].get(position.code)
        if sr and sr.percentile > 0.5:
            return ExitSignal("signal_decay", f"排名跌至 {sr.percentile:.0%}")
        
        # 规则 3：时间退出
        days_held = (parse_date(date) - parse_date(position.entry_date)).days
        if days_held > 20:
            return ExitSignal("time_exit", f"持有 {days_held} 天")
        
        return None
```

**Purge Gap WFO 窗口设计**：

```
时间轴 ────────────────────────────────────────────────────▶
[ 训练: D1-D252 ]  [purge:D253-D257]  [ 测试: D258-D383 ]
         [ 训练: D127-D378 ]  [purge:D379-D383]  [ 测试: D384-D509 ]
                [ 训练: D253-D504 ]  [purge:D505-D509]  [ 测试: D510-D635 ]
```

- **Train window**: 252 天（约1年）
- **Purge gap**: 5 天（防止 T+10 标签泄露）
- **Test window**: 126 天（约半年）
- **Step**: 126 天
- **总轮数**: 取决于数据长度，约 4-6 轮

```python
def walk_forward_windows(all_dates, train_size=252, test_size=126, purge_gap=5):
    windows = []
    start = 0
    while start + train_size + purge_gap + test_size <= len(all_dates):
        train_end = start + train_size
        test_start = train_end + purge_gap
        test_end = test_start + test_size
        windows.append({
            "train": all_dates[start:train_end],
            "test": all_dates[test_start:test_end],
            "purge": all_dates[train_end:test_start],
        })
        start += test_size  # 每次前进一个测试窗口
    return windows
```

### 模块 5：分析层 — 分数标定 + 多维度评估

**5.1 分数标定表**

回测完成后，把所有交易按买入时的评分分桶统计：

```python
def calibrate_scores(trades: List[Trade]) -> ScoreCalibration:
    """
    输入：所有模拟交易
    输出：每个评分段的统计特征
    """
    buckets = defaultdict(list)
    for t in trades:
        bucket = int(t.entry_percentile * 10)  # 0-9 桶
        buckets[bucket].append(t)
    
    calibration = {}
    for bucket, bucket_trades in sorted(buckets.items()):
        returns = [t.return_pct for t in bucket_trades]
        wins = [r for r in returns if r > 0]
        calibration[bucket] = {
            "percentile_range": f"{bucket*10}-{(bucket+1)*10}%",
            "n_trades": len(bucket_trades),
            "win_rate": len(wins) / len(bucket_trades) if bucket_trades else 0,
            "avg_return": np.mean(returns),
            "median_return": np.median(returns),
            "max_return": max(returns),
            "min_return": min(returns),
            "avg_hold_days": np.mean([t.holding_days for t in bucket_trades]),
            "best_exit_rule": find_best_exit_rule(bucket_trades),  # 该段最优退出规则
        }
    
    return calibration
```

**输出示例**：

| 排名段 | 交易数 | 胜率 | 平均收益 | 最大收益 | 最大亏损 | 平均持仓 | 最优退出 |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|---|
| top 10% | 234 | 68% | +3.2% | +22% | -8% | 8天 | 信号走弱 |
| 10-20% | 198 | 55% | +1.5% | +15% | -8% | 11天 | 止损-6% |
| 20-30% | 167 | 43% | -0.3% | +11% | -8% | 14天 | 不买 |
| ... | | | | | | | |
| bottom 30% | — | — | — | — | — | — | 不买 |

**这就是标尺**："top 10%" = 历史上买入有 68% 胜率、平均 +3.2%、持仓 8 天。

**5.2 多维度评估**

```python
class BacktestReport:
    # 交易层面
    total_trades: int
    win_rate: float
    avg_return: float
    profit_factor: float  # 总盈利/总亏损
    avg_hold_days: float
    
    # 组合层面
    annual_return: float
    sharpe: float
    calmar: float        # 年化收益/最大回撤
    sortino: float
    max_drawdown: float
    turnover_rate: float  # 换手率
    
    # 稳定性层面
    param_stability: Dict[str, float]  # 各参数在不同WFO窗口的标准差
    score_calibration: ScoreCalibration
    
    # 环境分解
    bull_market_sharpe: float   # 牛市区间的Sharpe
    bear_market_sharpe: float   # 熊市区间的Sharpe
    range_market_sharpe: float  # 震荡区间的Sharpe
    
    # 因子层面
    factor_icir: Dict[str, float]     # 每个因子的ICIR
    factor_retention: List[str]        # 存活的因子
    factor_removed: List[str]          # 被墓地移除的因子
```

---

## 四、完整数据流

```
┌─────────────────────────────────────────────────────────┐
│  步骤 1: 数据准备 (stock_db.py 已有，扩展 PoT 索引)        │
│                                                         │
│  K线 DB → PoTSnapshot(date) → {klines, prices, events}  │
│  一次构建，对所有 date 生成快照字典                        │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│  步骤 2: 因子计算 (每天、每只股票)                         │
│                                                         │
│  PoTSnapshot → compute_continuous_factors()             │
│  → {factor_name: (raw, zscore, cross_z)}               │
│                                                         │
│  同时记录每个因子的日 IC（与 T+10 收益的相关性）            │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│  步骤 3: WFO 循环                                        │
│                                                         │
│  for (train, purge, test) in walk_forward_windows():    │
│      ├─ 3a. 在 train 窗口训练 GLM                       │
│      ├─ 3b. 在 test 窗口逐日评分                        │
│      ├─ 3c. 模拟交易（TradeSimulator）                   │
│      └─ 3d. 记录 test 窗口的交易                         │
│                                                         │
│  因子墓地检查：在每个 train 窗口结束时                     │
│     for factor in factors:                              │
│         if factor.icir < 0.3 for 3 consecutive windows: │
│             factor.active = False                       │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│  步骤 4: 分析汇总                                        │
│                                                         │
│  所有 test 窗口的交易合并                                 │
│  → ScoreCalibration (按评分分段统计)                      │
│  → BacktestReport (多维度指标)                           │
│  → ParamStability (参数稳定性)                           │
│  → RegimeAnalysis (环境分解)                              │
└─────────────────────────────────────────────────────────┘
```

---

## 五、实施路径

### 第一阶段：因子连续化 + IC 追踪 (今天可做)

**目标**：不改动回测逻辑，只把 SS 的离散因子改成连续输出，加 IC 追踪。

**文件**：新建 `factor_engine_v11.py`

```python
# factor_engine_v11.py
def compute_continuous_factors(klines, idx, extra=None, fund_flow=None, events=None):
    """
    基于 score_ss_enhanced 的逻辑，但输出连续 z-score 而非离散加减分
    返回 {factor_name: {"raw": float, "zscore": float}}
    """
    pass  # 实现所有 SS 因子的连续化版本
```

**验证**：跑一次完整评分，对比新旧版本的前 20 只股票排名是否大致一致（如果完全不一样，说明离散化的信息损失很大）。

### 第二阶段：组合级交易模拟器 (1-2 天)

**文件**：新建 `trade_simulator.py`，实现 `TradeSimulator` 类

- 买入规则：截面排名 top 20%
- 退出规则：三档退出（止损、信号走弱、时间退出）
- 输出：完整的交易列表 + 每日净值

**验证**：用现有 V10 评分（不做连续化）跑一次交易模拟，看组合收益是否为正。

### 第三阶段：Purge Gap WFO (半天)

**文件**：新建 `wfo_runner.py`

- Walk-Forward 窗口切分
- 每轮 train → 训练 GLM → test → 模拟交易
- 所有 test 窗口的交易合并

### 第四阶段：分数标定 + 分析报告 (半天)

**文件**：扩展现有 `generate_report.py` 或新文件

- 按入场评分分桶统计
- 多维度绩效指标
- 参数稳定性检验
- 因子墓地状态

### 最终交付物

```
V11 回测跑完后的输出：
├── trades.csv              # 每笔交易详情（买价/卖价/日期/收益/持仓天/退出原因）
├── calibration.md          # 分数标定表（可读）
├── calibration.json        # 分数标定数据（可程序消费）
├── backtest_report.json    # 完整绩效指标
├── factor_status.json      # 因子存亡状态
└── daily_nav.csv           # 每日净值曲线
```

---

## 六、关键设计决策FAQ

**Q: 截面排序 vs 绝对分数，为什么选前者？**
A: 因为绝对分数需要在不同时间点可比。2024 年的 75 分和 2025 年的 75 分对应的"好"程度不同。截面排序自然解决了这个问题——排名前 20% 在任何时间点的含义一致。

**Q: 为什么不直接抛弃 SS 评分，全用 GLM？**
A: SS 的逻辑代表了你的市场理解（"放量上涨是好的""MACD金叉是信号"等）。GLM 可能选出一些你无法理解的因子组合。双模型各有价值：SS 提供可解释性，GLM 提供数据驱动的权重校准。

**Q: 因子墓地会不会过早移除有效因子？**
A: 设置较长的失效期（3 个 WFO 窗口 ≈ 1.5 年）和较宽松的 ICIR 阈值（0.3），确保只在因子确实长期无效时才移除。

**Q: 这个 V11 和 V10 报告怎么共存？**
A: V11 是新模块，不影响现有 `scoring_engine.py` 的每日评分。V11 是"验证工具"——跑一次看回测的真实质量。评分日报继续用 V10。
