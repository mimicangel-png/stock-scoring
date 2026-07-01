# SS-Enhanced A股量化评分系统

基于技术面、资金面、信息面三维度的A股量化评分引擎，每日自动生成评分报告并通过邮件推送。

## 评分模型

SS-Enhanced 模型采用 **技术面40% + 资金面40% + 信息面20%** 的加权结构，基分50分。

### 评分阈值

| 评分区间 | 建议 |
|---------|------|
| >=75 | 强烈买入 |
| >=70 | 逢低买入 |
| >=60 | 持有 |
| >=45 | 观望 |
| <45 | 回避 |
| <40（连续3天） | 强制卖出 |

### 技术面指标

均线多头/空头排列、MACD金叉/死叉、RSI强弱区间、量价关系（放量上涨/缩量下跌/放量回调/巨量下跌）、MA20偏离度、52周高低位区间、超买超涨封顶机制。

### 资金面指标

MFI资金流入流出、CMF资金净流向、VWAP20突破、持续放量检测、收盘强势度+放量共振。

### 信息面指标

3日强势上涨/大幅下跌、跳空缺口信号、5日动量趋势、20日突破信号、换手率异常。

## 项目结构

```
.
├── scoring_engine.py        # 核心评分引擎 + 邮件发送
├── uploaded-stock-codes.txt # 股票代码池（每行一个6位代码）
├── backtest_realtime.py     # 回测脚本
├── backtest_realtime_v3.py  # 回测脚本 V3
├── generate_report.py       # 报告生成工具
├── q2_earnings_analysis.py  # Q2财报分析
├── regen_html.py            # HTML重新生成工具
├── .env.example             # 环境变量模板
└── output/                  # 评分报告输出目录（运行后自动生成）
```

## 快速开始

### 1. 环境要求

- Python 3.9+（纯标准库实现，无需安装第三方依赖）

### 2. 配置邮箱

```bash
cp .env.example .env
```

编辑 `.env`，填入你的QQ邮箱和SMTP授权码：

```
SMTP_HOST=smtp.qq.com
SMTP_PORT=587
SMTP_USER=your_email@qq.com
SMTP_PASSWORD=your_smtp_auth_code
```

> 获取授权码：QQ邮箱 → 设置 → 账户 → 开启SMTP服务 → 生成授权码

### 3. 准备股票代码

在项目根目录创建 `uploaded-stock-codes.txt`，每行一个6位A股股票代码：

```
000001
600519
300750
...
```

### 4. 运行评分

```bash
python scoring_engine.py uploaded-stock-codes.txt your_email@qq.com
```

参数说明：
- 第1个参数：股票代码文件路径（可选，默认读取项目目录下的 `uploaded-stock-codes.txt`）
- 第2个参数：收件人邮箱（可选，不传则不发送邮件）

### 5. 查看报告

运行后将在 `output/` 目录生成以下文件：

| 文件 | 说明 |
|------|------|
| `SS增强版评分_YYYY-MM-DD.html` | 交互版HTML报告（含JS排序/筛选） |
| `SS增强版评分_YYYY-MM-DD_email.html` | 邮件版HTML（纯内联样式） |
| `SS增强版评分_YYYY-MM-DD.json` | 机器可读JSON数据 |
| `SS增强版评分_YYYY-MM-DD.md` | Markdown版本 |

## 数据源

- K线数据：腾讯财经接口（`web.ifzq.gtimg.cn`）
- 实时行情：腾讯财经接口（`qt.gtimg.cn`）

无需API Key，直接通过HTTP请求获取。

## 定时运行

可配合 crontab 或自动化工具实现每日定时运行：

```bash
# 每个交易日15:30运行
30 15 * * 1-5 cd /path/to/project && python scoring_engine.py uploaded-stock-codes.txt your_email@qq.com
```

## License

MIT
