# VenusA+WS63 vendor tool package

This directory is a reference copy of the vendor HISI/WS63 burn toolchain.

```text
VenusA+WS63/
  VenusA+WS63/
    auto_burn.py
    run.bat
    BurnTool_Gold.zip
      (auto-extracted on first run)
    VenusA_Burn/
      Uart_Burn_Tool.exe
```

`VenusA_Burn/Uart_Burn_Tool.exe` is the CSK/VenusA tool and is not used for a
HISI-only run. HISI-only runs use `BurnTool_Gold.zip` through
`auto_burn.py --skip-venusa`; the archive is extracted to `BurnTool_Gold/`
automatically on first run.

The repository copy is pruned: `BurnTool_Gold/configure/config_mcu` (MCU/JTAG
configuration for other chips) is removed. WS63/CSK essentials are retained,
including `config_WS63`, chip-type INI files, the Qt DLLs and the vendor
`Uart_Burn_Tool.exe`. The pruned copy was re-tested on dev01: BurnTool returned
`烧写结果：成功` and `AT+FTM=0` returned `OK`.

Runtime output is intentionally excluded from git:

```text
BurnTool_Gold/optLog/
BurnTool_Gold/tmp/
BurnTool_Gold/log.txt
```

Use the firmware package and exact command in
[`references/hisi-burn.md`](../../references/hisi-burn.md).
