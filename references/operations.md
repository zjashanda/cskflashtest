# 烧录步骤与异常处置

本文件按单轮固定时序列出每一步可能出意外的情况、检测方式和处置边界。普通失败只影响当前轮，不自动结束任务。

## 0. 预检

| 检查项 | 可能异常 | 检测 | 处置 |
|---|---|---|---|
| profile | 规则缺来源/捕获/fixture | `DeviceProfile.assert_formal_ready()` | `BLOCKED_PROFILE_CONTRACT`，不执行设备动作 |
| 固件 | 文件不存在、SHA-256 变化 | 文件存在性、`sha256_file()` | `BLOCKED_PROFILE_CONTRACT`，要求更新清单 |
| 清单 | 缺 MD5/写入大小/版本/构建号 | 清单字段检查 | `BLOCKED_PROFILE_CONTRACT` |
| 烧录工具 | exe 不存在 | 路径检查 | `INFRASTRUCTURE_UNAVAILABLE` |
| 端口 | COM13/14/15/18 不存在或被占用 | `list_ports` + 打开探测 | `INFRASTRUCTURE_UNAVAILABLE`，不进入轮次 |
| 控制 shell | 无 `root:/$` 提示符 | 发送空行并读取 2s | `INFRASTRUCTURE_UNAVAILABLE` |
| 磁盘 | 低于 `min_free_bytes` | `shutil.disk_usage` | `DISK_STOP_LINE` |
| HISI | COM18 打不开 | 打开探测 | 默认仅告警并跳过 HISI，不影响 CSK 判定 |

## 1. 下电 `uut-switch1.off`

- 期望：`switch_idx1_set: 0`。
- 可能异常：写串口失败、无回执、回执值错误、shell 无响应。
- 检测：`send_control()` 在 `control_ack_timeout_s` 内搜索回执；失败按 `control_retries` 重试。
- 处置：当前轮 `control_ack=FAIL`，继续执行后续命令；若设备仍未下电，后续烧录很可能建链失败，本轮记 `FAIL`。
- 连续写失败/串口掉线计入基础设施异常。

## 1.5 控制串口失联

- 现象：控制串口日志在某时刻后停止新增，所有 `uut-*` 命令无 `root:/$` 和回执；烧录、CSK 日志、log level 可能仍然正常。
- 检测：每轮开始发送空行探测 `root:/$`；四条进入烧录命令全部无任何回执时也判定控制链路异常。
- 处置：先重启控制串口采集线程并重试探测；仍失败则本轮 `control_alive=FAIL`，按基础设施异常累计；连续 3 轮停止该设备。
- 注意：控制 MCU 假死通常需要断电/重新插拔 USB 才能恢复，脚本不能替代硬件复位。

## 2. 退出 boot `uut-csk-boot.off`

- 期望：`csk6012_boot_set: 0`。
- 可能异常：无回执、回执值错误、命令被截断。
- 检测：同控制回执检查。
- 处置：当前轮 `FAIL`，继续进 boot 流程；重复下电/退出 boot 是幂等操作，可安全重试。

## 3. 进入 boot `uut-csk-boot.on`

- 期望：`csk6012_boot_set: 1`。
- 可能异常：无回执、回执值错误、boot 引脚未生效。
- 检测：同控制回执检查；最终由烧录工具能否 `CONNECT WITH CHIP` 旁证。
- 处置：当前轮 `FAIL`；若烧录仍成功，保留烧录证据但整轮仍判 `FAIL`，因为控制时序未满足。

## 4. 上电 `uut-switch1.on`

- 期望：`switch_idx1_set: 1`。
- 可能异常：无回执、设备未上电、电源异常导致反复掉电。
- 检测：回执 + 烧录工具建链结果 + CSK 启动日志。
- 处置：当前轮 `FAIL`；烧录工具建链超时后仍执行恢复流程。

## 5. 烧录

固定等待 `pre_burn_wait_s`（默认 1s）后启动工具。

