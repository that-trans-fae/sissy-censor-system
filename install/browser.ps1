<#
Sissy Censor System: dedicated browser installer for Windows.

Installs Docker Desktop and the sqlite3 tool with winget and Chromium from
Hibbiki's GitHub releases (the build winget's Hibbiki.Chromium packages),
pulls the proxy image from Docker Hub and starts it from %USERPROFILE%\scs, adds
a scheduled task that checks for updates at each sign-in (scs-update.ps1,
from the image), and adds a "Sissy Browser" shortcut to the Start menu and desktop:
a separate Chromium profile that always goes through the proxy and trusts
its CA in the profile's own certificate store. Nothing system-wide changes:
no Windows proxy, no CA in the Windows store, and your usual browsers are
untouched. Linux and macOS: install/browser.sh.

Paste into a normal PowerShell window (winget asks for elevation when it
needs it):

  irm https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.ps1 | iex

Not "powershell -ExecutionPolicy Bypass -c ..." from cmd or Run: Defender
blocks that command line as Trojan:Win32/Commando.A!ml, and execution policy
doesn't apply to iex anyway. Since the script runs in the window's own
session, it keeps to a child scope and stops with throw, never exit, which
would close the window.

Uninstall: removes the shortcut, its profile, the update task and the
proxy's container and image, asks before removing Docker Desktop, Chromium
and WSL, and deletes %USERPROFILE%\scs (config, logs, CA). The -Uninstall
switch, or SCS_UNINSTALL=1 in the environment, which "irm | iex" can't pass
a switch to:

  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.ps1))) -Uninstall

Environment overrides: SCS_IMAGE_REPO, SCS_CHANNEL, SCS_DIR, SCS_CHROMIUM_EXE,
and SCS_GPU=0 to leave the GPU out of the container.
#>
param([switch]$Uninstall)
& {
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- settings
$ScsImageRepo = if ($env:SCS_IMAGE_REPO) { $env:SCS_IMAGE_REPO } else { 'thattransfae/scs' }
# The tag installs follow; scs-update.ps1 moves to whatever it points at.
$ScsChannel   = if ($env:SCS_CHANNEL) { $env:SCS_CHANNEL } else { "$ScsImageRepo`:latest" }
$ScsDir       = if ($env:SCS_DIR) { $env:SCS_DIR } else { Join-Path $env:USERPROFILE 'scs' }
$Uninstall    = $Uninstall -or $env:SCS_UNINSTALL -eq '1'

$BrowserName = 'Sissy Browser'
$ProxyHost = '127.0.0.1'
$ProxyPort = 8080
# Google search goes around the proxy. Google answers a Chromium that reaches
# it over TCP instead of QUIC (HTTP/3) with a CAPTCHA on every search, and an
# HTTP proxy can't carry QUIC. The browser connects directly, and the
# ForceGoogleSafeSearch policy (Set-SafeSearchPolicy) keeps its searches
# filtered at Google's end (docs/decisions.md).
$ProxyBypass = 'www.google.com'
$PolicyKey = 'HKLM:\Software\Policies\Chromium'
$CaPem = Join-Path $ScsDir 'mitmproxy\mitmproxy-ca-cert.pem'
$UpdateTask = 'Sissy Censor System update'
$DockerDesktopExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$ChromiumWingetId = 'Hibbiki.Chromium'   # for the uninstall; the install fetches the release itself
$ChromiumReleaseApi = 'https://api.github.com/repos/Hibbiki/chromium-win64/releases/latest'
$ChromiumLatestUrl = 'https://github.com/Hibbiki/chromium-win64/releases/latest/download/mini_installer.exe'
# Where winget puts a package whose manifest says "portable" (Hibbiki.Chromium
# 155.0.8059.85 did, Oct 2026): the installer copied aside, not run.
$ChromiumPortableDir = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Hibbiki.Chromium_Microsoft.Winget.Source_8wekyb3d8bbwe'
# The profile certificate store (ServerCertificate) exists from Chromium
# 134; 134-136 only create it with this feature on (it is on by default from
# 137, and later builds ignore the name), so it goes on every start.
$ChromiumMinVersion = 134
$ChromiumFeatures = '--enable-features=EnableCertManagementUIV2Write'
$BrowserHome = Join-Path $env:LOCALAPPDATA 'SissyBrowser'
$ProfileDir = Join-Path $BrowserHome 'profile'
$IconPath = Join-Path $BrowserHome 'sissy-browser.ico'
$AdminUrl = 'http://localhost:8082/admin'
$AdminBookmark = 'SCS admin panel'
$Shortcuts = @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$BrowserName.lnk"),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) "$BrowserName.lnk")
)

