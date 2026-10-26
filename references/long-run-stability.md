# 长跑稳定性约束

## 已验证的代码边界

- 串口原始行只追加到 `serial_logs/*.log`，分析由 tailing parser 完成。
- `FactStore` 每端口最多保留 20,000 条事实；更早事实只保留日志文件。
- parser 单行缓冲上限 4MiB；超限记录 `SERIAL_LINE_OVERSIZED` 并丢弃缓冲。
- `TimelineLedger` 每个 case 关闭后释放 facts/anomalies，并移除 case 对象。
- `ScenarioRuntime` 在 `record_case` 后释放 case window、case facts、attempts、online/player 临时状态。
- `TaskArtifacts` 写入 results.csv 后移除 case metadata/position；checks 随 case 释放。
- `results.csv` 最终对账按行流式读取，不保留全量 row list。
- `tool.log` 校验按行流式读取，不再 `read_text()`。
- `run_multi` 的设备统计使用增量 `ResultSummary`；每个设备只保留固定数量状态。
- 子进程使用 Windows Job Object / atexit 清理；启动前检测活动 run.py/run_multi.py。

## 回归命令

```powershell
python -m unittest tests.test_serial_fact_store tests.test_long_run_soak -v
python -m py_compile scripts\burn_adapter.py scripts\run.py lstest\scripts\core.py lstest\scripts\timeline.py lstest\scripts\runtime.py
```

## 合成 soak 覆盖

- 100,000 条注入事实：FactStore 有界，最后事实可查询。
- 10,000 个 case：TimelineLedger 不保留已关闭 case。
- 20,000 行 tool.log：流式校验。
- 4MiB+ 无换行输入：parser buffer 有界并上报。

## 外部限制（不计入代码保证）

- USB/串口驱动、声卡、电源、设备固件和声学环境。
- `test_start` 约 6 小时的启动工具上限。
- 磁盘容量：1000 小时、4 台按当前日志/结果速率估算约 80–100GB，需要预留空间或定期归档。
- 独立子 agent 评审当前受 token/endpoint 认证限制；需要恢复后再做独立 F1-F3 复核。
