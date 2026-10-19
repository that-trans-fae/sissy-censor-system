<#
Sissy Censor System: dedicated browser installer for Windows.

Installs Git, Docker Desktop and Chromium with winget, clones the
repository, builds and starts the proxy, and adds a "Sissy Browser"
shortcut to the Start menu and desktop: a separate Chromium profile that
always goes through the proxy and trusts only its CA. Nothing system-wide
changes: no Windows proxy, no CA in the Windows store, and your usual
browsers are untouched. Linux and macOS: install/browser.sh.

Run from a normal PowerShell (winget asks for elevation when it needs it):

  powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/OWNER/scs/main/install/browser.ps1 | iex"

Remove the shortcut and its profile (keeps Docker Desktop, Chromium and the checkout):

  powershell -ExecutionPolicy Bypass -c "$env:SCS_UNINSTALL='1'; irm https://raw.githubusercontent.com/OWNER/scs/main/install/browser.ps1 | iex"

Environment overrides: SCS_REPO_URL, SCS_DIR, SCS_BRANCH, SCS_CHROMIUM_EXE.
#>
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- settings
# TODO: replace the placeholder with the real repository URL.
$ScsRepoUrl = if ($env:SCS_REPO_URL) { $env:SCS_REPO_URL } else { 'https://github.com/OWNER/scs.git' }
$ScsDir     = if ($env:SCS_DIR) { $env:SCS_DIR } else { Join-Path $env:USERPROFILE 'scs' }
$ScsBranch  = if ($env:SCS_BRANCH) { $env:SCS_BRANCH } else { 'main' }
$Uninstall  = $env:SCS_UNINSTALL -eq '1'

$BrowserName = 'Sissy Browser'
$ProxyHost = '127.0.0.1'
$ProxyPort = 8080
$CaPem = Join-Path $ScsDir 'src\mitmproxy\mitmproxy-ca-cert.pem'
$DockerDesktopExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$ChromiumWingetId = 'Hibbiki.Chromium'
$BrowserHome = Join-Path $env:LOCALAPPDATA 'SissyBrowser'
$ProfileDir = Join-Path $BrowserHome 'profile'
$IconPath = Join-Path $BrowserHome 'sissy-browser.ico'
$Shortcuts = @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$BrowserName.lnk"),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) "$BrowserName.lnk")
)

# ----------------------------------------------------------------- helpers
function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Magenta }
function Warn($msg) { Write-Host "warning: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Winget-Install($id) {
    if (-not (Have winget)) { Die 'winget is missing; install "App Installer" from the Microsoft Store and rerun' }
    winget install -e --id $id --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {   # -1978335189: already installed
        Die "winget could not install $id (exit $LASTEXITCODE)"
    }
    Refresh-Path
}

# ---------------------------------------------------------- docker desktop
function Install-DockerDesktop {
    if (Test-Path $DockerDesktopExe) { Log 'Docker Desktop is already installed'; return }
    Log 'Checking the Windows Subsystem for Linux'
    wsl --status *> $null
    if ($LASTEXITCODE -ne 0) {
        Die 'WSL 2 is not enabled. From an elevated PowerShell run "wsl --install --no-distribution", reboot, then rerun this installer'
    }
    Log 'Installing Docker Desktop'
    Winget-Install 'Docker.DockerDesktop'
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
            Write-Host '    in its window and leave it running. If it says you are not in the'
            Write-Host '    docker-users group, sign out and back in, then rerun this installer.'
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
    try {
        [void](Invoke-WebRequest -Uri 'http://censor.local/health' -Proxy "http://$ProxyHost`:$ProxyPort" -UseBasicParsing -TimeoutSec 15)
    } catch { Warn "health check through the proxy failed; check 'docker compose logs' in $ScsDir\src" }
}

# ---------------------------------------------------------------- chromium
function Find-Chromium {
    $candidates = @(
        $env:SCS_CHROMIUM_EXE,
        (Join-Path $env:LOCALAPPDATA 'Chromium\Application\chrome.exe'),
        (Join-Path $env:ProgramFiles 'Chromium\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Chromium\Application\chrome.exe')
    ) | Where-Object { $_ }
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

function Install-Chromium {
    $exe = Find-Chromium
    if (-not $exe) {
        Log 'Installing Chromium'
        Winget-Install $ChromiumWingetId
        $exe = Find-Chromium
    }
    if (-not $exe) { Die 'could not find chrome.exe after installing Chromium; set SCS_CHROMIUM_EXE to its path' }
    return $exe
}

# Base64 SHA-256 of the CA's SubjectPublicKeyInfo. mitmproxy signs each site
# certificate with the CA's own key pair, so this one hash covers every site
# the proxy intercepts, and Chromium accepts it with --ignore-certificate-errors-spki-list.
function Get-CaSpkiHash {
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CaPem)
    $key = $cert.PublicKey
    if ($key.PSObject.Methods.Name -contains 'ExportSubjectPublicKeyInfo') {
        $spki = $key.ExportSubjectPublicKeyInfo()
    } else {
        # Windows PowerShell 5.1: build the SPKI DER by hand (RSA only).
        if ($key.Oid.Value -ne '1.2.840.113549.1.1.1') { Die "unexpected CA key type $($key.Oid.FriendlyName); run this from PowerShell 7" }
        function Der-Length([int]$n) {
            if ($n -lt 128) { return ,[byte]$n }
            if ($n -lt 256) { return [byte[]](0x81, $n) }
            return [byte[]](0x82, ($n -shr 8), ($n -band 0xff))
        }
        function Der-Tlv([byte]$tag, [byte[]]$content) { return [byte[]]@($tag) + (Der-Length $content.Length) + $content }
        $alg  = Der-Tlv 0x30 ([byte[]](0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00))
        $bits = Der-Tlv 0x03 ([byte[]]@(0) + $key.EncodedKeyValue.RawData)
        $spki = Der-Tlv 0x30 ($alg + $bits)
    }
    $sha = [System.Security.Cryptography.SHA256]::Create().ComputeHash([byte[]]$spki)
    return [Convert]::ToBase64String($sha)
}

