# cskflashtest

CSK 串口烧录与烧录压测项目 Skill。Use when 需要通过控制串口给 CSK 设备下电、进 boot、上电、执行 Uart_Burn_Tool 烧录、退出 boot、重新上电并校验 CSK 启动日志；也用于多轮烧录压力测试、烧录日志留存、失败定位与结果统计。

## Skill layout

- `.gitignore`
- `SKILL.md`
- `agents/openai.yaml`
- `burn/VenusA+WS63/VenusA+WS63/auto_burn.py`
- `config/asset-bootstrap.json`
- `config/burn-profile.json`
- `config/devices.example.json`
- `config/firmware-manifest.json`
- `config/hisi-burn-manifest.json`
- `install_requirements.bat`
- `preflight.bat`
- `preflight_powercycle.bat`
- `references/hisi-burn.md`
- `references/long-run-stability.md`
- `references/manual-assets.md`
- `references/multi-device.md`
- `references/operations.md`
- `references/verification.md`
- `requirements.txt`
- `run_multi.bat`
- `run_powercycle.bat`
- `scripts/burn_adapter.py`
- `scripts/commission.py`
- `scripts/ensure_assets.py`
- `scripts/lstest_env.py`
- `scripts/run.py`
- `scripts/run_multi.py`
- `scripts/scan_commands.py`
- `tests/test_burn_profile.py`
- `tests/test_long_run_soak.py`
- `tests/test_multi_device.py`
- `tests/test_serial_fact_store.py`
- `verify_burn.bat`
- `verify_powercycle.bat`
- `wav/Adjust-to-coldest__BH123_SE_F001_normal_073.wav`
- `wav/Adjust-to-hottest__BH123_SE_F001_normal_072.wav`
- `wav/Automatic-fan-speed__BH123_SE_F001_normal_096.wav`
- `wav/Automatic-mode__BH123_SE_F002_normal_069.wav`
- `wav/Avoid-me__BH123_SE_F001_normal_124.wav`
- `wav/Blow-downward__BH123_SE_F001_normal_117.wav`
- `wav/Blow-on-me__BH123_SE_F001_normal_121.wav`
- `wav/Blow-to-the-left__BH123_SE_F003_normal_111.wav`
- `wav/Blow-to-the-middle__BH123_SE_F001_normal_119.wav`
- `wav/Blow-to-the-right__BH123_SE_F004_normal_113.wav`
- `wav/Blow-upward__BH123_SE_F004_normal_115.wav`
- `wav/Cooling-mode__BH123_SE_F001_normal_065.wav`
- `wav/De-humidification-mode__BH123_SE_F001_normal_067.wav`
- `wav/Decrease-fan-speed__BH123_SE_F001_normal_092.wav`
- `wav/Decrease-temperature__BH123_SE_F001_normal_071.wav`
- `wav/Eighteen-degrees__BH123_SE_F001_normal_078.wav`
- `wav/Fan-mode__BH123_SE_F004_normal_068.wav`
- `wav/Follow-me__BH123_SE_F010_normal_122.wav`
- `wav/Heating-mode__BH123_SE_F001_normal_066.wav`
- `wav/High-fan-speed__BH123_SE_F004_normal_093.wav`
- `wav/Increase-fan-speed__BH123_SE_F001_normal_091.wav`
- `wav/Increase-temperature__BH123_SE_F001_normal_070.wav`
- `wav/Low-fan-speed__BH123_SE_F001_normal_095.wav`
- `wav/Mid-fan-speed__BH123_SE_F001_normal_094.wav`
- `wav/Nineteen-degrees__BH123_SE_F001_normal_079.wav`
- `wav/Seventeen-degrees__BH123_SE_F001_normal_077.wav`
- `wav/Sixteen-degrees__BH123_SE_F002_normal_076.wav`
- `wav/Swing-to-the-downward__BH123_SE_F001_normal_118.wav`
- `wav/Swing-to-the-left__BH123_SE_F001_normal_112.wav`
- `wav/Swing-to-the-middle__BH123_SE_F003_normal_120.wav`
- `wav/Swing-to-the-right__BH123_SE_F004_normal_114.wav`
- `wav/Swing-to-the-upward__BH123_SE_F004_normal_116.wav`
- `wav/Thirty-degrees__BH123_SE_F001_normal_090.wav`
- `wav/Too-cold__BH123_SE_F001_normal_075.wav`
- `wav/Too-hot__BH123_SE_F001_normal_074.wav`
- `wav/Turn-down-volume__Turn down volume_norm.wav`
- `wav/Turn-off-AC__BH123_SE_F001_normal_064.wav`
- `wav/Turn-off-ECO__BH123_SE_F004_normal_134.wav`
- `wav/Turn-off-Health__BH123_SE_F003_normal_126.wav`
- `wav/Turn-off-aircon__BH123_SE_F003_normal_063.wav`
- `wav/Turn-off-left-right-swing__BH123_SE_F001_normal_099.wav`
- `wav/Turn-off-quiet-mode__BH123_SE_F002_normal_104.wav`
- `wav/Turn-off-self-cleaning__BH123_SE_F001_normal_102.wav`
- `wav/Turn-off-turbo__BH123_SE_F002_normal_110.wav`
- `wav/Turn-off-up-down-swing__BH123_SE_F001_normal_100.wav`
- `wav/Turn-on-AC__BH123_SE_F003_normal_062.wav`
- `wav/Turn-on-ECO-level-1__BH123_SE_F003_normal_129.wav`
- `wav/Turn-on-ECO-level-2__BH123_SE_F001_normal_130.wav`
- `wav/Turn-on-ECO-level-3__BH123_SE_F004_normal_131.wav`
- `wav/Turn-on-ECO-level-4__BH123_SE_F001_normal_132.wav`
- `wav/Turn-on-ECO-level-5__BH123_SE_F001_normal_133.wav`
- `wav/Turn-on-Energy-Saving-Mode__BH123_SE_F001_normal_128.wav`
- `wav/Turn-on-Health__BH123_SE_F001_normal_125.wav`
- `wav/Turn-on-Light__BH123_SE_F001_normal_135.wav`
- `wav/Turn-on-aircon__BH123_SE_F003_normal_061.wav`
- `wav/Turn-on-left-right-swing__BH123_SE_F001_normal_097.wav`
- `wav/Turn-on-up-down-swing__BH123_SE_F001_normal_098.wav`
- `wav/Turn-up-volume__BH123_SE_F001_normal_105.wav`
- `wav/Turn-volume-down-to-minimum__BH123_SE_F001_normal_108.wav`
- `wav/Turn-volume-up-to-maximum__BH123_SE_F001_normal_107.wav`
- `wav/Turnonquietmode__BH123_SE_F002_normal_103.wav`
- `wav/Turnonself-cleaning__BH123_SE_F001_normal_101.wav`
- `wav/Turnonturbo__BH123_SE_F001_normal_109.wav`
- `wav/Turu-off-Light__BH123_SE_F004_normal_136.wav`
- `wav/Twenty-degrees__BH123_SE_F002_normal_080.wav`
- `wav/Twenty-eight-degrees__BH123_SE_F001_normal_088.wav`
- `wav/Twenty-five-degrees__BH123_SE_F002_normal_085.wav`
- `wav/Twenty-four-degrees__BH123_SE_F001_normal_084.wav`
- `wav/Twenty-nine-degrees__BH123_SE_F001_normal_089.wav`
- `wav/Twenty-one-degrees__BH123_SE_F001_normal_081.wav`
- `wav/Twenty-seven-degrees__BH123_SE_F001_normal_087.wav`
- `wav/Twenty-six-degrees__BH123_SE_F001_normal_086.wav`
- `wav/Twenty-three-degrees__BH123_SE_F001_normal_083.wav`
- `wav/Twenty-two-degrees__BH123_SE_F001_normal_082.wav`
- `wav/Wake__hellohaier_norm.wav`

