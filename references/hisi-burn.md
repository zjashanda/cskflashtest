# HISI / WS63 烧录与验证

本文记录 `VenusA+WS63` 工具链的 HISI（WS63）烧录方式。它与 CSK 的 `Uart_Burn_Tool.exe` 是两套独立流程。

## 1. 工具与固件

| 用途 | 路径 |
|---|---|
| HISI 烧录工具 | `burn/VenusA+WS63/VenusA+WS63/BurnTool_Gold/BurnTool.exe` |
| HISI 命令行入口 | `burn/VenusA+WS63/VenusA+WS63/auto_burn.py` |
| HISI 固件包 | `fw/V46.1.8_20261102093954/HISI/ws63-liteos-app_all.fwpkg` |
| OTA 包（不要用于 BurnTool） | `fw/V46.1.8_20261102093954/HISI/out/update.fwpkg` |

`VenusA_Burn/Uart_Burn_Tool.exe` 只能用于 CSK/VenusA，不能用于 HISI。

工具包是厂商 GUI/命令行包，已纳入仓库：

```text
burn/VenusA+WS63/VenusA+WS63/
```

运行时日志 `BurnTool_Gold/optLog/`、`BurnTool_Gold/tmp/`、`BurnTool_Gold/log.txt` 和旧的临时 fwpkg 不入库，避免把历史运行数据混进仓库。

`auto_burn.py` 会从这里读取 `BurnTool_Gold/BurnTool.exe`。如果目标机器没有该目录，直接从仓库拉取 `burn/VenusA+WS63` 即可。

仓库版本已做精简：移除 `configure/config_mcu`（纯 MCU/JTAG 配置，WS63 UART 烧录不需要），保留 WS63/CSK 需要的 `BurnTool.exe`、Qt DLL、`config_WS63`、WS63 chip-type 配置和 `VenusA_Burn`。精简后已用同一套工具重新烧录 dev01，`烧写结果：成功` 且 `AT+FTM=0` 返回 `OK`。

## 2. 固件校验

HISI 固件包 MD5 必须与版本目录的 `md5_20261102093954.txt` 中 `hisi_md5` 一致：

```text
4A94BE733DE61E45DF4BFE6D0B44A9F3
```

`out/update.fwpkg` 的 MD5 不同，它是 OTA 包，不能传给 `BurnTool.exe -bin:`。

## 3. 串口参数

| 用途 | 参数 |
|---|---|
| 控制串口 | `115200` |
| BurnTool 烧录协议 | `-signalbaud:1000000` |
| HISI 应用日志 / `AT+FTM=0` | `1500000 8N1` |
| CSK 日志 | `115200` |

注意：`1000000` 是 BurnTool 的 `-signalbaud`，不是 HISI 日志波特率。HISI 日志用 `921600`、`1000000` 读取会乱码。

## 4. 单台设备命令

以 dev01 为例：

```powershell
cd D:\workstation\quattro\102200cskflashst\burn\VenusA+WS63\VenusA+WS63

python -u auto_burn.py `
  --firmware-root "D:\workstation\quattro\102200cskflashst\fw\V46.1.8_20261102093954\HISI" `
  --control-port COM16 `
  --ws63-port COM14 `
  --skip-venusa `
  --ws63-at-baud 1500000 `
  --ws63-burn-kick-delay-ms 1500
```

- `--skip-venusa`：只烧 HISI，不烧 CSK。
- `--ws63-at-baud 1500000`：当前 HISI 固件的 AT/日志波特率；脚本默认的 `921600` 会导致 AT 校验乱码。
- `--ws63-burn-kick-delay-ms 1500`：先让 BurnTool 打开串口，再给设备重新上电，降低偶发 `失败，15`。

`auto_burn.py` 实际的 BurnTool 命令等价于：

```powershell
BurnTool.exe `
  -com:14 `
  -bin:D:\workstation\quattro\102200cskflashst\fw\V46.1.8_20261102093954\HISI\ws63-liteos-app_all.fwpkg `
  -signalbaud:1000000 `
  -onlyburn:root_loaderboot_sign.bin `
  -onlyburn:root_params_sign.bin `
  -onlyburn:ssb_sign.bin `
  -onlyburn:flashboot_sign.bin `
  -onlyburn:flashboot_backup_sign.bin `
  -onlyburn:ws63-liteos-app-sign.bin `
  -onlyburn:efuse_cfg.bin `
  -onlyburn:ws63-liteos-mfg-sign.bin
