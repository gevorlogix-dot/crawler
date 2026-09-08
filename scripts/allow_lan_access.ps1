<#
.SYNOPSIS
    Lets the office LAN reach the site auditor's web UI (server.py).

.DESCRIPTION
    The Flask server already binds every interface, so when colleagues get a
    connection timeout the cause is Windows Firewall, not the app. Clicking
    "Cancel" on the first firewall prompt leaves two explicit *Block* rules for
    python.exe on the Public profile, and a Block rule beats every Allow rule --
    including any port 5000 allow rule already present.

    This script:
      1. removes those inbound Block rules for python.exe,
      2. adds one narrow Allow rule: TCP, the chosen port, local subnet only.

    It does NOT change your network category. Public vs Private no longer
    matters once the Block rules are gone and the Allow rule covers all
    profiles -- and leaving the Wi-Fi as Public keeps the rest of the machine
    (file sharing, discovery) closed.

.PARAMETER Port
    Port the auditor listens on. Default 5000.

.PARAMETER Subnet
    CIDR range allowed to connect. Defaults to this machine's own /24, so only
    the office network is admitted. Pass "Any" to allow anything that can route
    here (not recommended).

.PARAMETER Undo
    Remove the Allow rule this script created, closing the port again.

.EXAMPLE
    # Right-click -> Run with PowerShell (as Administrator), or:
    powershell -ExecutionPolicy Bypass -File scripts\allow_lan_access.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\allow_lan_access.ps1 -Undo
#>
[CmdletBinding()]
param(
    [int]$Port = 5000,
    [string]$Subnet,
    [switch]$Undo
)

$ErrorActionPreference = 'Stop'
$RuleName = "Site auditor (LAN) $Port"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host ""
        Write-Host "  This script changes Windows Firewall, so it needs Administrator." -ForegroundColor Yellow
        Write-Host "  Right-click PowerShell -> Run as administrator, then re-run:" -ForegroundColor Yellow
        Write-Host "    powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -ForegroundColor Cyan
        Write-Host ""
        exit 1
    }
}

# The routable address, asked of the OS routing table. Enumerating interfaces
# instead would return loopback and 169.254.x link-local addresses nobody uses.
function Get-LanIPv4 {
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
             Sort-Object RouteMetric | Select-Object -First 1
    if (-not $route) { return $null }
    $addr = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
            Select-Object -First 1
    return $addr
}

Assert-Admin

if ($Undo) {
    $existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Remove-NetFirewallRule
        Write-Host "  Removed '$RuleName'. Port $Port is closed to the LAN again." -ForegroundColor Green
    } else {
        Write-Host "  No rule named '$RuleName' -- nothing to undo." -ForegroundColor Yellow
    }
    Write-Host "  Note: the python.exe Block rules were not restored. Windows will" -ForegroundColor DarkGray
    Write-Host "  re-prompt next time python opens a listening socket." -ForegroundColor DarkGray
    exit 0
}

# --- 1. the Block rules that actually cause the timeout -----------------------

$blocks = Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True -ErrorAction SilentlyContinue |
          Where-Object {
              $app = $_ | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
              $app -and $app.Program -and $app.Program -match 'python(w)?\.exe$'
          }

if ($blocks) {
    Write-Host ""
    Write-Host "  Inbound Block rules for python.exe:" -ForegroundColor Yellow
    foreach ($rule in $blocks) {
        $app = $rule | Get-NetFirewallApplicationFilter
        $portf = $rule | Get-NetFirewallPortFilter
        Write-Host ("    {0,-12} {1,-4} profile={2}" -f $rule.DisplayName, $portf.Protocol, $rule.Profile)
        Write-Host ("                      {0}" -f $app.Program) -ForegroundColor DarkGray
    }
    $blocks | Remove-NetFirewallRule
    Write-Host "  Removed $($blocks.Count) Block rule(s)." -ForegroundColor Green
} else {
    Write-Host "  No inbound Block rules for python.exe. Good." -ForegroundColor Green
}

# --- 2. one narrow Allow rule ------------------------------------------------

if (-not $Subnet) {
    $addr = Get-LanIPv4
    if ($addr) {
        $octets = $addr.IPAddress.Split('.')
        $Subnet = "$($octets[0]).$($octets[1]).$($octets[2]).0/24"
    } else {
        $Subnet = 'LocalSubnet'
    }
}

Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule

New-NetFirewallRule -DisplayName $RuleName `
    -Description 'Inbound access to the site auditor web UI (server.py) from the office network.' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -RemoteAddress $Subnet -Profile Any -Enabled True | Out-Null

Write-Host "  Added '$RuleName' -- TCP $Port, from $Subnet only." -ForegroundColor Green

# --- 3. what to hand colleagues ----------------------------------------------

$addr = Get-LanIPv4
Write-Host ""
if ($addr) {
    Write-Host "  Colleagues on $Subnet can now open:" -ForegroundColor Cyan
    Write-Host "      http://$($addr.IPAddress):$Port" -ForegroundColor White
} else {
    Write-Host "  Could not determine this machine's LAN address; run 'ipconfig'." -ForegroundColor Yellow
}

$listening = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listening) {
    $anyIface = $listening | Where-Object { $_.LocalAddress -in @('0.0.0.0', '::') }
    if ($anyIface) {
        Write-Host "  server.py is listening on all interfaces. Nothing else to do." -ForegroundColor Green
    } else {
        Write-Host "  server.py is bound to $($listening[0].LocalAddress) only -- loopback." -ForegroundColor Yellow
        Write-Host "  Restart it as:  python server.py --port $Port" -ForegroundColor Cyan
    }
} else {
    Write-Host "  Nothing is listening on port $Port yet. Start it with:" -ForegroundColor Yellow
    Write-Host "      python server.py --port $Port" -ForegroundColor Cyan
}
Write-Host ""