## Install the skill

Copy this folder into:

```text
~/.codex/skills/cskflashtest
~/.config/opencode/skills/cskflashtest
```

Then restart Codex or OpenCode.

## Usage and workflow

本 Skill 只服务当前 CSK 烧录项目，不复制、不修改 `lstest`。运行时把 `lstest` 当作公共库，复用其 `TaskArtifacts`、`DeviceProfile`、`ScenarioRuntime`、`ToolLogValidator` 和四类产物标准。

云端只分发脚本、配置、文档和 WAV；本地烧录固件与厂商工具保持原样，不上传。其他客户端下载后，烧录预检或 HISI 入口会优先从 OSS 资产包恢复缺失文件；无网络时按 [手动资产清单](references/manual-assets.md) 手动补齐。未补齐前只能使用无需烧录资产的功能，不能直接烧录。

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

云端导入包只包含脚本、配置、文档和 WAV；烧录工具及固件均**不随云端上传**，本地已验证的文件保持原样。烧录前按 [references/manual-assets.md](references/manual-assets.md) 手动添加并校验。以下烧录路径是手动补齐的目标位置：

- `burn/Uart_Burn_Tool.exe`：CSK 烧录工具；
- `fw/V46.1.8_20261102093954/CSK/fw.hex`：hex 固件；
- `fw/V46.1.8_20261102093954/HISI/ws63-liteos-app_all.fwpkg`：HISI/WS63 烧录包；
- `wav/Wake__hellohaier_norm.wav`：唤醒词；
- `wav/Turn-on-aircon__BH123_SE_F003_normal_061.wav`：交互命令词“打开空调”（Turn on aircon）；
- `wav/*.wav`：每个意图保留 1 个 normal 文件，全部平铺在 `wav/` 下。

