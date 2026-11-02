# VenusA+WS63 vendor tool package

This directory is a reference copy of the vendor HISI/WS63 burn toolchain.

```text
VenusA+WS63/
  VenusA+WS63/
    auto_burn.py
    run.bat
    BurnTool_Gold/
      BurnTool.exe
      *.dll
      configure/
      platforms/
    VenusA_Burn/
      Uart_Burn_Tool.exe
```

`VenusA_Burn/Uart_Burn_Tool.exe` is the CSK/VenusA tool and is not used for a
HISI-only run. HISI-only runs use `BurnTool_Gold/BurnTool.exe` through
`auto_burn.py --skip-venusa`.

Runtime output is intentionally excluded from git:

```text
BurnTool_Gold/optLog/
BurnTool_Gold/tmp/
BurnTool_Gold/log.txt
```

Use the firmware package and exact command in
[`references/hisi-burn.md`](../../references/hisi-burn.md).