# ----------------------------------------------------------------- helpers
function Log($msg)  { Write-Host "==> $msg" -ForegroundColor Magenta }
function Warn($msg) { Write-Host "warning: $msg" -ForegroundColor Yellow }
function Die($msg)  { throw $msg }   # reported by the catch around main
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
# Runs a native command with all its output discarded; check $LASTEXITCODE.
# Under $ErrorActionPreference Stop, Windows PowerShell turns every stderr
# line of a redirected native command into a terminating error (docker
# compose prints its progress there), so the preference is relaxed here.
function Quiet([scriptblock]$cmd) {
    $ErrorActionPreference = 'Continue'
    & $cmd *> $null
}
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
    Quiet { wsl --status }
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
        Quiet { docker info }
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

# --------------------------------------------------------------- the proxy
# KEY=value in scs\.env, which compose reads; other keys are kept.
function Set-EnvValue($key, $value) {
    $file = Join-Path $ScsDir '.env'
    $lines = @(if (Test-Path $file) { Get-Content $file } else { '# Compose settings for this install (written by the installer and scs-update).' })
    $done = $false
    $out = foreach ($l in $lines) {
        if ($l.StartsWith("$key=")) { if (-not $done) { "$key=$value"; $done = $true } } else { $l }
    }
    if (-not $done) { $out = @($out) + "$key=$value" }
    [System.IO.File]::WriteAllLines($file, [string[]]@($out), (New-Object System.Text.UTF8Encoding $false))
}

# Earlier installers cloned the repository and built the image in
# $ScsDir\src. Stop that container and keep its CA (which the profile
# already trusts) and settings.
function Move-OldCheckout {
    $old = Join-Path $ScsDir 'src'
    if (-not (Test-Path (Join-Path $old 'compose.yaml'))) { return }
    Log 'Moving the CA and settings out of the old checkout layout'
    Push-Location $old
    try { docker compose down } finally { Pop-Location }
    foreach ($d in 'mitmproxy', 'config') {
        $from = Join-Path $old $d; $to = Join-Path $ScsDir $d
        if ((Test-Path $from) -and -not (Test-Path $to)) { Copy-Item -Recurse $from $to }
    }
}

function Get-Proxy {
    Log "Downloading $ScsChannel (over a gigabyte the first time)"
    docker pull $ScsChannel
    if ($LASTEXITCODE -ne 0) { Die "docker pull $ScsChannel failed" }
    $repo = $ScsChannel.Substring(0, $ScsChannel.LastIndexOf(':'))
    $image = docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' $ScsChannel |
        Where-Object { $_.StartsWith("$repo@sha256:") } | Select-Object -First 1
    if (-not $image) { Die "pulled $ScsChannel but found no digest for it" }
    # A local tag named after the digest keeps repo@sha256:... resolvable once
    # the channel tag moves on (scs-update.ps1 does the same).
    docker tag $image ($repo + ':sha256-' + ($image -split '@sha256:')[1])
    $envFile = Join-Path $ScsDir '.env'
    $script:OldImage = if (Test-Path $envFile) {
        (Get-Content $envFile | Where-Object { $_.StartsWith('SCS_IMAGE=') } | Select-Object -Last 1) -replace '^SCS_IMAGE=', ''
    }
    $script:NewImage = $image
    New-Item -ItemType Directory -Force -Path $ScsDir | Out-Null
    Move-OldCheckout
    foreach ($d in 'mitmproxy', 'config', 'logs') { New-Item -ItemType Directory -Force -Path (Join-Path $ScsDir $d) | Out-Null }
    # compose.yaml and the updater come out of the image.
    $cid = docker create $image
    if ($LASTEXITCODE -ne 0) { Die 'docker create failed' }
    docker cp "${cid}:/app/dist/." $ScsDir
    $copied = $LASTEXITCODE
    docker rm $cid | Out-Null
    if ($copied -ne 0) { Die 'could not copy the install files out of the image' }
    Set-EnvValue 'SCS_CHANNEL' $ScsChannel
    Set-EnvValue 'SCS_IMAGE' $image
    # scs-update.ps1 applies an update only while no browser runs on this profile.
    Set-EnvValue 'SCS_PROFILE' $ProfileDir
}

# An NVIDIA GPU for video decoding and encoding (video.hwaccel), through
# Docker Desktop's WSL2 backend: compose passes it in through
# compose.nvidia.yaml, which COMPOSE_FILE in .env adds. Kept only if the
# container's own test encode works with it, since a GPU compose can't set
# up stops the proxy from starting.
function Set-Gpu {
    $files = 'compose.yaml'
    if ($env:SCS_GPU -ne '0' -and (Have 'nvidia-smi')) {
        Log 'Looking for a GPU for video'
        Push-Location $ScsDir
        try {
            Quiet { docker compose -f compose.yaml -f compose.nvidia.yaml run --rm --no-deps -T censor-proxy python -m censor_proxy.hwaccel }
            if ($LASTEXITCODE -eq 0) { $files = 'compose.yaml;compose.nvidia.yaml' }
        } finally { Pop-Location }
    }
    Log $(if ($files -ne 'compose.yaml') { 'Video will use the NVIDIA GPU' } else { 'No usable GPU found; video uses the CPU' })
    Set-EnvValue 'COMPOSE_FILE' $files
}

