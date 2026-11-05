# 手动添加烧录资产

云端导入包只包含脚本、配置、文档和 WAV 音频。以下烧录资产**不上传云端**；实际烧录前，从本地已验证的工作目录或可信介质复制到导入后的 Skill 根目录。不要把它们加入导入包。`config/devices.json` 也是本机端口配置，不上传。

如果目标电脑可以访问公网，Skill 会在烧录预检或 HISI 命令行入口发现资产缺失时自动下载并校验：

```text
https://listenai-firmware-delivery.oss-cn-beijing.aliyuncs.com/bszheng/csk6hisiflashtools.zip
```

资产包大小为 `32689775` 字节，SHA-256 为：

```text
128ae7fdb513d7aa5df65a36560c58944f098e88e953096cf9c9d13741ecb076
```

已有本地资产优先使用，不会被下载包覆盖；已有文件但大小或 SHA-256 不匹配时会阻塞并要求人工处理。无网络时可以按本页表格手动复制。

**不要从本地删除资产：**本地 `D:\workstation\quattro\102200cskflashst` 保留完整工具、固件及 `result/`、`multi_logs/`、`logs/`。本页只规定其他客户端导入云端仓库后，需要从本地手工补哪些文件。

当前本地资产来源：`D:\workstation\quattro\102200cskflashst`。下表中的路径均相对于 Skill 根目录；复制后核对字节数与 SHA-256，禁止只根据文件名判定正确。

| 用途 | 手动添加路径 | 字节数 | SHA-256 |
|---|---|---:|---|
| CSK 烧录工具 | `burn/Uart_Burn_Tool.exe` | 83968 | `4bfdf6800b32dc9fa7ed5e17f039dc1ea4a7f049c25acfc98f83e3dcfcf197bb` |
| CSK hex 固件（正式清单引用） | `fw/V46.1.8_20261102093954/CSK/fw.hex` | 15969403 | `06630deee41b7653a68b65740c0c95c3a120f3636f27f1af238feb747ba7bdbb` |
| HISI 烧录工具压缩包 | `burn/VenusA+WS63/VenusA+WS63/BurnTool_Gold.zip` | 15571883 | `f771e75ca6d75bbf775c52ce75dd89ffcd2fe85aea497098a1672fffd88ace01` |
| HISI 固件 | `fw/V46.1.8_20261102093954/HISI/ws63-liteos-app_all.fwpkg` | 3658512 | `2968071cff99b51c50b58e0eba2ab561f901d6668e6346a53c2adc81f6b9dd88` |

HISI 命令行入口 `burn/VenusA+WS63/VenusA+WS63/auto_burn.py` 是小文件，随云端 Skill 一起导入。它会在首次执行时解压上表的 `BurnTool_Gold.zip`。若需要使用厂商脚本的**CSK + HISI 联合烧录**，还需手动添加 `burn/VenusA+WS63/VenusA+WS63/VenusA_Burn/Uart_Burn_Tool.exe`（10882782 字节；SHA-256 `40a467816e2734f07f04fe9c76611f8c77becdf9dabb420e46ea59494752f507`）。仅 HISI 烧录加 `--skip-venusa` 不需要它。

本地还保留 `fw/V46.1.8_20261102093954/CSK/fw.img`、`burn/fw.img`、`burn/fw.hex` 等副本；它们**不属于云端导入清单**，当前正式 CSK 清单只使用上表的 `fw.hex`。不要把这些副本导入有 1 MiB 限制的客户端。

手动补齐或自动下载后执行 `python -u scripts/run.py --preflight --devices config/devices.json --device-id dev01`：它会检查所选固件的 SHA-256 与烧录工具是否存在。也可以先只检查缺失项：

```powershell
python -u scripts/ensure_assets.py --dry-run
```

预检通过不等于整机稳定性通过；HISI 工具、串口参数及烧录后校验见 [hisi-burn.md](hisi-burn.md)。未补资产且无法访问资产 URL 的导入包只能用于阅读、音频和不依赖烧录资产的工具，不能直接烧录。