# ------------------------------------------------------------------- icon
# Draws docs/brand/browser-icon.svg with GDI+ (plum ink tile, petal glow,
# the 3x3 mosaic mark) and wraps the 256 px PNG as an .ico.
function Write-Icon($path) {
    Add-Type -AssemblyName System.Drawing
    function New-RoundedRect([float]$x, [float]$y, [float]$w, [float]$h, [float]$r) {
        $p = New-Object System.Drawing.Drawing2D.GraphicsPath
        $d = 2 * $r
        $p.AddArc($x, $y, $d, $d, 180, 90)
        $p.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
        $p.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
        $p.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
        $p.CloseFigure()
        return $p
    }
    $petal = @(255, 111, 174)   # #FF6FAE
    $bmp = New-Object System.Drawing.Bitmap 256, 256
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)
    $ink = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#16121A'))
    $g.FillPath($ink, (New-RoundedRect 0 0 256 256 56))
    $glowPath = New-Object System.Drawing.Drawing2D.GraphicsPath
    $glowPath.AddEllipse(10, 10, 236, 236)
    $glow = New-Object System.Drawing.Drawing2D.PathGradientBrush $glowPath
    $glow.CenterColor = [System.Drawing.Color]::FromArgb(80, $petal[0], $petal[1], $petal[2])
    $glow.SurroundColors = @([System.Drawing.Color]::FromArgb(0, $petal[0], $petal[1], $petal[2]))
    $g.FillPath($glow, $glowPath)
    $s = 3.0435; $o = 58
    $opacity = @(0.25, 0.6, 0.25, 0.6, 1.0, 0.6, 0.25, 0.6, 0.25)
    for ($i = 0; $i -lt 9; $i++) {
        $col = $i % 3; $row = [math]::Floor($i / 3)
        $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb([int](255 * $opacity[$i]), $petal[0], $petal[1], $petal[2]))
        $g.FillPath($brush, (New-RoundedRect ($o + $col * 16 * $s) ($o + $row * 16 * $s) (14 * $s) (14 * $s) (3 * $s)))
    }
    $g.Dispose()
    $pngStream = New-Object System.IO.MemoryStream
    $bmp.Save($pngStream, [System.Drawing.Imaging.ImageFormat]::Png)
    $png = $pngStream.ToArray()
    $ico = New-Object System.IO.MemoryStream
    $w = New-Object System.IO.BinaryWriter $ico
    $w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]1)          # ICONDIR: reserved, type icon, one image
    $w.Write([byte]0); $w.Write([byte]0); $w.Write([byte]0); $w.Write([byte]0)  # 256x256 (0 means 256), palette, reserved
    $w.Write([uint16]1); $w.Write([uint16]32)                             # planes, bits per pixel
    $w.Write([uint32]$png.Length); $w.Write([uint32]22)                   # size, offset past the 22-byte header
    $w.Write($png); $w.Flush()
    New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
    [System.IO.File]::WriteAllBytes($path, $ico.ToArray())
}

# --------------------------------------------------------------- shortcut
function Install-Shortcuts($chromium) {
    Log "Adding the $BrowserName shortcuts"
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    Write-Icon $IconPath
    $spki = Get-CaSpkiHash
    $shell = New-Object -ComObject WScript.Shell
    foreach ($path in $Shortcuts) {
        $lnk = $shell.CreateShortcut($path)
        $lnk.TargetPath = $chromium
        $lnk.Arguments = "--user-data-dir=`"$ProfileDir`" --proxy-server=$ProxyHost`:$ProxyPort " +
                         "--ignore-certificate-errors-spki-list=$spki --no-first-run --no-default-browser-check"
        $lnk.WorkingDirectory = Split-Path $chromium
        $lnk.IconLocation = "$IconPath,0"
        $lnk.Description = 'Chromium through the Sissy Censor System proxy'
        $lnk.Save()
    }
}

function Uninstall-Shortcuts {
    Log "Removing the $BrowserName shortcuts and profile"
    foreach ($path in $Shortcuts) { Remove-Item -Force -ErrorAction SilentlyContinue $path }
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $BrowserHome
}

# -------------------------------------------------------------------- main
if ($Uninstall) {
    Uninstall-Shortcuts
    Log "Done. Docker Desktop, Chromium and $ScsDir were left in place; stop the proxy with"
    Write-Host "    cd $ScsDir\src; docker compose down"
    exit 0
}

Install-DockerDesktop
Start-DockerDesktop
Get-Repo
Start-Proxy
$chromium = Install-Chromium
Install-Shortcuts $chromium

Log 'Installed'
Write-Host @"
    Shortcut:     $BrowserName (Start menu and desktop)
    Profile:      $ProfileDir
    Proxy:        $ProxyHost`:$ProxyPort (Docker, starts with Windows)
    Admin panel:  http://localhost:8082/admin
    Checkout:     $ScsDir
    Only $BrowserName goes through the proxy; your other browsers are unchanged.
    The shortcut bakes in the CA's key hash: rerun this installer if you ever
    delete $ScsDir\src\mitmproxy and the proxy makes a new CA.
"@
