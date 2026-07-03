# V11 回测框架
# =============
# 评分 → 截面排序 → 交易模拟 → 分数标定
#
# 模块:
#   factor_engine  — 连续化因子 + 多周期IC追踪 + 因子墓地
#   data_builder   — Point-in-Time 快照构建器
#   glm_model      — 多周期 Walk-Forward GLM
#   trade_sim      — 组合级交易模拟器
#   wfo_runner     — Purge Gap Walk-Forward 回测
#   analyzer       — 分数标定 + 多维度评估
