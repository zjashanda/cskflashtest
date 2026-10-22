[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateSet("CurrentUserAllHosts", "CurrentUserCurrentHost")]
    [string]$Scope = "CurrentUserAllHosts"
)

$ErrorActionPreference = "Stop"

$profilePath = $PROFILE.$Scope
if (-not $profilePath) {
    throw "Unable to resolve profile path for scope: $Scope"
}

$profileDir = Split-Path -Parent $profilePath
$beginMarker = "# >>> laid >>>"
$endMarker = "# <<< laid <<<"

$laidBlock = @'
# >>> laid >>>
function Get-ListenAIWaveFormatChannels {
    [CmdletBinding()]
    param(
        [byte[]]$Blob
    )

    if (-not $Blob -or $Blob.Length -lt 12) {
        return $null
    }

    $offset = 0
    if ($Blob.Length -ge 8 -and [BitConverter]::ToUInt32($Blob, 0) -eq 65) {
        $offset = 8
    }

    if ($Blob.Length -lt ($offset + 4)) {
        return $null
    }

    return [int][BitConverter]::ToUInt16($Blob, $offset + 2)
}

function Get-ListenAIDeviceKeyFromInterface {
    [CmdletBinding()]
    param(
        [string]$Interface
    )

    if ([string]::IsNullOrWhiteSpace($Interface)) {
        return $null
    }

    if ($Interface -notmatch '^(?:\{\d+\}\.)?USB\\(?<Head>[^\\]+)\\(?<Tail>.+)$') {
        return $null
    }

    $head = $matches['Head']
    $tail = $matches['Tail']
    $vidPid = ($head -replace '&MI_[0-9A-F]{2}$', '').ToUpperInvariant()
    if ($vidPid -notmatch '^VID_[0-9A-F]{4}&PID_[0-9A-F]{4}$') {
        return $null
    }

    $token = ($tail -replace '[^A-Za-z0-9]+', '_').Trim('_').ToUpperInvariant()
    if ($token -match '^([A-Z0-9]{4,})_0_([A-Z0-9]{2,})$') {
        $token = "$($matches[1])_$($matches[2])"
    }
    if (-not $token) {
        return $null
    }

    return ("{0}:{1}" -f $vidPid, $token)
}

