#!/usr/bin/env python3
"""
V11 回测系统入口
=================
完整流程：
  1. 加载数据 (stock_db + PoT 构建)
  2. 预计算前向收益
  3. 运行 Purge Gap WFO
  4. 生成分析报告（分数标定 + 因子状态 + 参数稳定性）

Usage:
    python -m v11.run --codes uploaded-stock-codes.txt --output output/v11/
"""

import sys, os, json, argparse

# 确保项目根目录在 path 中
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from stock_db import StockDB
from v11.data_builder import PoTDataBuilder, compute_forward_returns
from v11.wfo_runner import WFORunner
from v11.analyzer import (
    calibrate_scores, calibration_to_markdown,
    check_param_stability, generate_full_report,
)


def main():
    parser = argparse.ArgumentParser(description="V11 回测系统")
    parser.add_argument("--codes", default="uploaded-stock-codes.txt", help="股票代码文件")
    parser.add_argument("--output", default="output/v11", help="输出目录")
    parser.add_argument("--train-size", type=int, default=252, help="训练窗口天数")
    parser.add_argument("--test-size", type=int, default=126, help="测试窗口天数")
    parser.add_argument("--purge-gap", type=int, default=5, help="Purge间隔天数")
    parser.add_argument("--top-pct", type=float, default=0.15, help="买入排名阈值 (0.15=top15%)")
    parser.add_argument("--max-positions", type=int, default=20, help="最大持仓数")
    parser.add_argument("--lookback", type=int, default=300, help="回溯天数")
    args = parser.parse_args()

    # ====== 准备 ======
    os.makedirs(args.output, exist_ok=True)

    # 加载股票代码
    codes_file = args.codes
    if not os.path.isabs(codes_file):
        codes_file = os.path.join(PROJECT_DIR, codes_file)

    with open(codes_file) as f:
        codes = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    print(f"[V11] 股票池: {len(codes)} 只")
    print(f"[V11] 输出目录: {args.output}")

    # ====== 1. 加载数据 ======
    print("\n[1/4] 加载数据...")
    db = StockDB()
    klines_all = db.get_klines(codes, days=args.lookback)
    extra_all = db.get_extra_info(codes)
    fund_flow_all = db.get_fund_flows(codes)
    events_all = db.get_events(codes, days=args.lookback)

    print(f"  K线: {len(klines_all)}只")
    print(f"  行情: {len(extra_all)}只")
    print(f"  资金流: {len(fund_flow_all)}只")
    print(f"  事件: {len(events_all)}只有事件数据")

    # ====== 2. 预计算前向收益 ======
    print("\n[2/4] 预计算前向收益...")
    forward_returns = compute_forward_returns(klines_all, codes)

    # ====== 3. 运行 WFO ======
    print("\n[3/4] 运行 Purge Gap WFO 回测...")
    runner = WFORunner(
        train_size=args.train_size,
        test_size=args.test_size,
        purge_gap=args.purge_gap,
        top_pct=args.top_pct,
        max_positions=args.max_positions,
    )

    report = runner.run(
        klines_all=klines_all,
        extra_all=extra_all,
        fund_flow_all=fund_flow_all,
        event_all=events_all,
        forward_returns=forward_returns,
    )

    # ====== 4. 生成报告 ======
    print("\n[4/4] 生成分析报告...")

    # 保存完整 JSON
    json_path = os.path.join(args.output, "v11_full_report.json")
    with open(json_path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 分数标定
    if report.get("trades"):
        from v11.trade_sim import Trade
        trades = [
            Trade(**t) if isinstance(t, dict) else t
            for t in report["trades"]
        ]
        calibration = calibrate_scores(trades)
        calibration_md = calibration_to_markdown(calibration)

        md_path = os.path.join(args.output, "v11_calibration.md")
        with open(md_path, "w") as f:
            f.write(calibration_md)

        # 交易明细 CSV
        csv_path = os.path.join(args.output, "v11_trades.csv")
        with open(csv_path, "w") as f:
            f.write("code,entry_date,exit_date,return_pct,holding_days,exit_reason,entry_percentile\n")
            for t_data in report["trades"]:
                f.write(f"{t_data['code']},{t_data['entry_date']},{t_data['exit_date']},"
                       f"{t_data['return_pct']},{t_data['holding_days']},"
                       f"{t_data['exit_reason']},{t_data['entry_percentile']}\n")

        # 参数稳定性
        stability = check_param_stability(report.get("window_results", []))
        stability_path = os.path.join(args.output, "v11_stability.json")
        with open(stability_path, "w") as f:
            json.dump(stability, f, ensure_ascii=False, indent=2)

    # ====== 打印摘要 ======
    print("\n" + "="*60)
    print("V11 回测完成")
    print("="*60)

    summary = report.get("summary", {})
    if summary:
        print(f"  交易笔数: {summary.get('total_trades', 0)}")
        print(f"  胜率:     {summary.get('win_rate', 0):.1%}")
        print(f"  平均收益: {summary.get('avg_return', 0):+.1f}%")
        print(f"  年化收益: {summary.get('annual_return', 0):.1%}")
        print(f"  Sharpe:   {summary.get('sharpe', 0):.2f}")
        print(f"  最大回撤: {summary.get('max_drawdown', 0):.1%}")
        print(f"  Calmar:   {summary.get('calmar', 0):.2f}")

    factor_status = report.get("factor_status", {})
    if factor_status:
        print(f"\n  活跃因子: {factor_status.get('active_count', 0)}/{factor_status.get('total_count', 0)}")
        if factor_status.get("graveyard"):
            print(f"  墓地中的因子: {factor_status['graveyard']}")

    print(f"\n  报告文件:")
    print(f"    JSON: {json_path}")
    print(f"    标定: {os.path.join(args.output, 'v11_calibration.md')}")
    print(f"    交易: {os.path.join(args.output, 'v11_trades.csv')}")


if __name__ == "__main__":
    main()
