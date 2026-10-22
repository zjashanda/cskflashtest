# 烧录成功校验口径

## 结论优先级

单轮结果以 `results.csv.final_status` 为准，必须同时满足控制、烧录、启动三类 required checks。任何一项 FAIL，本轮即 FAIL；工具口头提示或进度 100% 不能单独算成功。

## 控制链路

每条命令必须出现对应的设备回执：

| 命令 | 期望回执 | 实际样例 |
|---|---|---|
| `uut-switch1.off` | `switch_idx1_set: 0` | `[A/I:274247:_] switch_idx1_set: 0` |
| `uut-csk-boot.off` | `csk6012_boot_set: 0` | `[A/I:274301:_] csk6012_boot_set: 0` |
| `uut-csk-boot.on` | `csk6012_boot_set: 1` | `[A/I:274355:_] csk6012_boot_set: 1` |
| `uut-switch1.on` | `switch_idx1_set: 1` | `[A/I:274409:_] switch_idx1_set: 1` |

单轮共 7 条控制命令（4 条进入烧录 + 3 条恢复）。回执超时按 `control_retries` 重试，最终仍失败则 `control_ack=FAIL`。

## 烧录工具

以下条件全部满足才通过：

| 检查 | 证据 | 当前固件期望 |
|---|---|---|
| 退出码 | 子进程返回码 | `0` |
| 建链 | `CONNECT WITH CHIP SUCCESS` | 必须出现 |
| 芯片型号 | `CHIP TYPE = 0x...` | `0x60120001` |
| 波特率切换 | `CHANGE BAUDRATE COMMAND SUCCESS` | 必须出现 |
| 写入大小 | `WIRITE FLASH OFFSET ADDR:0x0 WRITE ... SIZE: N` | img `16777216`；hex `5685248` |
| 发送结束 | `FLASH DATA SEND END SUCCESS` | 必须出现 |
| 进度 | `FLASH DATA SEND PROGESS: 100%` | 必须出现 |
| MD5 | `MD5 CHECK SUCCESS MD5_VALUE = 0x...` | img `0x2dc44aa1d8f3c2e0dd318627a9a2fb98`；hex `0xf6d6369435d52784efe1119ebd08f582` |
| 下载完成 | `FLASH DOWNLOAD SUCCESS` 或 `FLASH 0x0 SECTOR DOWNLOAD SUCCESS` | 必须出现 |

### img 与 hex 的 MD5 差异

- `fw.img` 是 16 MB 完整镜像，工具报告的 MD5 等于文件 MD5，因此可以直接用文件 MD5 校验；
- `fw.hex` 记录的是 `0x18000000` 起的 5 个分段，工具按 4KB 对齐后写入，工具 MD5 是对工具内部数据流计算的，**不等于** `.hex` 文本文件 MD5，也不等于 16 MB 镜像 MD5；
- 已用独立脚本验证：`fw.hex` 映射到 16 MB 镜像（地址 `0x18000000 → 0x0`，空区 `0xFF`）后与 `fw.img` 完全一致，镜像 MD5 为 `0x2dc44aa1d8f3c2e0dd318627a9a2fb98`；
- hex 的工具 MD5 `0xf6d6369435d52784efe1119ebd08f582` 是已确认正常烧录的登记值，更换 hex 后必须重新登记。

## CSK 启动

| 检查 | 证据 | 当前固件期望 |
|---|---|---|
| bootloader | `>>>> bootloader application >>>>` | 出现 |
| 版本 | `venus ap appliction(V4.99.8@Oct 20 2026 14:42:43)` | 前缀 `V4.99.8` |
| 构建号 | `Project Build Info: 2026-10-20 14:42:30_1ac783d` | 完全一致 |
| 启动原因 | `Boot Reason: POWER|HWPIN 0x03` | 记录并展示，不单独作为失败条件 |
| 应用就绪 | `AP Boot Ready !!!` | 出现 |
| 致命日志 | `FATAL/ASSERT FAIL/HardFault/panic/watchdog reset/Stack overflow` | 不出现 |
| 启动次数 | `bootloader application` 计数 | ≤ 5（防止启动循环） |

注意：启动日志中允许出现 `[E][boot_task # SPI_XFER]`、`[E][rx_task # SPI_PROT] type=3 not find` 等已知非致命错误，不能简单用 `[E]` 判失败。

## 初始化后 log level

| 检查 | 证据 | 通过条件 |
|---|---|---|
| 命令 | `flash.setloglev 4` 发送到 COM13 | 命令已发送 |
| 成功回执 | `flash set log_lev:4 success` | 本次发送后的新日志中出现 |
| 重试 | 每次发送的原始回执 | 首次失败后最多重试 5 次 |
| 失败回执 | `flash write fail -1` / `cmd not found` | 出现则本次尝试失败并重试 |