function Get-ListenAIDeviceKeys {
    [CmdletBinding()]
    param(
        [ValidateSet('All', 'Render', 'Capture')]
        [string]$Direction = 'All'
    )

    $endpointMap = @{}
    Get-PnpDevice -Class AudioEndpoint -PresentOnly | ForEach-Object {
        if ($_.InstanceId -match 'SWD\\MMDEVAPI\\\{[^}]+\}\.\{([0-9A-Fa-f-]+)\}$') {
            $endpointMap["{$($matches[1].ToLower())}"] = $_.FriendlyName
        }
    }

    $rows = foreach ($root in @(
        @{ Direction = 'Render'; Path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render' },
        @{ Direction = 'Capture'; Path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture' }
    )) {
        if ($Direction -ne 'All' -and $Direction -ne $root.Direction) {
            continue
        }

        Get-ChildItem -LiteralPath $root.Path | ForEach-Object {
            $propsPath = Join-Path $_.PSPath 'Properties'
            if (-not (Test-Path -LiteralPath $propsPath)) { return }

            $state = (Get-ItemProperty -LiteralPath $_.PSPath -Name DeviceState -ErrorAction SilentlyContinue).DeviceState
            if (($state -band 0xF) -ne 1) { return }

            $props = Get-ItemProperty -LiteralPath $propsPath -ErrorAction SilentlyContinue
            if ($props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6' -ne 'ListenAI Audio') { return }
            $channels = Get-ListenAIWaveFormatChannels $props.'{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0'

            $interface = $props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},2'
            $deviceKey = Get-ListenAIDeviceKeyFromInterface $interface
            if (-not $deviceKey) { return }

            [pscustomobject]@{
                Direction    = $root.Direction
                DeviceKey    = $deviceKey
                Channels     = $(if ($null -eq $channels) { '?' } else { $channels })
                FriendlyName = $endpointMap[$_.PSChildName.ToLower()]
                EndpointId   = $_.PSChildName
            }
        }
    }

    $rows | Sort-Object Direction, DeviceKey
}

function Get-AudioEndpointStateLabel {
    [CmdletBinding()]
    param(
        [int]$State
    )

    switch ($State -band 0xF) {
        1 { return 'Active' }
        2 { return 'Disabled' }
        4 { return 'NotPresent' }
        8 { return 'Unplugged' }
        default { return 'Unknown' }
    }
}

function Get-AudioEndpointList {
    [CmdletBinding()]
    param(
        [ValidateSet('All', 'Render', 'Capture')]
        [string]$Direction = 'All',
        [switch]$IncludeDisabled
    )

    $endpointMap = @{}
    Get-PnpDevice -Class AudioEndpoint -PresentOnly | ForEach-Object {
        if ($_.InstanceId -match 'SWD\\MMDEVAPI\\\{[^}]+\}\.\{([0-9A-Fa-f-]+)\}$') {
            $endpointMap["{$($matches[1].ToLower())}"] = $_.FriendlyName
        }
    }

    $rows = foreach ($root in @(
        @{ Direction = 'Render'; Path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render' },
        @{ Direction = 'Capture'; Path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture' }
    )) {
        if ($Direction -ne 'All' -and $Direction -ne $root.Direction) {
            continue
        }

        Get-ChildItem -LiteralPath $root.Path | ForEach-Object {
            $propsPath = Join-Path $_.PSPath 'Properties'
            if (-not (Test-Path -LiteralPath $propsPath)) { return }

            $state = (Get-ItemProperty -LiteralPath $_.PSPath -Name DeviceState -ErrorAction SilentlyContinue).DeviceState
            if (-not $IncludeDisabled -and (($state -band 0xF) -ne 1)) { return }

            $props = Get-ItemProperty -LiteralPath $propsPath -ErrorAction SilentlyContinue
            $interface = $props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},2'
            $deviceKey = Get-ListenAIDeviceKeyFromInterface $interface
            if (-not $deviceKey) {
                $deviceKey = if ([string]::IsNullOrWhiteSpace($interface)) {
                    'N/A'
                } else {
                    $interface -replace '^\{\d+\}\.', ''
                }
            }

            $channels = Get-ListenAIWaveFormatChannels $props.'{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0'
            $friendlyName = $endpointMap[$_.PSChildName.ToLower()]
            if ([string]::IsNullOrWhiteSpace($friendlyName)) {
                $friendlyName = $props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6'
            }

            [pscustomobject]@{
                Direction    = $root.Direction
                State        = Get-AudioEndpointStateLabel $state
                DeviceKey    = $deviceKey
                Channels     = $(if ($null -eq $channels) { '?' } else { $channels })
                FriendlyName = $friendlyName
                EndpointId   = $_.PSChildName
            }
        }
    }

    $rows | Sort-Object Direction, FriendlyName, DeviceKey
}

function audio-list {
    <#
    .SYNOPSIS
    列出 Windows 当前的通用音频采集和播放端点。

    .DESCRIPTION
    默认只列出活动端点。使用 -IncludeDisabled 可同时查看禁用、未连接和不存在的端点。

    .EXAMPLE
    audio-list

    .EXAMPLE
    audio-list -Direction Capture -Json
    #>
    [CmdletBinding()]
    param(
        [ValidateSet('All', 'Render', 'Capture')]
        [string]$Direction = 'All',
        [switch]$IncludeDisabled,
        [switch]$Json
    )

    $rows = @(Get-AudioEndpointList -Direction $Direction -IncludeDisabled:$IncludeDisabled)
    if ($Json) {
        $rows | ConvertTo-Json -Depth 4
        return
    }

    if (-not $rows) {
        Write-Output 'No matching audio endpoints found.'
        return
    }

    $rows | Format-Table -AutoSize
}

function laid {
    [CmdletBinding()]
    param(
        [ValidateSet('All', 'Render', 'Capture')]
        [string]$Direction = 'All',
        [switch]$Json
    )

    $rows = @(Get-ListenAIDeviceKeys -Direction $Direction)
    if ($Json) {
        $rows | ConvertTo-Json -Depth 4
        return
    }

    if (-not $rows) {
        Write-Output 'No active ListenAI endpoints found.'
        return
    }

    $rows | Format-Table -AutoSize
}
# <<< laid <<<
'@

if (-not (Test-Path -LiteralPath $profileDir)) {
    if ($PSCmdlet.ShouldProcess($profileDir, "Create profile directory")) {
        New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
    }
}

$existing = ""
if (Test-Path -LiteralPath $profilePath) {
    $existing = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8
}

$pattern = "(?ms)^" + [System.Text.RegularExpressions.Regex]::Escape($beginMarker) + "\r?\n.*?^" + [System.Text.RegularExpressions.Regex]::Escape($endMarker) + "\r?\n?"
$cleaned = [System.Text.RegularExpressions.Regex]::Replace($existing, $pattern, "")
$cleaned = $cleaned.TrimEnd()

$newContent = if ($cleaned) {
    $cleaned + "`r`n`r`n" + $laidBlock.Trim() + "`r`n"
} else {
    $laidBlock.Trim() + "`r`n"
}

if ($PSCmdlet.ShouldProcess($profilePath, "Install laid helper")) {
    Set-Content -LiteralPath $profilePath -Value $newContent -Encoding UTF8
}

Write-Output "laid installed to: $profilePath"
Write-Output "Open a new PowerShell session, or run: . `"$profilePath`""
