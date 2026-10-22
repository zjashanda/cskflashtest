---
name: cskflashtest
description: CSK 串口烧录与烧录压测项目 Skill。Use when 需要通过控制串口给 CSK 设备下电、进 boot、上电、执行 Uart_Burn_Tool 烧录、退出 boot、重新上电并校验 CSK 启动日志；也用于多轮烧录压力测试、烧录日志留存、失败定位与结果统计。
---

# cskflashtest

本 Skill 只服务当前 CSK 烧录项目，不复制、不修改 `lstest`。运行时把 `lstest` 当作公共库，复用其 `TaskArtifacts`、`DeviceProfile`、`ScenarioRuntime`、`ToolLogValidator` 和四类产物标准。

## 适用场景

- 单次 CSK 串口烧录验证；
- 多轮烧录压力测试；
- 烧录失败定位、启动日志取证、版本一致性校验；
- 需要长期保留烧录工具原始输出和 CSK 连续日志的任务。

## 端口与关键文件

| 用途 | 端口/文件 | 参数 |
|---|---|---|
| 控制串口 | `COM14` | `115200`，shell 提示符 `root:/$` |
| CSK 日志 | `COM13` | `115200`，持续保存 |
| CSK 烧录串口 | `COM15` | 由 `Uart_Burn_Tool.exe` 独占 |
| HISI 日志 | `COM18` | `1500000`，默认采集，可用 `--no-hisi` 关闭 |
| 烧录工具 | `burn/Uart_Burn_Tool.exe` | 工作目录 `burn/` |
| 固件清单 | `config/firmware-manifest.json` | 固件路径、SHA-256、预期 MD5、写入大小、版本、构建号 |
| 日志 profile | `config/burn-profile.json` | 控制回执、烧录旁证、CSK 启动事实的正则合同 |

## 附带资源

仓库已包含运行所需资源：

- `burn/Uart_Burn_Tool.exe`：CSK 烧录工具；
- `burn/fw.img`：整镜像固件；
- `fw/V46.99.8_20261020144346/CSK/fw.hex`：hex 固件；
- `wav/Wake/hellohaier.wav`：唤醒词；
- `wav/Turn-down-volume/Turn down volume_norm.wav`：交互命令词；
- `wav/` 下的完整意图语料，供命令词扫描使用。

公共框架 `lstest` 不复制进本仓库；运行时需将 `lstest` 放在项目根目录（与本 Skill 同级），脚本通过 `lstest.scripts` 调用公共运行时和播放器。

## 快速开始

```powershell
# 1. 无设备动作预检（profile fixture、固件哈希、端口、控制 shell）
python -u cskflashtest/scripts/run.py --preflight

# 2. 单轮实际烧录，默认优先 hex；没有 hex 自动使用 img
python -u cskflashtest/scripts/run.py --rounds 1

# 3. 指定固件类型
python -u cskflashtest/scripts/run.py --rounds 1 --fw img
python -u cskflashtest/scripts/run.py --rounds 1 --fw hex

# 4. 不烧录，只验证控制时序和 CSK 启动日志
python -u cskflashtest/scripts/run.py --rounds 1 --skip-burn --no-hisi

# 5. 压力测试，例如 100 轮，不采集 HISI（默认优先 hex）
python -u cskflashtest/scripts/run.py --rounds 100 --no-hisi
```

`--rounds` 为必填轮数。运行中可按 `Ctrl+C` 停止，或在结果目录创建 `STOP` 文件；当前轮收尾后停止。

### 多设备并行

最多支持 10 台设备同时压测。先在 `config/devices.json` 为每台设备填写独立的 control/csk/burn 端口，hisi 可选，并设置 `enabled=true`：

```powershell
# 校验 10 台端口映射，不启动烧录
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --dry-run

# 最多 10 台并行，每台 20 轮
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --rounds 20

# 稳定性不足时降低并行数
python -u cskflashtest/scripts/run_multi.py --devices cskflashtest/config/devices.json --rounds 20 --max-parallel 5
```

每台设备生成独立的 `result/<timestamp>_cskflashtest_<id>/` 标准目录；编排汇总和每台终端输出位于 `multi_logs/<timestamp>/`。详细端口要求、失败隔离和加量建议见 [references/multi-device.md](references/multi-device.md)。

## Windows 快捷入口

```text
cskflashtest\preflight.bat    # 只校验 devices.json 端口映射
cskflashtest\run_multi.bat    # 读取 devices.json 执行多设备压测
```

`run_multi.bat` 会把额外参数原样传给 `run_multi.py`：

```powershell
cskflashtest\run_multi.bat --rounds 100 --no-hisi
cskflashtest\run_multi.bat --rounds 100 --max-parallel 5 --stagger 2
```

## CSK 初始化完成标记

- 主标记：`[A/W:181:BOOT] AP Boot Ready !!!`
- 后续会打印 `Project Config` 配置块，并以 `==========================================` 结束；
- 当前触发：出现 `AP Boot Ready !!!` 后等待 `post_ready_settle_s`（默认 2s）；
- 20 轮压测中该触发方式 20/20 首次设置 log level 成功。

