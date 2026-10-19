<#
Sissy Censor System: system-level installer for Windows.

Installs Git and Docker Desktop with winget, clones the repository, builds
and starts the proxy, trusts its CA in the machine's Trusted Root store
(Edge, Chrome and Firefox via policy) and sets the Windows proxy to
127.0.0.1:8080.

Run from an elevated PowerShell (Run as administrator):

  powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/OWNER/scs/main/install/windows.ps1 | iex"

Undo the trust and proxy changes (keeps Docker Desktop and the checkout):

  powershell -ExecutionPolicy Bypass -c "$env:SCS_UNINSTALL='1'; irm https://raw.githubusercontent.com/OWNER/scs/main/install/windows.ps1 | iex"

Environment overrides: SCS_REPO_URL, SCS_DIR, SCS_BRANCH.
#>
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- settings
# TODO: replace the placeholder with the real repository URL.
$ScsRepoUrl = if ($env:SCS_REPO_URL) { $env:SCS_REPO_URL } else { 'https://github.com/OWNER/scs.git' }
$ScsDir     = if ($env:SCS_DIR) { $env:SCS_DIR } else { Join-Path $env:USERPROFILE 'scs' }
$ScsBranch  = if ($env:SCS_BRANCH) { $env:SCS_BRANCH } else { 'main' }
$Uninstall  = $env:SCS_UNINSTALL -eq '1'

$ProxyHost = '127.0.0.1'
$ProxyPort = 8080
$CaPem = Join-Path $ScsDir 'src\mitmproxy\mitmproxy-ca-cert.pem'
$DockerDesktopExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$InternetSettings = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$FirefoxPolicy = 'HKLM:\SOFTWARE\Policies\Mozilla\Firefox\Certificates'

# ----------------------------------------------------------------- helpers
function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Magenta }
function Warn($msg) { Write-Host "warning: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Die 'run this from an elevated PowerShell (Run as administrator)'
}

function Winget-Install($id) {
    if (-not (Have winget)) { Die 'winget is missing; install "App Installer" from the Microsoft Store and rerun' }
    winget install -e --id $id --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {   # -1978335189: already installed
        Die "winget could not install $id (exit $LASTEXITCODE)"
    }
    Refresh-Path
}

# Tell WinINET clients (Edge, Chrome, Firefox) that the proxy settings changed.
function Refresh-ProxySettings {
    if (-not ('Scs.WinInet' -as [type])) {
        Add-Type -Namespace Scs -Name WinInet -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("wininet.dll", SetLastError = true)]
public static extern bool InternetSetOption(System.IntPtr hInternet, int dwOption, System.IntPtr lpBuffer, int dwBufferLength);
'@
    }
    [void][Scs.WinInet]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)   # INTERNET_OPTION_SETTINGS_CHANGED
    [void][Scs.WinInet]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)   # INTERNET_OPTION_REFRESH
}

# ---------------------------------------------------------- docker desktop
function Install-DockerDesktop {
    if (Test-Path $DockerDesktopExe) { Log 'Docker Desktop is already installed'; return }
    Log 'Checking the Windows Subsystem for Linux'
    wsl --status *> $null
    if ($LASTEXITCODE -ne 0) {
        Log 'Enabling WSL 2 (Docker Desktop runs on it)'
        wsl --install --no-distribution
        Write-Host '    WSL was just enabled. Reboot, then run this installer again.'
        exit 0
    }
    Log 'Installing Docker Desktop'
    Winget-Install 'Docker.DockerDesktop'
    try { Add-LocalGroupMember -Group 'docker-users' -Member $env:USERNAME -ErrorAction Stop } catch {}
}

function Start-DockerDesktop {
    Log 'Starting Docker Desktop'
    if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        Start-Process $DockerDesktopExe
    }
    Refresh-Path
    $waited = 0
    while ($true) {
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
        if ($waited -eq 0) {
            Write-Host '    Waiting for Docker Desktop. On first start, accept the service agreement'
            Write-Host '    in its window and leave it running.'
        }
        if ($waited -ge 900) { Die 'Docker Desktop did not become ready within 15 minutes' }
        Start-Sleep 5; $waited += 5
    }
}