function Start-Proxy {
    # No quotes inside the template: Windows PowerShell strips them from a
    # native command's arguments.
    $other = docker ps --filter "publish=$ProxyPort" --format '{{.Names}} {{.Labels}}' |
        Where-Object { $_ -notmatch 'com\.docker\.compose\.project=scs(,|$)' } | Select-Object -First 1
    if ($other) { $name = ($other -split ' ')[0]; Die "another container ($name) holds port $ProxyPort; stop it (docker stop $name) and rerun" }
    Log 'Starting the proxy'
    Push-Location $ScsDir
    try {
        docker compose up -d
        if ($LASTEXITCODE -ne 0) { Die 'docker compose up failed' }
    } finally { Pop-Location }
    # The release a reinstall replaced.
    if ($script:OldImage -and $script:OldImage -ne $script:NewImage) {
        Quiet { docker image rm (($script:OldImage -split '@sha256:')[0] + ':sha256-' + ($script:OldImage -split '@sha256:')[1]) }
    }
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
            if ($waited -ge 60) { Warn "health check through the proxy failed; check 'docker compose logs' in $ScsDir"; break }
            Start-Sleep 3; $waited += 3
        }
    }
}

# One scs-update.ps1 run each time you sign in, as a task of this user, a
# minute after sign-in. conhost --headless keeps a console window from
# showing. The run may last while it waits for the browser to close, so it
# has no time limit.
function Install-Updater {
    Log 'Adding the update check at sign-in'
    $script = Join-Path $ScsDir 'scs-update.ps1'
    $action = New-ScheduledTaskAction -Execute 'conhost.exe' `
        -Argument "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = 'PT1M'
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $UpdateTask -Action $action -Trigger $trigger -Settings $settings `
        -Description 'Keeps the Sissy Censor System proxy up to date' -Force | Out-Null
}