| 异常 | 检测 | 处置 |
|---|---|---|
| COM15 被占用/打不开 | 工具输出/退出码 | 本轮 `burn_exit=FAIL`，恢复后继续下一轮 |
| 建链超时 | 无 `CONNECT WITH CHIP SUCCESS` | `burn_connect=FAIL` |
| 芯片型号不符 | `CHIP TYPE` 与清单不一致 | `burn_chip=FAIL` |
| RAM loader 失败 | 无 `CONNECT ROM AND DOWNLOAD RAM LOADER SUCCESS` | 由后续 MD5/完成标记缺失判 `FAIL` |
| 波特率切换失败 | 无 `CHANGE BAUDRATE COMMAND SUCCESS` | `burn_baud=FAIL` |
| 写入大小不符 | `WRITE SIZE` 与清单不一致 | `burn_write_size=FAIL` |
| 写 Flash 卡住 | 超过 `burn_timeout_s`（默认 600s） | 终止工具，`burn_exit=FAIL`，继续恢复 |
| 数据错误 | `MD5 CHECK FAIL` 或 MD5 与清单不一致 | `burn_md5=FAIL` |
| 工具崩溃/非零退出 | 退出码非 0 | `burn_exit=FAIL` |
| 进度到 100% 但无结束标记 | 无 `FLASH DATA SEND END SUCCESS` | `burn_send_end=FAIL` |
| 无下载完成标记 | 无 `FLASH DOWNLOAD SUCCESS` / `SECTOR DOWNLOAD SUCCESS` | `burn_done=FAIL` |

烧录工具原始 stdout 全部逐行写入 `serial_logs/serial_COM15_burn.log`，进度行也保留。

## 6. 退出 boot `uut-csk-boot.off`

- 期望：`csk6012_boot_set: 0`。
- 可能异常：无回执、boot 仍保持高电平。
- 处置：当前轮 `FAIL`；若 boot 未退出，后续正常启动日志会缺失，最终由 CSK 检查判 `FAIL`。
- 恢复流程会重试退出 boot。

## 7. 重新上电 `uut-switch1.off` → `uut-switch1.on`

- 注意：只发 `on` 不会产生复位；必须先 `off`。
- 可能异常：`off` 无回执、`on` 无回执、上电后无启动日志。
- 处置：当前轮 `FAIL`；仍等待 CSK 日志，超时后结束本轮。
- 恢复流程在异常中断时也会尝试执行这三条命令。

## 8. 观察 CSK 启动日志

等待 `boot_observe_timeout_s`（默认 30s），命中 `AP Boot Ready !!!` 或致命日志后提前结束，再额外观察 `post_ready_settle_s`（默认 2s）。

| 异常 | 检测 | 处置 |
|---|---|---|
| 无 bootloader | 窗口内无 `bootloader application` | `csk_bootloader=FAIL` |
| 版本不符 | 版本前缀与清单不一致 | `csk_version=FAIL`，可能烧错固件或清单过期 |
| 构建号不符 | `Project Build Info` 与清单不一致 | `csk_build=FAIL` |
| 未就绪 | 无 `AP Boot Ready !!!` | `csk_ready=FAIL` |
| 致命异常 | `FATAL/ASSERT FAIL/HardFault/panic/watchdog reset/Stack overflow` | `csk_no_fatal=FAIL` |
| 启动循环 | `bootloader application` 次数超过上限（默认 5） | `csk_boot_count=FAIL` |
| CSK 串口掉线 | 读取异常、重连 3 次仍失败 | 当前轮 `FAIL`；连续 3 轮计入基础设施停止 |
| 旧日志混入 | 游标从本轮 `CaseWindow` 开始，启动等待从重新上电前开始 | 版本/构建取最后一次匹配，boot 次数按窗口统计 |

## 8.5 初始化后设置 log level

- 触发时机：`AP Boot Ready !!!` 后额外等待 `post_ready_settle_s + csk_init_wait_s`。
- 发送端口：`COM13`，命令 `flash.setloglev 4`。
- 成功回执：`flash set log_lev:4 success`。
- 可能异常：
  - `cmd not found`：命令未注册，通常是固件版本或命令拼写不一致；注意设备 help 中登记的是 `flash.setloglev`；
  - `flash write fail -1`：NVS/Flash 写入失败，常见于初始化尚未完成或 Flash 区域被占用；
  - 无任何回执：CSK shell 未就绪或串口写失败；
  - 回执为旧日志中的 success：脚本按发送前游标过滤，不会误判。
- 处置：首次失败后最多重试 5 次；仍失败则本轮 `loglevel_set=FAIL`，完成收尾后继续下一轮。
- 注意：log level 4 会显著增加后续 CSK 日志量，长时间压测需关注磁盘。

## 9. 收尾与继续

- 每轮结束时关闭 `CaseWindow`，将本轮 checks、烧录结果、启动结果写入 `results.csv`；
- `PASS` 需要全部 required checks 通过；
- 任一轮 `FAIL`：记录证据、恢复设备、继续下一轮；
- 连续 3 轮控制/CSK 基础设施异常：`INFRASTRUCTURE_UNAVAILABLE` 停止；
- 用户 `Ctrl+C` 或 `STOP` 文件：`USER_STOP` 停止；
- `tool.log`/`results.csv` 写入失败：`EVIDENCE_WRITE_FAILURE` 停止；
- 磁盘低于阈值：`DISK_STOP_LINE` 停止。