```

然后脚本通过控制串口发送：

```text
uut-switch1.off
uut-switch1.on
```

让设备进入 WS63 烧录模式。

## 5. 多台设备端口

| 设备 | control | hisi | 烧录命令中的 `--ws63-port` |
|---|---|---|---|
| dev01 | COM16 | COM14 | COM14 |
| dev02 | COM19 | COM21 | COM21 |
| dev03 | COM32 | COM29 | COM29 |
| dev04 | COM11 | COM10 | COM10 |
| dev05 | COM6 | COM4 | COM4 |

每台设备使用自己的 `--control-port` 和 `--ws63-port`，不要并行启动多个共用同一 `BurnTool_Gold` 工作目录的 BurnTool 实例。

## 6. 烧录成功校验

### 6.1 BurnTool 结果

查看最新日志：

```text
burn/VenusA+WS63/VenusA+WS63/BurnTool_Gold/optLog/optLog_*.txt
```

成功必须出现：

```text
烧写结果：成功
```

失败示例：

```text
烧写结果：失败，15
```

失败时不要修改 CSK 固件或伪造结果，先重新上电、确认端口未被占用，再按上面的命令重试。

### 6.2 HISI AT 校验

烧录后重新上电，等待启动，再在 HISI 串口发送：

```text
AT+FTM=0
```

成功响应应包含：

```text
+FTM SWITCH: start
FTM SWTICH:0x230000, size:0x23b000 OK.
OK
```

### 6.3 CSK 启动校验

CSK 日志应出现预期版本和构建号：

```text
venus ap appliction(V4.1.8@Nov  2 2026 09:21:24)
Project Build Info: 2026-11-02 09:21:11_1cdeeab
AP Boot Ready !!!
```

### 6.4 HISI 启动校验

HISI 日志应出现：

```text
ListenAI APP Version: 46.1.8
CSK6 Ready !!!
```

`CSK6 not Ready, Checking...90` 是 HISI 等待 CSK 握手的中间状态，不能单独当作烧录成功或失败。

## 7. 连续唤醒验证

烧录成功不等于整机功能通过，必须再做上电后的连续唤醒和异常重启验证。`run.py` 的 `--wake-repeat` 会在**同一次上电**后连续执行指定次数，不会每轮重新上电。

单台 10 次：

```powershell
python -u scripts/run.py `
  --mode powercycle `
  --rounds 1 `
  --devices config/devices.json `
  --device-id dev01 `
  --wake-test `
  --wake-repeat 10 `
  --acceptance wake_only
```

5 台顺序执行：

```powershell
python -u scripts/run_multi.py `
  --mode powercycle `
  --devices config/devices.json `
  --rounds 1 `
  --wake-test `
  --wake-repeat 10 `
  --acceptance wake_only `
  --max-parallel 1
```

不要加 `--no-hisi`，否则无法同时保存 HISI 复位/握手日志。判定时必须同时看：

- 唤醒成功标记 `start local wakeup` / `keyword:HELLO HAIER`；
- CSK 是否出现额外 `bootloader application`；
- CSK 是否出现 `Boot Reason: POWER|HWPIN`、`CRASH`、`FATAL`；
- HISI 是否出现 `reset_reason: reset` 和 `CSK6 not Ready/Ready`。

## 8. 日志保存

至少保存：

```text
BurnTool_Gold/optLog/optLog_<timestamp>.txt
HISI_COM<port>.log
CSK_COM<port>.log
```

测试时不要使用 `--no-hisi` 省略 HISI；CSK 与 HISI 必须同时留证。

## 9. 2026-11-02 验证记录

V46.1.8 的 HISI 固件在 5 台设备上均完成烧录，BurnTool 均返回 `烧写结果：成功`，AT 校验均返回 `OK`。

但烧录后的功能验证未通过：

- 5 台设备每次上电后连续唤醒 10 次，均为 `10/10` 失败；
- 每次播放唤醒词后 CSK 出现 `bootloader application`，`Boot Reason: POWER|HWPIN 0x03`，没有任何 `start local wakeup` / `keyword:HELLO HAIER`；
- 不播放唤醒词、仅重新上电等待 30 秒，CSK 仍出现 4 次启动记录（首次上电加至少 3 次重启），说明重启不是唤醒音频单独触发；
- HISI 日志同时出现 `reset_reason: reset`、`CSK6 not Ready`、`CSK6 Ready`，但没有明确的“HISI 主动复位 CSK”命令记录。

因此当前状态只能记录为：

```text
HISI 烧录成功
CSK/HISI 启动后可握手
但整机存在周期复位/异常重启，功能验收不通过
```

在复位原因定位前，不要把该版本标记为“正常使用”或“稳定性通过”。
