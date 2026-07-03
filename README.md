# SS with GLM 双模型A股量化评分系统 V10

SS（固定规则因子模型）+ GLM（Walk-Forward 集成学习）双模型架构。每日自动评分生成报告，双模型共识/分歧建议驱动决策，邮件推送。

## 架构

```
                ┌───────────────────┐     ┌────────────────────┐
K线/行情/事件 ──▶│ SS 固定规则模型    │────▶│ 双模型共识/分歧建议  │
                │ 技术35% + 资金55% │     │ 共识强买/共识卖出    │
                │ + 信息5% + 事件5% │     │ SS建议 / GLM建议    │
                └───────────────────┘     └────────────────────┘
                         │                          │
                ┌───────────────────┐               │
K线/行情/事件 ──▶│ GLM 集成学习       │───────────────┘
                │ 线性+多项式+时间加权 │
                │ 15特征 · 60天训练   │
                └───────────────────┘
```

- **SS 模型**：固定规则评分的规则引擎，基分50，对各维度加减分后裁剪到 [5, 95]
- **GLM 模型**：Walk-Forward Ridge 回归，每天从近60天数据学习15个特征的权重，预测 T+10 收益
- **最终建议**：SS 分 + GLM 分 → 共识/分歧判断 → 买卖建议

## 回测性能 (Walk-Forward)

| 指标 | 固定权重(V8) | GLM 集成 | 提升 |
|------|:----------:|:------:|:---:|
| T+10 Sharpe | 2.14 | 3.66 | +71% |
| IC Rank | ~0.12 | ~0.18 | +50% |
| Top-20% 多头均值 | 基准 | >+2% | — |

## 评分阈值

| 评分区间 | SS 建议 | GLM 建议 |
|---------|--------|---------|
| >=75 | 强烈买入 | 强烈看多 |
| >=70 | 逢低买入 | 看多 |
| >=60 | 持有 | 中性偏多 |
| >=45 | 观望 | 中性 |
| <45 | 回避 | 偏空 |
| <40（连续3天） | 强制卖出 | 强烈看空 |

## 双模型共识/分歧建议

| SS | GLM | 建议 | 含义 |
|----|-----|------|------|
| >=70 | >=70 | 🔥共识强买 | 两模型同时强烈看多，置信度最高 |
| >=70 | <70 | ⚡SS建议买入 | SS 看多但 GLM 保留，存在分歧 |
| >=70 | <45 | ⚡SS建议买入 | SS 单独看多，需警惕 |
| <70 | >=70 | 🧠GLM建议买入 | GLM 学出SS未捕获的信号 |
| <45 | <45 | 🚨共识卖出 | 两模型同时看空，应考虑减仓 |
| <45 | >=60 | ⚠️GLM建议回避 | GLM 数据驱动看空，SS 规则未触发 |

## 技术面指标

均线多头/空头排列、MACD零上金叉（与均线多头重叠时降权到2分）、RSI四级细分（弱势40-55=−3, 强动量75+=+10, 极端80+=+12, 超卖30-=−8）、量价关系四级（放量上涨+8/缩量下跌−10/放量回调洗盘+5/巨量下跌出货−8）、MA20偏离度、52周高低位区间。

## 资金面指标

MFI资金流、CMF资金净流向、VWAP20突破（与均线多头重叠Jaccard 0.79时降权）、持续放量检测、巨量突破/出货（量比20日>3）、振幅异常、大/小市值分层、主力资金5/20日净流向。

## 信息面指标

近两周公告事件自动抓取和评分（预增+20/减持−15/回购+10等）、3日涨幅/跌幅动量、跳空缺口、5日连涨/连跌、换手率异常。

## 项目结构

```
.
├── scoring_engine.py         # SS+GLM 双模型引擎 + 盘中建议 + HTML报告 + 邮件
├── stock_db.py               # 本地 SQLite 缓存（K线/行情/资金流/事件）
├── uploaded-stock-codes.txt  # 股票代码池（~137只）
├── backtest_final.py         # 终极对比：6方案 × Walk-Forward
├── backtest_glm_comprehensive.py  # GLM 因子学习 vs 固定权重
├── backtest_realtime.py      # 回测 V3（实时信号回测）
├── backtest_t1.py            # T+1 回测
├── backtest_intraday.py      # 盘中回测
├── backtest_position_factor.py    # 仓位因子回测
├── generate_report.py        # 报告生成工具
├── q2_earnings_analysis.py   # Q2财报分析
├── regen_html.py             # HTML重新生成（无需重跑引擎）
├── .env.example              # 环境变量模板
└── output/                   # 报告 + JSON + SQLite缓存
```

## 快速开始

### 1. 环境要求

- Python 3.9+（纯标准库 + numpy）

```bash
pip install numpy
```

### 2. 配置邮箱

```bash
cp .env.example .env
```

编辑 `.env`，填入你的QQ邮箱和SMTP授权码

### 3. 准备股票代码

在项目根目录已包含 `uploaded-stock-codes.txt`，137只A股代码

### 4. 运行评分

```bash
# 完整运行（抓数据+评分+生成报告）
python scoring_engine.py

# 跳过数据抓取（仅重新评分+生成报告，适用改分类/改权重场景）
python scoring_engine.py --regen-only

# 重新评分并发送邮件
python scoring_engine.py --regen-only --mail

# 指定收件人
python scoring_engine.py uploaded-stock-codes.txt your_email@qq.com
```

### 5. 查看报告

运行后在 `output/` 生成：

| 文件 | 说明 |
|------|------|
| `SS增强版评分_YYYY-MM-DD.html` | 交互版（Tab切换板块、点击排序、展开因子明细、SS/GLM双栏） |
| `SS增强版评分_YYYY-MM-DD_email.html` | 邮件版（纯内联样式） |
| `SS增强版评分_YYYY-MM-DD.json` | 机器可读JSON（含全部字段） |
| `SS增强版评分_YYYY-MM-DD.md` | Markdown版本 |
| `ss_score_history.json` | 评分历史（用于趋势展示） |

## 数据源

- K线：腾讯财经（`web.ifzq.gtimg.cn`）复权数据
- 实时行情：腾讯财经（`qt.gtimg.cn`）
- 主力资金：自选股 MCP（`westock-mcp`）
- 公告事件：自选股 MCP

## 定时运行

```bash
# 每个交易日15:30运行
30 15 * * 1-5 cd /path/to/stock-scoring && python scoring_engine.py
```

## License

MIT