本仓库**不内置** `lstest` 副本，运行时依赖外部 `lstest` 公共库，复用其 `TaskArtifacts`、`DeviceProfile`、`ScenarioRuntime`、`ToolLogValidator` 和四类产物标准。入口按以下顺序解析 `lstest`，找不到直接报错，不回退到任何内置副本：

1. 已可导入的 `lstest` 包（`pip install`）；
2. 环境变量 `LSTEST_HOME` 指向 lstest 仓库根目录；
3. 与本仓库同级的 `lstest` 目录（现场机器历史布局）。

## 云端导入包

为了让导入器通过 **1MB/单文件** 限制，云端仓库只保留脚本、配置、文档和 75 个 wav 音频：

- 不包含 `fw/` 下的 CSK/HISI 固件；
- 不包含 `burn/` 下的烧录工具和厂商工具包；
- 实际烧录前，优先从 OSS 资产包自动恢复；网络不可用时，再从本地或可信介质按 [手动资产清单](references/manual-assets.md) 补齐路径，并核对 SHA-256；本机端口配置 `config/devices.json` 不上传。

**本机原有固件、工具和测试日志不删除。**云端删除只是让受限客户端能导入；没有手动补资产的客户端不能直接烧录。导入时使用最新 `main`，不要沿用含大文件的旧快照。

## 环境准备

- Windows + Python 3.10+；
- 首次运行执行 `install_requirements.bat`，或 `python -m pip install -r requirements.txt`；
- `lstest` 公共运行时（必需，不随本仓库分发）：`python -m pip install "lstest @ git+https://github.com/zjashanda/lstest.git@<commit>"`，或设 `LSTEST_HOME`，或把 `lstest` 检出到与本仓库同级；
- 需要 FFmpeg 用于音频转换，播放需要 `pygame`，串口需要 `pyserial`；语音合成由 `lstest` 统一走 `lstts`，本仓库不再依赖 `edge-tts`；
- `config/devices.json` 是本地端口配置，不上传云端；云端提供脚本、音频与依赖声明，固件与工具须按 [手动资产清单](references/manual-assets.md) 补齐。

## 快速开始