function Uninstall-Updater {
    Log 'Removing the update task'
    Unregister-ScheduledTask -TaskName $UpdateTask -Confirm:$false -ErrorAction SilentlyContinue
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

function Chromium-Version($exe) {
    [version]((Get-Item $exe).VersionInfo.ProductVersion -replace '[^0-9.].*$')
}
# Hibbiki's Chromium build from its GitHub release: the installer and its
# SHA-256 from the release's asset list (the stable latest-download URL,
# unverified, if the API is unreachable), run per user, or system-level
# (an elevation prompt) to update a Chromium in Program Files. Not winget:
# its Hibbiki.Chromium manifest shipped as "portable" in Oct 2026, which
# installs nothing.
# A web request, retried over transient errors (GitHub answered 500 to a
# first attempt once, and the retry went through).
function Invoke-Retrying([scriptblock]$request) {
    for ($attempt = 1; ; $attempt++) {
        try { return (& $request) }
        catch {
            if ($attempt -ge 4) { throw }
            Warn "$($_.Exception.Message) (attempt $attempt of 4; retrying in 5 s)"
            Start-Sleep 5
        }
    }
}
function Install-ChromiumBuild([bool]$systemLevel) {
    $ProgressPreference = 'SilentlyContinue'   # the progress bar makes Invoke-WebRequest crawl
    $url = $ChromiumLatestUrl; $digest = $null; $tag = ''
    try {
        $rel = Invoke-Retrying { Invoke-RestMethod -Uri $ChromiumReleaseApi -UseBasicParsing }
        $asset = $rel.assets | Where-Object { $_.name -eq 'mini_installer.exe' } | Select-Object -First 1
        if ($asset) { $url = $asset.browser_download_url; $digest = "$($asset.digest)" -replace '^sha256:', ''; $tag = " $($rel.tag_name)" }
    } catch { Warn "could not read the Chromium release list ($($_.Exception.Message)); the latest build is downloaded unverified" }
    Log "Downloading Chromium$tag (about 130 MB)"
    $exe = Join-Path $env:TEMP 'scs-chromium-mini_installer.exe'
    Invoke-Retrying { Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing }
    if ($digest) {
        $have = (Get-FileHash -Algorithm SHA256 $exe).Hash
        if ($have -ne $digest.ToUpper()) { Remove-Item -Force $exe; Die "the Chromium download's SHA-256 ($have) is not the release's ($digest)" }
    }
    Log $(if ($systemLevel) { 'Installing Chromium (it asks to run as administrator)' } else { 'Installing Chromium' })
    $opts = @('--do-not-launch-chrome')
    if ($systemLevel) { Start-Process -Wait -Verb RunAs -FilePath $exe -ArgumentList ($opts + '--system-level') }
    else { Start-Process -Wait -FilePath $exe -ArgumentList $opts }
    Remove-Item -Force -ErrorAction SilentlyContinue $exe
}
# The profile certificate store needs Chromium 134; an older one that is
# already installed is updated in place.
function Install-Chromium {
    $exe = Find-Chromium
    if (-not $exe) {
        if (Test-Path $ChromiumPortableDir) {
            Log "Removing winget's stray Hibbiki.Chromium record (its manifest installed nothing)"
            winget uninstall -e --id $ChromiumWingetId --accept-source-agreements --silent | Out-Null
        }
        Install-ChromiumBuild $false
        $exe = Find-Chromium
    } elseif ((Chromium-Version $exe).Major -lt $ChromiumMinVersion) {
        Log "Chromium $(Chromium-Version $exe) at $exe is too old ($ChromiumMinVersion or newer is needed)"
        Install-ChromiumBuild ($exe -like "$env:ProgramFiles\*" -or $exe -like "${env:ProgramFiles(x86)}\*")
        $exe = Find-Chromium
    }
    if (-not $exe) { Die 'could not find chrome.exe after installing Chromium; set SCS_CHROMIUM_EXE to its path' }
    if ((Chromium-Version $exe).Major -lt $ChromiumMinVersion) {
        Die "Chromium $(Chromium-Version $exe) at $exe is too old ($ChromiumMinVersion or newer is needed); update it or set SCS_CHROMIUM_EXE to a newer one"
    }
    return $exe
}

# ------------------------------------------------------------------- trust
# The proxy CA goes into the profile's own certificate store (the "Installed
# by you" list in chrome://certificate-manager): a SQLite file Chromium
# creates on first start, written here with the sqlite3 command-line tool.
# No flag and no system-wide trust is needed.
function Find-Sqlite {
    Refresh-Path
    $cmd = Get-Command sqlite3.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $pkgs = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path $pkgs) {
        $hit = Get-ChildItem -Path $pkgs -Recurse -Filter sqlite3.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Test-ProfileInUse {
    $lock = Join-Path $ProfileDir 'lockfile'
    if (-not (Test-Path $lock)) { return $false }
    try { [System.IO.File]::Open($lock, 'Open', 'ReadWrite', 'None').Dispose(); return $false } catch { return $true }
}

function Trust-CaInProfile($chromium) {
    $sqlite = Find-Sqlite
    if (-not $sqlite) { Log 'Installing the SQLite command-line tool'; Winget-Install 'SQLite.SQLite'; $sqlite = Find-Sqlite }
    if (-not $sqlite) { Die 'could not find sqlite3.exe after installing SQLite.SQLite' }
    $db = Join-Path $ProfileDir 'Default\ServerCertificate'
    if (-not (Test-Path $db)) {
        if (Test-ProfileInUse) { Warn "$BrowserName is running; close it and rerun to trust the proxy CA"; return }
        Log 'Initialising the profile'
        # Its output (the blank page's DOM, a harmless registry error from a
        # system-level Chromium) is kept out of the console and shown only
        # when the store does not appear.
        $log = Join-Path $env:TEMP 'sissy-browser-init'
        Start-Process -Wait -WindowStyle Hidden -FilePath $chromium -ArgumentList @(
            "--user-data-dir=`"$ProfileDir`"", $ChromiumFeatures, '--headless=new', '--disable-gpu', '--no-first-run', '--dump-dom', 'about:blank') `
            -RedirectStandardOutput "$log.out" -RedirectStandardError "$log.err"
        if (-not (Test-Path $db)) { Get-Content "$log.err" -ErrorAction SilentlyContinue | Select-Object -Last 10 | Write-Host }
        Remove-Item -Force -ErrorAction SilentlyContinue "$log.out", "$log.err"
    }
    if (-not (Test-Path $db)) { Die "Chromium $(Chromium-Version $chromium) at $chromium did not create $db ($ChromiumMinVersion or newer is needed)" }
    # A running browser holds the store locked. An earlier run has usually
    # trusted the CA already, so that is a warning, not a stop: the steps
    # after this one (the update task) still run.
    if (Test-ProfileInUse) {
        Warn "$BrowserName is running, so the proxy CA was not written again; close it and rerun if pages show certificate warnings"
        return
    }
    Log "Trusting the proxy CA in the $BrowserName profile"
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CaPem)
    $der = $cert.RawData
    $hash = ([System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($der))) -replace '-'
    $derHex = ([System.BitConverter]::ToString($der)) -replace '-'
    # trust_settings is a CertificateMetadata protobuf: trust { trust_type: TRUSTED }
    & $sqlite $db "INSERT OR IGNORE INTO certificates VALUES ('$hash', X'$derHex', X'0A020803');"
    if ($LASTEXITCODE -ne 0) { Die 'sqlite3 could not write the certificate into the profile' }
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
# The taskbar groups windows by AppUserModelID, and Chromium gives every
# browser window one of its own (browser_window_property_manager_win.cc):
# its registered id (Chromium, plus a per-user suffix for a per-user
# install) and the profile, which shell_integration_win.cc builds from the
# user-data-dir's and the profile's directory names (letters, digits and
# dots kept), joined by ShellUtil::BuildAppUserModelId: at most 64
# characters, so each of the two parts keeps at most 31, a longer one its
# first 15 and last 16 (the per-user id is 35). With --user-data-dir
# Chromium's profile shortcut manager is off, so the window gets no relaunch
# command or icon. The shortcuts carry the same id, so Windows groups the
# window under them and pins through them, with their flags and icon.
function Get-ChromiumAppId($chromium) {
    $registered = Get-ItemProperty -ErrorAction SilentlyContinue -Name AppUserModelID -Path `
        'HKCU:\Software\Classes\ChromiumHTM*\Application', 'HKLM:\Software\Classes\ChromiumHTM*\Application' |
        Select-Object -First 1
    if ($registered) { $app = $registered.AppUserModelID }
    else {
        # Not registered: Chromium's own rule. Per-user installs get a dot and
        # the base32 of the MD5 of the user's SID (ShellUtil's suffix).
        $app = 'Chromium'
        if (-not $chromium.StartsWith($env:ProgramFiles, 'OrdinalIgnoreCase')) {
            $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
            $md5 = [System.Security.Cryptography.MD5]::Create().ComputeHash([System.Text.Encoding]::ASCII.GetBytes($sid))
            $alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
            $bits = ($md5 | ForEach-Object { [Convert]::ToString($_, 2).PadLeft(8, '0') }) -join ''
            $b32 = ''
            for ($i = 0; $i + 5 -le $bits.Length; $i += 5) { $b32 += $alphabet[[Convert]::ToInt32($bits.Substring($i, 5), 2)] }
            if ($bits.Length % 5) { $b32 += $alphabet[[Convert]::ToInt32($bits.Substring($i).PadRight(5, '0'), 2)] }
            $app = "Chromium.$b32"
        }
    }
    $profile = ((Split-Path -Leaf $ProfileDir) + '.Default') -replace '[^A-Za-z0-9.]', ''
    $max = [math]::Floor((64 - 1) / 2)
    $parts = foreach ($c in $app, $profile) {
        if ($c.Length -le $max) { $c }
        else { $c.Substring(0, [math]::Floor($max / 2)) + $c.Substring($c.Length - [math]::Floor(($max + 1) / 2)) }
    }
    return $parts -join '.'
}

# Writes System.AppUserModel.ID into a .lnk (its property store).
function Set-ShortcutAppId($path, $appId) {
    if (-not ('Scs.Lnk' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
namespace Scs {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Sequential)]
    public struct PropVariant { public ushort vt; public ushort r1; public ushort r2; public ushort r3; public IntPtr p; public IntPtr p2; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }
    [ComImport, Guid("00021401-0000-0000-C000-000000000046"), ClassInterface(ClassInterfaceType.None)]
    public class ShellLink { }
    public static class Lnk {
        public static void SetAppId(string path, string appId) {
            object link = new ShellLink();
            ((IPersistFile)link).Load(path, 2);   // STGM_READWRITE
            PropertyKey key = new PropertyKey();
            key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");   // System.AppUserModel.ID
            key.pid = 5;
            PropVariant value = new PropVariant();
            value.vt = 31;   // VT_LPWSTR
            value.p = Marshal.StringToCoTaskMemUni(appId);
            try {
                IPropertyStore store = (IPropertyStore)link;
                store.SetValue(ref key, ref value);
                store.Commit();
                ((IPersistFile)link).Save(path, true);
            } finally { Marshal.FreeCoTaskMem(value.p); }
        }
    }
}
'@
    }
    [Scs.Lnk]::SetAppId($path, $appId)
}

function Install-Shortcuts($chromium) {
    Log "Adding the $BrowserName shortcuts"
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    Write-Icon $IconPath
    $appId = Get-ChromiumAppId $chromium
    $shell = New-Object -ComObject WScript.Shell
    foreach ($path in $Shortcuts) {
        $lnk = $shell.CreateShortcut($path)
        $lnk.TargetPath = $chromium
        $lnk.Arguments = "--user-data-dir=`"$ProfileDir`" --proxy-server=$ProxyHost`:$ProxyPort " +
                         "--proxy-bypass-list=$ProxyBypass " +
                         "--no-first-run --no-default-browser-check $ChromiumFeatures"
        $lnk.WorkingDirectory = Split-Path $chromium
        $lnk.IconLocation = "$IconPath,0"
        $lnk.Description = 'Chromium through the Sissy Censor System proxy'
        $lnk.Save()
        try { Set-ShortcutAppId $path $appId }
        catch { Warn "could not give $path Chromium's taskbar id ($($_.Exception.Message)); its windows will show as Chromium" }
    }
    # A taskbar pin is its own copy of the shortcut, made when it was pinned,
    # so one from an older install has no id (or a wrong one).
    $pins = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
    $stale = $false
    foreach ($pin in Get-ChildItem -ErrorAction SilentlyContinue -Path $pins -Filter *.lnk) {
        $lnk = $shell.CreateShortcut($pin.FullName)
        if ($lnk.Arguments -notlike "*$ProfileDir*") { continue }
        $lnk.Arguments = $Shortcuts | Select-Object -First 1 | ForEach-Object { $shell.CreateShortcut($_).Arguments }
        $lnk.IconLocation = "$IconPath,0"
        $lnk.Save()
        try { Set-ShortcutAppId $pin.FullName $appId; $stale = $true }
        catch { Warn "could not update the taskbar pin $($pin.Name) ($($_.Exception.Message))" }
    }
    if ($stale) { Warn "if $BrowserName still shows as a separate Chromium button, unpin it and pin it again from the Start menu" }
}

# Locks SafeSearch on in Chromium (ForceGoogleSafeSearch), since Google
# search doesn't go through the proxy. Policies are per Chromium install, not
# per profile, and every Policies key is admin-only (HKCU's included), so an
# elevated PowerShell writes the machine key. Nothing is asked when the value
# is already as wanted; a declined prompt warns and the install goes on.
function Set-SafeSearchPolicy([bool]$on) {
    $current = (Get-ItemProperty -Path $PolicyKey -Name ForceGoogleSafeSearch -ErrorAction SilentlyContinue).ForceGoogleSafeSearch
    if ($on -and $current -eq 1) { return }
    if (-not $on -and $null -eq $current) { return }
    if ($on) {
        Log 'Locking SafeSearch on in Chromium (asks to run as administrator)'
        $cmd = "New-Item -Path '$PolicyKey' -Force | Out-Null; " +
               "New-ItemProperty -Path '$PolicyKey' -Name ForceGoogleSafeSearch -Value 1 -PropertyType DWord -Force | Out-Null"
    } else {
        Log 'Removing the SafeSearch lock from Chromium (asks to run as administrator)'
        $cmd = "Remove-ItemProperty -Path '$PolicyKey' -Name ForceGoogleSafeSearch -ErrorAction SilentlyContinue"
    }
    try {
        $p = Start-Process -Wait -PassThru -Verb RunAs -WindowStyle Hidden powershell.exe `
            -ArgumentList '-NoProfile', '-NonInteractive', '-Command', $cmd
        if ($p.ExitCode -ne 0) { throw "exit code $($p.ExitCode)" }
    } catch {
        Warn "the SafeSearch policy was not changed ($_); Google search in $BrowserName is unfiltered until it is"
    }
}

# Seeds the profile so the bookmark bar is shown with the admin panel on it.
# Chromium reads these files when the profile first opens and fills in the
# ids and checksum itself; an existing profile gets the bookmark inserted.
function Write-Json($path, $data) {
    $json = $data | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding $false))   # no BOM
}