## 单轮固定时序

每个控制命令通过 `COM14` 发送，命令以 `\r` 结束，命令后固定等待 `0.5s`；等待回执超时会按 `control_retries` 重试，重试仍遵守 0.5s 间隔。

1. `uut-switch1.off`：下电，期望 `switch_idx1_set: 0`；
2. 等 `0.5s`；
3. `uut-csk-boot.off`：退出 boot，期望 `csk6012_boot_set: 0`；
4. 等 `0.5s`；
5. `uut-csk-boot.on`：进入 boot，期望 `csk6012_boot_set: 1`；
6. 等 `0.5s`；
7. `uut-switch1.on`：上电，期望 `switch_idx1_set: 1`；
8. 等 `1s` 后启动 `Uart_Burn_Tool.exe`，等待工具结束；
9. `uut-csk-boot.off`：退出 boot；
10. `uut-switch1.off`：下电；
11. `uut-switch1.on`：重新上电，触发正常启动；
12. 持续采集 `COM13`，直到观察到 `AP Boot Ready !!!` 或超时，并额外观察 `post_ready_settle_s` 防止启动后异常。

> 关键修正：第 10 步不能省略。实测只执行 `uut-switch1.on` 不会复位设备，必须先 `off` 再 `on` 才会产生 CSK 启动日志。

## 烧录命令

实际执行的命令由 `BurnToolRunner.build_args()` 生成，等价于：

```powershell
# img
.\Uart_Burn_Tool.exe -b 1500000 -p COM15 -c -t 10 -f <fw.img> -l -m -d -a 0x0 -s

# hex
.\Uart_Burn_Tool.exe -b 1500000 -p COM15 -c -t 10 -f <fw.hex> -l -m -d 0x00 -s
```

`-t 10` 只表示建链超时，不是总烧录超时；总超时由 `timing.burn_timeout_s`（默认 600s）控制，超时会终止工具，随后仍执行退出 boot 和重新上电。

## 初始化后设置 log level

设备启动到 `AP Boot Ready !!!` 并额外稳定 `post_ready_settle_s` 后，脚本通过 `COM13` 发送：

```text
flash.setloglev 4
```

期望回执：

```text
flash set log_lev:4 success
```

- 设备 `help` 中登记的命令是 `flash.setloglev`，不是 `flash.setloglevel`；实测 `flash.setloglevel 4` 返回 `cmd not found`，因此脚本使用 `flash.setloglev 4`；
- 首次失败后最多重试 5 次（总共最多 6 次发送），每次独立等待 `attempt_timeout_s`（默认 3s）；
- 成功条件：在本次发送后的新日志中出现 `flash set log_lev:4 success`；
- 5 次重试仍失败时，本轮 `loglevel_set=FAIL`，不会静默忽略；
- 设置成功后才结束本轮；设置产生的 `log_lev` 事实会进入 `tool.log` 和 `results.csv`。

## 声卡播报与唤醒校验

如果指定 Render 声卡无法唤醒设备，先通过**控制串口 COM14** 设置音频常开：

```text
pa-enable.set 1 2 0 1
pa-enable.set 0 17 0 1
pa-enable.show
```

`pa-enable.show` 出现 `PA Enable:` 表示设置生效。之后用指定声卡播报时，控制串口会出现：

```text
spk.uas@start!!
spk.uas@stop!!
```

这两行表示声卡确实在播报。CSK 日志中以下标记表示唤醒成功：

```text
keyword:HELLO HAIER
wakeup data:
start local wakeup
algo pre wakeup info
```

唤醒测试命令：

```powershell
python -u cskflashtest/scripts/run.py --devices cskflashtest/config/devices.json --device-id dev01 --rounds 1 --skip-burn --wake-test --no-hisi
```

- 使用 `wake.render_device_key` 指定的声卡，不回落系统默认声卡；
- 初始化完成后切换英文 `algo.switch.lang 0`，再播报唤醒词；
- 每次唤醒失败最多尝试 5 次，每轮把失败次数写入 `wake_failures` / `wake.failed_attempts`；
- 5 次均失败时本轮 `wake_result=FAIL`，但不会中断后续轮次；
- 多设备可加 `--wake-test`：`run_multi.bat --rounds 1 --wake-test`。

## 命令词交互校验

唤醒成功后播报命令词 `Turn down volume`（已转换为 16k 单声道 WAV，并使用归一化版本）：

```text
wav/Turn-down-volume/Turn down volume.wav
wav/Turn-down-volume/Turn down volume_norm.wav
```

期望结果：

```text
keyword:Turn down volume
vir_kid:78
refresh volume from <before> to <after>
```

- 校验 ASR 关键词、`vir_kid` 和音量变化；
- 默认要求音量下降，例如 `70 -> 60`；
- 设备侧响应日志包含 `_volume_adjust` / 音量调整提示音；
- 实测结果：`Turn down volume` 识别正确，音量 `70 -> 60`。

命令：

```powershell
python -u cskflashtest/scripts/run.py --devices cskflashtest/config/devices.json --device-id dev01 --rounds 1 --skip-burn --wake-test --interaction-test --no-hisi
```

