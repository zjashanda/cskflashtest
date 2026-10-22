# 多设备并行烧录压测

## 结论

支持最多 10 台设备同时压测。每台设备必须有独立的：

- 控制串口 `control`（当前单台为 COM14）
- CSK 日志串口 `csk`（COM13）
- 烧录串口 `burn`（COM15）
- 可选 HISI 日志串口 `hisi`（COM18）

每台设备独立启动一个 `run.py` 子进程，互不共享串口、烧录工具进程和结果目录。单台失败不会中断其他设备。

## 端口数量

- 10 台设备最少需要 **30 个串口**（control/csk/burn 各 10 个）；
- 若同时采集 HISI，需要 **40 个串口**；
- 当前机器只有少量 CH344 端口，正式 10 台前需要增加 USB 转串口适配器；
- 建议使用带独立供电的 USB Hub，避免 10 台设备同时上电造成掉电/枚举失败；
- 默认每台设备启动间隔 2s（`--stagger`），降低同时上电和 USB 枚举冲击。

## 配置文件

复制并修改：

```text
cskflashtest/config/devices.json
cskflashtest/config/devices.example.json
```

每台设备一条：

```json
{
  "id": "dev01",
  "control": "COM14",
  "csk": "COM13",
  "burn": "COM15",
  "hisi": "COM18",
  "enabled": true
}
```

规则：

- `id` 只能包含字母、数字、下划线、点和短横线，且唯一；
- `control/csk/burn` 必填，且所有设备之间不能重复；
- `hisi` 可选；不填则这台设备不采集 HISI；
- `enabled=false` 的设备不启动；
- 最多 10 台启用；
- 可选的 `fw` 字段可覆盖全局固件类型（`auto`/`img`/`hex`）；
- 可选的 `control_baud`、`csk_baud`、`hisi_baud` 可覆盖默认波特率；
- 启用 `--wake-test` 时，建议每台设备配置自己的 `render_device_key` 和可选 `audio_file`，避免所有设备播报到同一张声卡。

## Windows 快捷入口

```text
cskflashtest\preflight.bat    # 校验 devices.json，不启动烧录
cskflashtest\run_multi.bat    # 使用 devices.json 启动多设备压测
```

`run_multi.bat` 支持直接追加参数：

```powershell
cskflashtest\run_multi.bat --rounds 100 --no-hisi
```

## 命令

```powershell
# 检查端口映射，不启动烧录
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --dry-run

# 10 台并行，每台 20 轮，默认优先 hex
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --rounds 20

# 只开 5 台并行，降低 USB/电源压力
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --rounds 20 --max-parallel 5

# 不采集 HISI
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --rounds 20 --no-hisi

# 每轮烧录后增加英文唤醒校验（最多 5 次尝试）
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --rounds 20 --wake-test --no-hisi

# 唤醒后增加命令词 ASR/音量响应校验
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --rounds 20 --interaction-test --no-hisi
```

## 结果目录

每台设备仍生成独立的 lstest 标准目录：

```text
result/<timestamp>_cskflashtest_dev01/
  serial_logs/*.log
  tool.log
  results.csv
  cases.csv

result/<timestamp>_cskflashtest_dev02/
  ...
```

多设备编排额外生成：

```text
multi_logs/<timestamp>/
  summary.csv
  summary_actual.csv
  progress.log
  dev01.console.log
  dev02.console.log
  ...
```

`summary.csv` 汇总每台设备的退出码、PASS/FAIL/BLOCKED、请求轮数、实际完成轮数 `completed`、结果目录和耗时；`progress.log` 每 30 秒记录各设备完成轮数；`summary_actual.csv` 用于异常停止时补充实际完成统计。`*.console.log` 是子进程终端输出，便于定位启动失败。它们不属于单台设备的 lstest 结果目录，不会污染四类产物。

## 停止与收尾

- 优先在每个设备的当前结果目录创建 `STOP` 文件；设备完成当前轮烧录、启动校验和 log level 设置后停止；
- 不建议直接关闭启动窗口，否则子进程可能仍在烧录；
- 普通单轮失败不会停止；连续 3 轮控制/CSK 基础设施异常会停止该设备；
- 停止后查看 `multi_logs/<timestamp>/summary.csv` 的 `completed` 和 `status`。

## 失败隔离

- 某台设备预检失败：该设备退出码 2、状态 BLOCKED，其他设备继续；
- 某台设备烧录/启动失败：该设备退出码 1、状态 FAIL，其他设备继续；
- 单台普通失败不会触发全局停止；
- 编排器最终返回码：全部 PASS 为 0，否则为 1。

## 端口稳定性建议

- 每台设备的 control/csk/burn/hisi 在物理上固定到同一个 USB Hub 端口；
- 不要在多设备运行中插拔 USB 或改变 Hub 拓扑，否则 COM 号可能变化；
- 首次接线后用 `--dry-run` 和 `python -c "from serial.tools import list_ports; ..."` 核对端口；
- 若 COM 号漂移，先固定拓扑再重启枚举，确认后再开始正式压测。

## 验证边界

当前已用 1 台真实设备验证多设备编排器端到端 PASS；10 台配置通过 `--dry-run` 校验，但尚未在 10 台真机上同时跑过。正式 10 台前建议先 2 台、再 5 台、最后 10 台逐步加量。