```powershell
# 1. 无设备动作预检（profile fixture、固件哈希、端口、控制 shell）
python -u cskflashtest/scripts/run.py --preflight

# 2. 手动补齐资产后执行单轮 hex 烧录
python -u cskflashtest/scripts/run.py --rounds 1

# 3. 指定固件类型
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

### 手动验收脚本（按配置执行全部设备，终端实时输出）

```text
verify_burn.bat [轮数] [验收模式]
verify_powercycle.bat [轮数] [验收模式]
```

设备数量由 `config/devices.json` 决定；启用多少台就执行多少台，不需要填写设备 ID。

示例：

```powershell
verify_powercycle.bat 3 any_asr
verify_powercycle.bat 3 any_asr_playback
verify_burn.bat 1 strict
```

终端会按设备标签实时输出：轮次、上下电、CSK 启动、log level、唤醒、识别结果、播报检查和本轮判定。

每轮结束额外输出：

- `[STATS] ...`：当前设备的累计 `pass/fail` 和 `hit/misheard/empty/asleep`。
- `[TOTAL] ...`：当前所有设备的累计 `pass/fail` 和四态统计。

任务结束输出 `[OUTCOME]` 表格，并写入 `summary_outcomes.csv`（按设备统计轮数、成功、失败、四态、多结果和平均单轮耗时）。

## 长跑稳定性约束

- `SerialCollector` 使用 append-only `serial_logs` + tailing parser + 有界 `FactStore`（每端口 2 万条），不再保留全量串口事件。
- parser 单行缓冲上限 4MiB；超限丢弃当前缓冲并记录 `SERIAL_LINE_OVERSIZED`，防止无换行日志无限增长。
- `TimelineLedger`、`ScenarioRuntime`、`TaskArtifacts` 在每个 case 结束后释放 case facts、anomalies、window、metadata 和 position。
- 最终 `results.csv` 对账和 `tool.log` 校验使用流式读取，禁止 `read_text()` 或全量 row list。
- `StopSupervisor` 在磁盘低于阈值时请求 `DISK_STOP_LINE`；1000 小时运行仍需预留约 80–100GB 日志/结果空间（4 台估算）。
- 上述约束只覆盖代码层；USB、串口驱动、声卡、电源和设备固件属于外部风险。`test_start` 约 6 小时运行上限也属于外部启动工具限制。
- 长跑回归：`python -m unittest tests.test_serial_fact_store tests.test_long_run_soak -v`。

## 两套压测入口

### 1. 烧录 + 唤醒/ASR

默认模式，入口不变：

```powershell
python -u scripts/run_multi.py --mode burn --devices config/devices.json --rounds 20 --interaction-test --no-hisi
run_multi.bat --rounds 20 --interaction-test --no-hisi
```

结果前缀：`result/<timestamp>_cskflashtest_...`

### 2. 上下电 + CSK 初始化 + 唤醒/ASR

不调用烧录工具，也不占用烧录串口：

```powershell
python -u scripts/run_multi.py --mode powercycle --devices config/devices.json --rounds 20 --interaction-test --no-hisi
run_powercycle.bat --rounds 20 --interaction-test --no-hisi
preflight_powercycle.bat
```

单轮时序：

1. `uut-csk-boot.off`：确保退出 boot；
2. `uut-switch1.off`：断电；
3. 等待 `pre_burn_wait_s`；
4. `uut-switch1.on`：上电；
5. 等待 `AP Boot Ready !!!`；
6. 设置 `flash.setloglev 4`；
7. 唤醒 + ASR 交互（如启用）。

结果前缀：`result/<timestamp>_cskpowercycle_...`
多设备唤醒仍受 `audio_lock.slots=4` 限制，最多 4 台同时播报。

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

# hex
.\Uart_Burn_Tool.exe -b 1500000 -p COM15 -c -t 10 -f <fw.hex> -l -m -d 0x00 -s
```

`-t 10` 只表示建链超时，不是总烧录超时；总超时由 `timing.burn_timeout_s`（默认 600s）控制，超时会终止工具，随后仍执行退出 boot 和重新上电。

## HISI / WS63 烧录

HISI 与 CSK 使用独立工具、固件包和串口参数，完整流程见 [references/hisi-burn.md](references/hisi-burn.md)。

| 项目 | CSK | HISI / WS63 |
|---|---|---|
| 工具 | `burn/Uart_Burn_Tool.exe` | `burn/VenusA+WS63/VenusA+WS63/BurnTool_Gold.zip`（auto_burn.py 首次自动解压） |
| 固件 | `fw/.../CSK/fw.hex` | `fw/.../HISI/ws63-liteos-app_all.fwpkg` |
| 烧录协议波特率 | `1500000` | `1000000`（`-signalbaud`） |
| 应用日志/AT 波特率 | `115200` | `1500000` |
| 结果日志 | Uart_Burn_Tool 输出 + CSK 日志 | `BurnTool_Gold/optLog/optLog_*.txt` + HISI/CSK 日志 |

单台示例：

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

成功判定：`optLog` 出现 `烧写结果：成功`，且 `AT+FTM=0` 返回 `+FTM SWITCH: start ... OK`。`HISI/out/update.fwpkg` 是 OTA 包，不能用于 BurnTool。

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
- 每次唤醒失败最多尝试 4 次（首次 + 3 次重试），每轮把失败次数写入 `wake_failures` / `wake.failed_attempts`；
- 4 次均失败时本轮 `wake_result=FAIL`，但不会中断后续轮次；
- 多设备可加 `--wake-test`：`run_multi.bat --rounds 1 --wake-test`。

## 命令词交互校验

唤醒成功后播报命令词“打开空调”：

```text
wav/Turn-on-aircon__BH123_SE_F003_normal_061.wav
```

单轮交互结果统一为四种：

- `hit`：唤醒成功，识别到目标词 `Turn on aircon`；
- `misheard`：唤醒成功，有识别结果但不是目标词（例如 `Turn on ECO`）；
- `empty`：唤醒成功，但没有识别结果；
- `asleep`：未唤醒。