注意：设备 help 登记命令为 `flash.setloglev`；`flash.setloglevel 4` 实测返回 `cmd not found`。如后续固件改为别名，需同步更新 profile 和清单中的 `csk_init.command`。

## 声卡播放与唤醒

| 检查 | 证据 | 通过条件 |
|---|---|---|
| 声卡常开 | COM14 的 `pa-enable.set 1 2 0 1` / `pa-enable.set 0 17 0 1` | `pa-enable.show` 出现 `PA Enable:` |
| 指定声卡播放 | COM14 的 `spk.uas@start!!` / `spk.uas@stop!!` | 出现 start/stop |
| 唤醒 | CSK 的 `keyword:HELLO HAIER`、`start local wakeup`、`wakeup data:`、`algo pre wakeup info` | 至少一个唤醒标记 |
| 尝试次数 | `wake.attempts` / `wake.failed_attempts` | 最多 5 次尝试；失败次数写入 facts |

注意：播放必须使用 `wake.render_device_key`，默认是 `VID_8765&PID_5678:9_2A847557_0_0000`；不能使用系统默认声卡代替。

## 命令词 ASR 与响应

| 检查 | 证据 | 通过条件 |
|---|---|---|
| 命令词播报 | 指定声卡播放 `wav/Turn-down-volume/Turn down volume_norm.wav` | `spk.uas@start!!` / `spk.uas@stop!!` |
| ASR 关键词 | CSK 的 `keyword:Turn down volume` | 出现且 `vir_kid:78` |
| 响应 | `refresh volume from X to Y` | `Y < X`，默认要求音量下降 |
| 响应提示音 | `_volume_adjust` / TONE_LANG_EN | 出现，作为设备响应旁证 |

实测：`Turn down volume` → `vir_kid:78` → `refresh volume from 70 to 60`。

## 低音量边界与 CRASH

- 实测：`40 -> 30` 后出现音量调整提示音，随后 `Boot Reason: CRASH 0x30`；重启后 `Volume: 30`；
- 再次 `30 -> 20` 后同样 `CRASH 0x30`，重启后 `Volume: 20`；
- 这不是正常的“已到最小音量”响应，而是设备固件在低音量调节/提示音路径崩溃；
- 脚本设置 `interaction.min_volume=40`：启动音量不高于 40 时跳过降音量命令，记 `volume_boundary=PASS`；
- 交互过程中出现 `Boot Reason: CRASH` 会记入 `csk_fatal`/`csk_no_fatal`，不会被误判为正常音量响应。

## 结果账本

```text
result/<timestamp>_cskflashtest/
  serial_logs/serial_COM13_csk.log
  serial_logs/serial_COM14_control.log
  serial_logs/serial_COM15_burn.log
  serial_logs/serial_COM18_hisi.log
  tool.log
  results.csv
  cases.csv
```

- `results.csv`：每轮一行，`final_status` 为最终结论；`facts_json.checks` 保存 13 项 required checks 的实际值；
- `cases.csv`：只记录已声明的轮次，轮次在设备动作前写入；
- `tool.log`：只出现 profile 捕获的原始值、动作和每轮唯一 `[RESULT]`；
- `serial_logs/*.log`：连续原始证据，行格式 `[时间] 端口 角色 #游标 原文`，`#游标` 可直接对应 `evidence`；
- 运行结束自动执行 `ToolLogValidator`，格式错误会打印并返回非零退出码。

## 失败样例

| 现象 | 判定 |
|---|---|
| 工具退出码 0，但无 `MD5 CHECK SUCCESS` | `burn_md5=FAIL` |
| MD5 值变化 | `burn_md5=FAIL`，检查固件或工具 |
| 写入大小不符 | `burn_write_size=FAIL` |
| 有 `AP Boot Ready` 但版本/构建号不符 | `csk_version`/`csk_build=FAIL` |
| 启动后出现 `FATAL`/`HardFault`/`panic` | `csk_no_fatal=FAIL` |
| bootloader 反复出现超过 5 次 | `csk_boot_count=FAIL` |
| 控制回执缺失但烧录成功 | 整轮仍 `FAIL`，因为控制时序未满足 |
| CSK 无任何日志 | `csk_bootloader`/`csk_ready` 等 `FAIL` |
| 只发 `uut-switch1.on` 后无日志 | 属于时序错误；必须先 `off` 再 `on` |
| `flash.setloglev 4` 返回 `cmd not found` | `loglevel_set=FAIL`；检查命令拼写/固件版本 |
| 重试 5 次仍无 `flash set log_lev:4 success` | `loglevel_set=FAIL` |