function Seed-Profile {
    $default = Join-Path $ProfileDir 'Default'
    $bookmarks = Join-Path $default 'Bookmarks'
    $prefs = Join-Path $default 'Preferences'
    $lock = Join-Path $ProfileDir 'lockfile'
    if (Test-Path $lock) {
        try { [System.IO.File]::Open($lock, 'Open', 'ReadWrite', 'None').Dispose() }
        catch { Warn "$BrowserName is running; close it and rerun to add the admin panel bookmark"; return }
    }
    Log 'Putting the admin panel on the bookmark bar'
    New-Item -ItemType Directory -Force -Path $default | Out-Null
    $entry = [ordered]@{ name = $AdminBookmark; type = 'url'; url = $AdminUrl }
    if (-not (Test-Path $bookmarks)) {
        Write-Json $bookmarks ([ordered]@{
            roots = [ordered]@{
                bookmark_bar = [ordered]@{ children = @($entry); name = 'Bookmarks bar'; type = 'folder' }
                other        = [ordered]@{ children = @(); name = 'Other bookmarks'; type = 'folder' }
                synced       = [ordered]@{ children = @(); name = 'Mobile bookmarks'; type = 'folder' }
            }
            version = 1
        })
    } else {
        $data = Get-Content -Raw $bookmarks | ConvertFrom-Json
        $bar = $data.roots.bookmark_bar
        if (-not ($bar.children | Where-Object { $_.url -eq $AdminUrl })) {
            $bar.children = @([pscustomobject]$entry) + @($bar.children)
            $data.PSObject.Properties.Remove('checksum')   # Chromium recomputes it
            Write-Json $bookmarks $data
        }
    }
    if (-not (Test-Path $prefs)) {
        Write-Json $prefs @{ bookmark_bar = @{ show_on_all_tabs = $true } }
    } else {
        $data = Get-Content -Raw $prefs | ConvertFrom-Json
        if (-not $data.bookmark_bar) { $data | Add-Member -NotePropertyName bookmark_bar -NotePropertyValue ([pscustomobject]@{}) }
        if (-not $data.bookmark_bar.show_on_all_tabs) {
            $data.bookmark_bar | Add-Member -Force -NotePropertyName show_on_all_tabs -NotePropertyValue $true
            Write-Json $prefs $data
        }
    }
}