`--interaction-test` 会自动启用唤醒测试；多设备时加 `--interaction-test`，并为每台设备配置自己的 `render_device_key`。

低音量边界：

- 实测音量 `40 -> 30`、`30 -> 20` 后设备出现 `Boot Reason: CRASH 0x30`，这不是正常的“最小音量”响应；
- 为避免重复触发固件崩溃，`interaction.min_volume` 默认 40：启动后音量不高于 40 时跳过降音量命令，记录 `volume_boundary=PASS`；
- 交互日志若出现 `Boot Reason: CRASH` / `CSK AP Ready after exception`，会按设备异常记录，不会被当作正常音量响应。

## 烧录成功判定

单轮 PASS 需要同时满足：

1. 7 条控制命令均收到正确回执；
2. 烧录工具退出码为 `0` 且未超时；
3. 工具日志包含 `CONNECT WITH CHIP SUCCESS`、`CHIP TYPE = <清单值>`、`CHANGE BAUDRATE COMMAND SUCCESS`；
4. 写入大小等于清单 `expected_write_size`；
5. 出现 `FLASH DATA SEND PROGESS: 100%`、`FLASH DATA SEND END SUCCESS`；
6. `MD5 CHECK SUCCESS` 且 MD5 等于清单 `expected_tool_md5`；
7. 出现 `FLASH DOWNLOAD SUCCESS` 或 `FLASH <addr> SECTOR DOWNLOAD SUCCESS`；
8. CSK 日志出现 `bootloader application`、预期版本前缀、预期构建号、`AP Boot Ready !!!`；
9. 启动窗口中未出现 `FATAL`、`ASSERT FAIL`、`HardFault`、`panic`、`watchdog reset`、`Stack overflow`；
10. `bootloader application` 次数不超过清单上限（默认 5 次，防启动循环）；
11. 初始化完成后 `flash.setloglev 4` 在最多 5 次重试内返回 `flash set log_lev:4 success`。

`img` 的 `expected_tool_md5` 等于固件文件 MD5。`hex` 的工具 MD5 是工具对解析/补齐后数据流计算的 MD5，不等于 `.hex` 文本文件或完整 16MB 镜像的 MD5；清单中保存的是已确认正常烧录的工具值，更换 hex 后必须用 `scripts/commission.py` 重新登记。

## 失败处理与停止边界

- 单轮控制失败、烧录失败、校验失败、启动失败只把当前轮记为 `FAIL`，收尾后继续下一轮；
- 控制串口/CSK 串口掉线会自动重连 3 次；连续 3 轮基础设施异常才请求 `INFRASTRUCTURE_UNAVAILABLE` 停止；
- 单条原始行解析失败、启动日志缺失、MD5 不一致、工具非零退出都不是任务自动退出理由；
- 只有用户停止、证据无法可靠写入、正式 profile 预检阻塞、必需基础设施恢复耗尽、磁盘低于阈值才停止；
- 每轮无论成功失败都会尝试恢复：退出 boot、下电、上电。

详细逐步故障矩阵见 [references/operations.md](references/operations.md)，校验口径见 [references/verification.md](references/verification.md)。

## 结果产物

每次运行生成：

```text
result/<timestamp>_cskflashtest/
  serial_logs/
    serial_COM13_csk.log       # CSK 连续原始日志
    serial_COM14_control.log   # 控制串口原始日志
    serial_COM15_burn.log      # 烧录工具逐行原始输出
    serial_COM18_hisi.log      # HISI 原始日志（可关闭）
  tool.log                     # 唯一人读执行账本
  results.csv                  # 每轮一行结果
  cases.csv                    # 已执行轮次在动作前冻结
```

- `serial_logs/*.log` 每行格式：`[时间] 端口 角色 #游标 原始内容`；
- `tool.log` 只显示 profile 捕获的原始事实和动作/结果；
- 结果目录不产生 `.bin`、`task.log`、JSONL 或额外临时日志；
- HISI 日志约 7 MB/轮（3.5 分钟级），长时间压测建议 `--no-hisi` 或预留足够磁盘；
- 多设备运行时每台设备仍有独立四类产物；`multi_logs/<timestamp>/summary.csv` 汇总各设备结果，`*.console.log` 保存子进程输出，不会写入单台设备的四类产物目录。

## 固件变更

```powershell
# 查看当前 img/hex 的 SHA-256、写入大小和映射镜像 MD5
python cskflashtest/scripts/commission.py --key img
python cskflashtest/scripts/commission.py --key hex

# 固件更新后登记新哈希；hex 需要提供一次已确认正常烧录的工具 MD5
python cskflashtest/scripts/commission.py --key img --update
python cskflashtest/scripts/commission.py --key hex --update --tool-md5 0x...
```

固件 SHA-256 与清单不一致时预检会阻塞，不允许带未知固件进入正式压测。

## 非目标

- 本 Skill 不负责 HISI 固件烧录；
- 不把烧录工具输出写进 `tool.log`；
- 不修改 `lstest` 目录；
- 不在预检失败时猜测日志含义或继续上电烧录。
