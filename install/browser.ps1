<#
Sissy Censor System: dedicated browser installer for Windows.

Installs Docker Desktop, Chromium and the sqlite3 tool with winget, pulls
the proxy image from Docker Hub and starts it from %USERPROFILE%\scs, adds
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

Remove the shortcut, its profile and the update task (keeps Docker Desktop,
Chromium and %USERPROFILE%\scs):

  $env:SCS_UNINSTALL='1'; irm https://raw.githubusercontent.com/that-trans-fae/sissy-censor-system/main/install/browser.ps1 | iex; Remove-Item Env:SCS_UNINSTALL

Environment overrides: SCS_IMAGE_REPO, SCS_CHANNEL, SCS_DIR, SCS_CHROMIUM_EXE,
and SCS_GPU=0 to leave the GPU out of the container.
#>
& {
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- settings
$ScsImageRepo = if ($env:SCS_IMAGE_REPO) { $env:SCS_IMAGE_REPO } else { 'thattransfae/scs' }
# The tag installs follow; scs-update.ps1 moves to whatever it points at.
$ScsChannel   = if ($env:SCS_CHANNEL) { $env:SCS_CHANNEL } else { "$ScsImageRepo`:latest" }
$ScsDir       = if ($env:SCS_DIR) { $env:SCS_DIR } else { Join-Path $env:USERPROFILE 'scs' }
$Uninstall    = $env:SCS_UNINSTALL -eq '1'

$BrowserName = 'Sissy Browser'
$ProxyHost = '127.0.0.1'
$ProxyPort = 8080
$CaPem = Join-Path $ScsDir 'mitmproxy\mitmproxy-ca-cert.pem'
$UpdateTask = 'Sissy Censor System update'
$DockerDesktopExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$ChromiumWingetId = 'Hibbiki.Chromium'
$ChromiumMinVersion = 134   # the profile certificate store (ServerCertificate)
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
# The profile certificate store needs Chromium 134; an older one that is
# already installed is updated (winget install upgrades what it finds).
function Install-Chromium {
    $exe = Find-Chromium
    if (-not $exe) {
        Log 'Installing Chromium'
        Winget-Install $ChromiumWingetId
        $exe = Find-Chromium
    } elseif ((Chromium-Version $exe).Major -lt $ChromiumMinVersion) {
        Log "Updating Chromium $(Chromium-Version $exe) at $exe ($ChromiumMinVersion or newer is needed)"
        Winget-Install $ChromiumWingetId
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
        Start-Process -Wait -WindowStyle Hidden -FilePath $chromium -ArgumentList @(
            "--user-data-dir=`"$ProfileDir`"", '--headless=new', '--disable-gpu', '--no-first-run', '--dump-dom', 'about:blank')
    }
    if (-not (Test-Path $db)) { Die "Chromium $(Chromium-Version $chromium) at $chromium did not create $db" }
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
function Install-Shortcuts($chromium) {
    Log "Adding the $BrowserName shortcuts"
    New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    Write-Icon $IconPath
    $shell = New-Object -ComObject WScript.Shell
    foreach ($path in $Shortcuts) {
        $lnk = $shell.CreateShortcut($path)
        $lnk.TargetPath = $chromium
        $lnk.Arguments = "--user-data-dir=`"$ProfileDir`" --proxy-server=$ProxyHost`:$ProxyPort " +
                         "--no-first-run --no-default-browser-check"
        $lnk.WorkingDirectory = Split-Path $chromium
        $lnk.IconLocation = "$IconPath,0"
        $lnk.Description = 'Chromium through the Sissy Censor System proxy'
        $lnk.Save()
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

# -------------------------------------------------------------------- main
try {
    if ($Uninstall) {
        Uninstall-Shortcuts
        Uninstall-Updater
        Log "Done. Docker Desktop, Chromium and $ScsDir were left in place; stop the proxy with"
        Write-Host "    cd $ScsDir; docker compose down"
        return
    }

    Install-DockerDesktop
    Start-DockerDesktop
    Get-Proxy
    Set-Gpu
    Start-Proxy
    $chromium = Install-Chromium
    Install-Shortcuts $chromium
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
    Only $BrowserName goes through the proxy; your other browsers are unchanged.
    The CA is trusted inside the profile: rerun this installer if you ever
    delete $ScsDir\mitmproxy and the proxy makes a new CA.
"@
} catch {
    Write-Host "error: $($_.Exception.Message)" -ForegroundColor Red
}
}