function Uninstall-Shortcuts {
    Log "Removing the $BrowserName shortcuts and profile"
    foreach ($path in $Shortcuts) { Remove-Item -Force -ErrorAction SilentlyContinue $path }
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $BrowserHome
}

# ---------------------------------------------------------------- uninstall
# A yes/no question in the window. No window, or no answer, means no.
function Ask($question) {
    if (-not [Environment]::UserInteractive) { return $false }
    try { $ans = Read-Host "==> $question [y/N]" } catch { return $false }
    return [bool]($ans -match '^(y|yes)$')
}
# A native command's stdout, stderr dropped (see Quiet).
function Capture([scriptblock]$cmd) {
    $ErrorActionPreference = 'Continue'
    & $cmd 2> $null
}
# wsl.exe prints UTF-16; names of the registered distributions, Docker
# Desktop's own left out.
function Wsl-Distros {
    if (-not (Have wsl)) { return @() }
    $prev = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::Unicode
        $out = Capture { wsl --list --quiet }
        if ($LASTEXITCODE -ne 0) { return @() }
    } finally { [Console]::OutputEncoding = $prev }
    return @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ -and $_ -notlike 'docker-desktop*' })
}
function Test-Wsl {
    if (-not (Have wsl)) { return $false }
    Quiet { wsl --status }
    return $LASTEXITCODE -eq 0
}
# The channel this install follows, for the image tags to remove.
function Channel-Repo {
    $channel = $ScsChannel
    $envFile = Join-Path $ScsDir '.env'
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_.StartsWith('SCS_CHANNEL=') } | Select-Object -Last 1
        if ($line) { $channel = ($line.Substring('SCS_CHANNEL='.Length) -replace '"', '') }
    }
    return $channel.Substring(0, $channel.LastIndexOf(':'))
}