验收模式通过 `--acceptance` 选择（默认 `any_asr`）：

| 模式 | 唤醒 | 唤醒播报 | ASR 结果 | 识别响应/播报 | 多唤醒/多识别 |
|---|---|---|---|---|---|
| `wake_only` | 要求 | 不检查 | 不检查 | 不检查 | 记录 |
| `wake_playback` | 要求 | 至少 1 次完整播报 | 不检查 | 不检查 | 记录 |
| `strict` | 要求 | 不检查 | 只接受 `hit` | 不检查 | **FAIL** |
| `strict_playback` | 要求 | 至少 1 次完整播报 | 只接受 `hit` | 要求响应 + 至少 1 次完整播报 | **FAIL** |
| `any_asr` | 要求 | 不检查 | `hit` / `misheard` | 不检查 | 记录 |
| `any_asr_playback` | 要求 | 至少 1 次完整播报 | `hit` / `misheard` | 要求响应 + 至少 1 次完整播报 | 记录 |
| `asr_optional` | 要求 | 不检查 | `hit` / `misheard` / `empty`；整轮任务至少一次非空 | 不检查 | 记录 |
| `asr_optional_playback` | 要求 | 至少 1 次完整播报 | `hit` / `misheard` / `empty`；整轮任务至少一次非空 | 有识别结果的轮次要求响应 + 至少 1 次完整播报 | 记录 |

规则：

- 以上都是**单轮交互**判定；`asr_optional` 的“至少一次非空”是额外的任务级约束；
- 单轮内所有唤醒结果和识别结果都记录到 `wake_results` / `asr_results`，并统计次数；
- 一次播放出现多个唤醒结果或多个识别结果时，记录 `MULTI_WAKE` / `MULTI_ASR` / `MULTI_RESULT` 异常；
- `strict` / `strict_playback` 遇到多结果直接 FAIL；其他模式只记录，不影响本轮通过；
- 唤醒播报默认按 1 次检查；
- 识别播报允许 1 次或多次，每次要求 PLAYING → COMPLETE 或 start → stop；
- 唤醒失败或无识别结果才重试，最多 4 次（首次 + 3 次重试）；
- 误识别不重试；
- 不再校验音量变化，`vir_kid` 仅记录；
- 上下电测试默认使用 `any_asr`。

命令：

```powershell
python -u cskflashtest/scripts/run.py --devices cskflashtest/config/devices.json --device-id dev01 --rounds 1 --skip-burn --acceptance any_asr --no-hisi
```

`--acceptance strict/any_asr/asr_optional/strict_playback/any_asr_playback/asr_optional_playback` 会自动启用唤醒和 ASR；`--acceptance wake_only/wake_playback` 只验证唤醒。多设备时每台设备仍需配置自己的 `render_device_key`。

## 压测前初始化清理

`run_multi.py` 启动前自动调用 `lstest` 的 `environment_cleanup`：

- 清空 `logs/audio_slots`；
- 杀掉残留的 `listenai_play.py` / `internal-play-once` 进程；
- 删除过期的临时音频；
- 扫描 Render 端点，记录未使用/缺失的声卡 Key。

## 串口断开重连

控制口无提示符时，`run.py` 调用 `lstest` 的 `serial_recovery`：

- 最多 20 次重开；
- 等待从 0.5s 起逐步延长到 30s；
- 每次用 3s 探测 `root:/$`；
- 全部失败才判 `control_alive` FAIL，连续 3 轮失败停止任务。

## 结果目录命名

`<批次时间戳>_<场景>_<设备ID>_<任务简介>_<总次数>rounds`

同一批设备共用批次时间戳；同一设备同一任务多次运行追加到同一目录。

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
# 查看当前 hex 的 SHA-256、写入大小和映射镜像 MD5
python cskflashtest/scripts/commission.py --key hex

# 固件更新后登记新哈希；hex 需要提供一次已确认正常烧录的工具 MD5
python cskflashtest/scripts/commission.py --key hex --update --tool-md5 0x...
```

固件 SHA-256 与清单不一致时预检会阻塞，不允许带未知固件进入正式压测。

## 非目标

- CSK 主流程不包含 HISI 烧录；HISI/WS63 使用独立工具，步骤见 [references/hisi-burn.md](references/hisi-burn.md)；
- 不把烧录工具输出写进 `tool.log`；
- 不修改 `lstest` 仓库（外部依赖，只读引用）；
- 不在预检失败时猜测日志含义或继续上电烧录。