# ---------------------------------------------------------------- the repo
function Get-Repo {
    if (-not (Have git)) { Log 'Installing Git'; Winget-Install 'Git.Git' }
    if (Test-Path (Join-Path $ScsDir '.git')) {
        Log "Updating $ScsDir"
        git -C $ScsDir pull --ff-only
        if ($LASTEXITCODE -ne 0) { Warn "could not fast-forward $ScsDir; continuing with what is there" }
    } else {
        if ($ScsRepoUrl -like '*OWNER/*') { Die 'SCS_REPO_URL is still the placeholder; set it to the real repository URL' }
        Log "Cloning $ScsRepoUrl into $ScsDir"
        git clone --branch $ScsBranch $ScsRepoUrl $ScsDir
        if ($LASTEXITCODE -ne 0) { Die 'git clone failed' }
    }
}

function Start-Proxy {
    Log 'Building and starting the proxy (the first build downloads a few hundred MB)'
    Push-Location (Join-Path $ScsDir 'src')
    try {
        docker compose up -d --build
        if ($LASTEXITCODE -ne 0) { Die 'docker compose up failed' }
    } finally { Pop-Location }
    $waited = 0
    while (-not ((Test-Path $CaPem) -and (Get-Item $CaPem).Length -gt 0)) {
        if ($waited -ge 120) { Die "the proxy did not write its CA to $CaPem" }
        Start-Sleep 2; $waited += 2
    }
    # The container takes a few seconds to load the models after (re)creation.
    $waited = 0
    while ($true) {
        try {
            [void](Invoke-WebRequest -Uri 'http://censor.local/health' -Proxy "http://$ProxyHost`:$ProxyPort" -UseBasicParsing -TimeoutSec 15)
            break
        } catch {
            if ($waited -ge 60) { Warn "health check through the proxy failed; check 'docker compose logs' in $ScsDir\src"; break }
            Start-Sleep 3; $waited += 3
        }
    }
}

# ------------------------------------------------------------------- trust
function Get-CaCert { New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CaPem) }

function Trust-Ca {
    Log 'Trusting the proxy CA in the machine Trusted Root store (Edge, Chrome, Windows apps)'
    certutil -addstore -f Root $CaPem | Out-Null
    if ($LASTEXITCODE -ne 0) { Die 'certutil could not add the CA' }
    Log 'Telling Firefox to use the Windows trust store (policy)'
    New-Item -Path $FirefoxPolicy -Force | Out-Null
    Set-ItemProperty -Path $FirefoxPolicy -Name 'ImportEnterpriseRoots' -Value 1 -Type DWord
}

function Untrust-Ca {
    Log 'Removing the proxy CA from the Trusted Root store'
    if (Test-Path $CaPem) {
        $thumb = (Get-CaCert).Thumbprint
        Get-ChildItem Cert:\LocalMachine\Root | Where-Object Thumbprint -eq $thumb | Remove-Item
    } else {
        Get-ChildItem Cert:\LocalMachine\Root | Where-Object Subject -eq 'CN=mitmproxy, O=mitmproxy' | Remove-Item
    }
    Remove-ItemProperty -Path $FirefoxPolicy -Name 'ImportEnterpriseRoots' -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------- proxy
function Set-SystemProxy {
    Log "Setting the Windows proxy to $ProxyHost`:$ProxyPort (fails closed while the proxy is stopped)"
    Set-ItemProperty -Path $InternetSettings -Name ProxyServer -Value "$ProxyHost`:$ProxyPort"
    Set-ItemProperty -Path $InternetSettings -Name ProxyOverride -Value 'localhost;127.*;[::1];<local>'
    Set-ItemProperty -Path $InternetSettings -Name ProxyEnable -Value 1 -Type DWord
    Remove-ItemProperty -Path $InternetSettings -Name AutoConfigURL -ErrorAction SilentlyContinue
    Refresh-ProxySettings
}

function Clear-SystemProxy {
    Log 'Turning the Windows proxy off'
    Set-ItemProperty -Path $InternetSettings -Name ProxyEnable -Value 0 -Type DWord
    Refresh-ProxySettings
}

# -------------------------------------------------------------------- main
if ($Uninstall) {
    Clear-SystemProxy
    Untrust-Ca
    Log "Done. Docker Desktop and $ScsDir were left in place; stop the proxy with"
    Write-Host "    cd $ScsDir\src; docker compose down"
    exit 0
}

Install-DockerDesktop
Start-DockerDesktop
Get-Repo
Start-Proxy
Trust-Ca
Set-SystemProxy

Log 'Installed'
Write-Host @"
    Proxy:        $ProxyHost`:$ProxyPort (Docker, starts with Windows)
    Admin panel:  http://localhost:8082/admin
    Checkout:     $ScsDir
    Restart any open browser so it picks up the CA and the proxy.
    Docker Desktop follows the Windows proxy by default: in Settings > Resources
    > Proxies choose manual with empty fields so image pulls do not go through
    the censor.
"@