function Remove-Proxy {
    Log 'Removing the proxy container and image'
    $repo = Channel-Repo
    $left = "the proxy container and image were left. With Docker Desktop running: docker rm -f (docker ps -aq --filter label=com.docker.compose.project=scs); docker image rm -f (docker images -q $repo)"
    if (-not (Test-Path $DockerDesktopExe)) { Warn "Docker Desktop is not installed; $left"; return }
    Refresh-Path
    Quiet { docker info }
    if ($LASTEXITCODE -ne 0) {
        if (-not (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue)) {
            Log 'Starting Docker Desktop to remove the proxy'
            Start-Process $DockerDesktopExe
        }
        $waited = 0
        while ($true) {
            Quiet { docker info }
            if ($LASTEXITCODE -eq 0) { break }
            if ($waited -ge 120) { Warn "Docker Desktop did not start; $left"; return }
            Start-Sleep 5; $waited += 5
        }
    }
    if (Test-Path (Join-Path $ScsDir 'compose.yaml')) {
        Push-Location $ScsDir
        try {
            docker compose down --remove-orphans
            if ($LASTEXITCODE -ne 0) { Warn 'docker compose down failed' }
        } finally { Pop-Location }
    }
    $ids = @(docker images --filter "reference=$repo" -q | Select-Object -Unique)
    foreach ($id in $ids) {
        Quiet { docker image rm -f $id }
        if ($LASTEXITCODE -ne 0) { Warn "image $id could not be removed" }
    }
}

# Docker Desktop's uninstaller takes its WSL distributions and data
# (every container and image) with it.
function Remove-DockerDesktop {
    Log 'Removing Docker Desktop (it asks to run as administrator)'
    Get-Process 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    winget uninstall -e --id Docker.DockerDesktop --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) { Warn "winget could not remove Docker Desktop (exit $LASTEXITCODE); remove it from Settings > Apps" }
}

function Remove-Chromium($exe) {
    Log "Removing Chromium ($exe)"
    Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | Stop-Process -Force -ErrorAction SilentlyContinue
    winget uninstall -e --id $ChromiumWingetId --accept-source-agreements --silent
    if (-not (Test-Path $exe)) { return }
    # Not winget's: Chromium's own uninstaller, next to chrome.exe.
    $setup = Get-ChildItem (Join-Path (Split-Path $exe) '*\Installer\setup.exe') -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $setup) { Warn "could not remove Chromium at $exe; remove it from Settings > Apps"; return }
    $opts = @('--uninstall', '--force-uninstall')
    if ($exe -like "$env:ProgramFiles\*" -or $exe -like "${env:ProgramFiles(x86)}\*") {
        Start-Process -Wait -Verb RunAs -FilePath $setup.FullName -ArgumentList ($opts + '--system-level')
    } else {
        Start-Process -Wait -FilePath $setup.FullName -ArgumentList $opts
    }
    if (Test-Path $exe) { Warn "Chromium is still at $exe; remove it from Settings > Apps" }
}

# The Store WSL package, every remaining distribution, and the Windows
# features behind them (a restart finishes that).
function Remove-Wsl {
    Log 'Removing the Windows Subsystem for Linux (asks to run as administrator)'
    Quiet { wsl --shutdown }
    foreach ($d in Wsl-Distros) { Quiet { wsl --unregister $d } }
    Quiet { wsl --uninstall }
    $cmd = 'dism /online /disable-feature /featurename:Microsoft-Windows-Subsystem-Linux /norestart; ' +
           'dism /online /disable-feature /featurename:VirtualMachinePlatform /norestart'
    Start-Process -Wait -Verb RunAs powershell.exe -ArgumentList '-NoProfile', '-Command', $cmd
}

# Only a folder that looks like ours: SCS_DIR can be pointed anywhere.
function Remove-ScsDir {
    if (-not (Test-Path $ScsDir)) { return }
    if (-not (Test-Path (Join-Path $ScsDir 'compose.yaml')) -and -not (Test-Path (Join-Path $ScsDir '.env'))) {
        Warn "$ScsDir has no compose.yaml or .env, so it was left alone"
        return
    }
    Log "Removing $ScsDir"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ScsDir
    if (Test-Path $ScsDir) { Warn "$ScsDir could not be removed entirely (a file in it is open, or your shell is inside it); delete it by hand" }
}

function Uninstall {
    # The questions first, so nothing is started only to be removed.
    $removeDocker = (Test-Path $DockerDesktopExe) -and (Ask 'Remove Docker Desktop too, with every container and image in it?')
    $chromium = Find-Chromium
    $removeChromium = $chromium -and (Ask "Remove Chromium too ($chromium)?")
    $removeWsl = $false
    if ((Test-Wsl) -and ($removeDocker -or -not (Test-Path $DockerDesktopExe))) {
        $distros = Wsl-Distros
        $q = 'Remove the Windows Subsystem for Linux too'
        if ($distros) { $q += ", with its Linux distributions ($($distros -join ', '))" }
        $removeWsl = Ask "$q?"
    }
    Uninstall-Shortcuts
    Set-SafeSearchPolicy $false
    Uninstall-Updater
    if ($removeDocker) { Remove-DockerDesktop } else { Remove-Proxy }
    if ($removeChromium) { Remove-Chromium $chromium }
    if ($removeWsl) { Remove-Wsl }
    Remove-ScsDir
    Log 'Done'
    if ($removeWsl) { Write-Host '    Restart Windows to finish removing WSL.' }
}

# -------------------------------------------------------------------- main
try {
    if ($Uninstall) { Uninstall; return }

    Install-DockerDesktop
    Start-DockerDesktop
    Get-Proxy
    Set-Gpu
    Start-Proxy
    $chromium = Install-Chromium
    Install-Shortcuts $chromium
    Set-SafeSearchPolicy $true
    Seed-Profile
    Trust-CaInProfile $chromium
    Install-Updater

    Log 'Installed'
    Write-Host @"
    Shortcut:     $BrowserName (Start menu and desktop)
    Profile:      $ProfileDir (bookmark bar: $AdminBookmark)
    Proxy:        $ProxyHost`:$ProxyPort (Docker, starts with Windows)
    Admin panel:  http://localhost:8082/admin
    Install:      $ScsDir (checks $ScsChannel for updates at each sign-in)
    Only $BrowserName goes through the proxy; your other browsers are unchanged,
    except that Google search runs with SafeSearch locked on in every Chromium profile.
    The CA is trusted inside the profile: rerun this installer if you ever
    delete $ScsDir\mitmproxy and the proxy makes a new CA.
"@
} catch {
    Write-Host "error: $($_.Exception.Message)" -ForegroundColor Red
}
}
