<#
Sissy Censor System updater for Windows (Linux and macOS: scs-update).

Lives in the install folder (%USERPROFILE%\scs) and runs once each time you
sign in, from the scheduled task the installer adds. The run does what
scs-update does: waits for Docker Desktop and the proxy, reads updates.*
through the container, pulls SCS_CHANNEL (retrying while the network comes
up), and once the Sissy Browser is closed recreates the container on a new
release, keeping it only once it reports healthy. The result goes to
logs\update.json (shown in the admin panel) and a history to update.log.
Nothing read from the container or the folders it can write (config, logs,
mitmproxy) is evaluated.

  powershell -ExecutionPolicy Bypass -File scs-update.ps1          what runs at sign-in
  powershell -ExecutionPolicy Bypass -File scs-update.ps1 -Now     check and apply at once
#>
param([switch]$Now)
$ErrorActionPreference = 'Continue'

$Dir = $PSScriptRoot
Set-Location $Dir
$Service = 'censor-proxy'
$State = Join-Path $Dir '.update-state'
$EnvFile = Join-Path $Dir '.env'
$LogFile = Join-Path $Dir 'update.log'
$HealthTimeout = 300
# At startup: how long to wait for Docker and the proxy, how many times to
# try the download (a minute apart), and how often to look whether the
# browser has closed.
$StartupWait = 900
$PullTries = 10
$Poll = 60
$script:Available = ''

function Log($msg) {
    Add-Content -Path $LogFile -Value ("{0} {1}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $msg)
    if ([Environment]::UserInteractive -and -not [Console]::IsOutputRedirected) { Write-Host $msg }
}

# KEY=value files (.env, .update-state), read and written without evaluating them.
function Kv-Get($file, $key) {
    if (-not (Test-Path $file)) { return '' }
    $line = Get-Content $file | Where-Object { $_.StartsWith("$key=") } | Select-Object -Last 1
    if (-not $line) { return '' }
    return $line.Substring($key.Length + 1).Trim('"')
}
function Kv-Set($file, $key, $value) {
    $lines = @(if (Test-Path $file) { Get-Content $file })
    $done = $false
    $out = foreach ($l in $lines) {
        if ($l.StartsWith("$key=")) { if (-not $done) { "$key=$value"; $done = $true } } else { $l }
    }
    if (-not $done) { $out = @($out) + "$key=$value" }
    $tmp = "$file.tmp"
    [System.IO.File]::WriteAllLines($tmp, [string[]]@($out), (New-Object System.Text.UTF8Encoding $false))
    Move-Item -Force $tmp $file
}

function Version-Of($image) {
    if (-not $image) { return '' }
    $v = (docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' $image 2>$null) -replace '[^A-Za-z0-9._+-]', ''
    if ($v -and $v -ne 'dev') { return $v }
    $d = ($image -split '@sha256:')[-1]
    return $d.Substring(0, [Math]::Min(12, $d.Length))
}

# logs\update.json for the panel, removed and created afresh (CreateNew), so
# nothing planted there is written through. Not renamed into place: Docker
# Desktop's file sharing goes on showing the container the old file.
function Write-Status($state, $message) {
    Log "${state}: $message"
    New-Item -ItemType Directory -Force -Path (Join-Path $Dir 'logs') | Out-Null
    $data = [ordered]@{
        checked_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        state = $state; message = $message
        version = (Version-Of (Kv-Get $EnvFile 'SCS_IMAGE')); available = $script:Available
    }
    $path = Join-Path $Dir 'logs\update.json'
    try {
        Remove-Item -Force $path -ErrorAction SilentlyContinue
        $bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes(($data | ConvertTo-Json -Compress))
        $f = [System.IO.File]::Open($path, 'CreateNew', 'Write', 'None')
        try { $f.Write($bytes, 0, $bytes.Length) } finally { $f.Dispose() }
    } catch { Log "couldn't write logs\update.json" }
}

function Container { (docker compose ps -aq $Service 2>$null | Select-Object -First 1) }

# Each image in use (running, being installed, or skipped as bad) keeps a
# local tag named after its digest. Without one, Docker's containerd image
# store forgets repo@sha256:... as soon as the channel tag moves on, and a
# rollback or `docker compose up` would download it again.
function Keep-Tag($image) { $at = $image.IndexOf('@sha256:'); return $image.Substring(0, $at) + ':sha256-' + $image.Substring($at + 8) }
function Keep($image) { if ($image) { docker tag $image (Keep-Tag $image) *> $null } }
function Unkeep($image) { if ($image) { docker image rm (Keep-Tag $image) *> $null } }

$script:Enabled = $true; $script:WaitForBrowser = $true
function Read-Policy {
    $out = docker compose exec -T $Service python -m censor_proxy.updates policy 2>$null
    if ($LASTEXITCODE -ne 0) { $out = docker compose run --rm -T --no-deps $Service python -m censor_proxy.updates policy 2>$null }
    if ($LASTEXITCODE -ne 0) { Log "couldn't read updates.* from config.yaml; using the defaults"; return }
    foreach ($line in @($out)) {
        switch -Regex ($line) {
            '^enabled=([01])$' { $script:Enabled = $Matches[1] -eq '1' }
            '^wait_for_browser=([01])$' { $script:WaitForBrowser = $Matches[1] -eq '1' }
        }
    }
}

function Test-BrowserOpen {
    $userData = Kv-Get $EnvFile 'SCS_PROFILE'
    if (-not $userData) { return $false }
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue
    return [bool]($procs | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($userData) })
}

function Wait-Healthy {
    $cid = Container
    if (-not $cid) { return $false }
    $waited = 0
    while ($waited -lt $HealthTimeout) {
        $st = docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}}' $cid 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        if ($st -like 'running healthy *') { return $true }
        if ($st -like '* unhealthy *' -or $st -like 'exited*' -or $st -like 'dead*') { return $false }
        if ($st -like 'running none *' -and $waited -ge 30) { return $true }
        if (($st -split ' ')[-1] -ne '0') { return $false }
        Start-Sleep 5; $waited += 5
    }
    return $false
}

# Copies /app/dist out of an image into a new folder; returns its path.
function Extract($image) {
    $stage = Join-Path $Dir (".stage.{0}" -f [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $cid = docker create $image 2>$null
    if ($LASTEXITCODE -eq 0) {
        docker cp "${cid}:/app/dist/." $stage *>> $LogFile
        docker rm $cid *> $null
    }
    if (-not (Test-Path (Join-Path $stage 'compose.yaml'))) { Remove-Item -Recurse -Force $stage; return $null }
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $stage 'scs-update.ps1'), [ref]$null, [ref]$errors)
    if ($errors) { Remove-Item -Recurse -Force $stage; return $null }
    return $stage
}

function Apply($target) {
    $previous = Kv-Get $EnvFile 'SCS_IMAGE'
    Log "applying $target (was $previous)"
    $stage = Extract $target
    if (-not $stage) { Write-Status 'error' "Couldn't read the files out of release $($script:Available)."; return }
    # compose.yaml and the overrides .env's COMPOSE_FILE may add to it
    # (compose.nvidia.yaml) go in together, and back.
    Get-ChildItem -Force -File $stage -Filter 'compose*.yaml' | ForEach-Object {
        if (Test-Path $_.Name) { Copy-Item -Force $_.Name ".$($_.Name).previous" }
        Move-Item -Force $_.FullName (Join-Path $Dir $_.Name)
    }
    Kv-Set $EnvFile 'SCS_IMAGE' $target
    docker compose up -d *>> $LogFile
    if ($LASTEXITCODE -eq 0 -and (Wait-Healthy)) {
        Get-ChildItem -Force $stage | ForEach-Object { Move-Item -Force $_.FullName (Join-Path $Dir $_.Name) }
        Remove-Item -Recurse -Force $stage, .compose*.yaml.previous -ErrorAction SilentlyContinue
        if ($previous -ne $target) { Unkeep $previous }
        $bad = Kv-Get $State 'BAD'
        if ($bad -ne $target) { Unkeep $bad }
        Kv-Set $State 'BAD' ''
        Write-Status 'updated' "Updated to $($script:Available)."
        return
    }
    Log "release $($script:Available) didn't become healthy; its last log lines:"
    docker compose logs --no-color --tail 40 $Service *>> $LogFile
    Get-ChildItem -Force -File -Filter '.compose*.yaml.previous' | ForEach-Object {
        Move-Item -Force $_.FullName (Join-Path $Dir ($_.Name.Substring(1) -replace '\.previous$', ''))
    }
    Kv-Set $EnvFile 'SCS_IMAGE' $previous
    docker compose up -d *>> $LogFile
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    Kv-Set $State 'BAD' $target
    Write-Status 'failed' ("Release $($script:Available) didn't start, so the proxy is back on $(Version-Of $previous). " +
                           "It will be skipped until a newer one is out; details in $LogFile.")
}

function Proxy-State { docker inspect -f '{{.State.Status}}' (Container) 2>$null }

# Pulls the channel, retrying while the network comes up; returns the
# release to install, or nothing.
function Check {
    $channel = Kv-Get $EnvFile 'SCS_CHANNEL'
    if (-not $channel) { Write-Status 'error' "SCS_CHANNEL isn't set in $EnvFile."; return }
    $repo = $channel.Substring(0, $channel.LastIndexOf(':'))
    for ($try = 1; ; $try++) {
        docker pull -q $channel *>> $LogFile
        if ($LASTEXITCODE -eq 0) { break }
        if ($try -ge $PullTries) {
            Write-Status 'error' "Couldn't download $channel (offline, or Docker Hub unreachable); trying again at the next sign-in."
            return
        }
        Start-Sleep $Poll
    }
    $latest = docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' $channel 2>$null |
        Where-Object { $_.StartsWith("$repo@sha256:") } | Select-Object -First 1
    if (-not $latest) { Write-Status 'error' "Downloaded $channel but found no digest for it."; return }
    Keep $latest
    $script:Available = Version-Of $latest
    if ($latest -eq (Kv-Get $EnvFile 'SCS_IMAGE')) {
        $script:Available = ''
        Write-Status 'up_to_date' 'Running the latest release.'
    } elseif ($latest -eq (Kv-Get $State 'BAD')) {
        Write-Status 'skipped' "Release $($script:Available) failed to start here earlier; waiting for a newer one."
    } else {
        return $latest
    }
}

# One run at a time: the lock file stays open while this runs, so one left
# by a run cut off at shutdown doesn't block the next.
$lock = Join-Path $Dir '.update.lock'
try { $lockStream = [System.IO.File]::Open($lock, 'OpenOrCreate', 'Write', 'None') } catch { exit 0 }
try {
    # Docker Desktop starts at sign-in too, and then restarts the proxy. A
    # proxy still stopped after two minutes was stopped by you, and is left alone.
    $waited = 0
    while ($true) {
        docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            $st = Proxy-State
            if ($st -in @('running', 'restarting') -or ($st -and $waited -ge 120)) { break }
        }
        if ($waited -ge $StartupWait) {
            Write-Status 'error' "Docker or the proxy didn't start within $($StartupWait / 60) minutes; checking again at the next sign-in."
            exit 1
        }
        Start-Sleep 10; $waited += 10
    }
    if ($st -notin @('running', 'restarting') -and -not $Now) { Log "the proxy isn't running ($st); nothing to do"; exit 0 }

    Read-Policy
    if (-not $script:Enabled -and -not $Now) { Write-Status 'disabled' 'Automatic updates are off.'; exit 0 }
    $target = Check
    if (-not $target) { exit 0 }

    if (-not $Now -and $script:WaitForBrowser -and (Test-BrowserOpen)) {
        Write-Status 'pending' "Release $($script:Available) is downloaded and switches in once the Sissy Browser is closed."
        while (Test-BrowserOpen) { Start-Sleep $Poll }
        # Updates may have been turned off on the panel meanwhile.
        Read-Policy
        if (-not $script:Enabled) { Write-Status 'disabled' "Automatic updates are off; release $($script:Available) wasn't installed."; exit 0 }
    }
    Apply $target
} finally {
    $lockStream.Dispose()
    Get-ChildItem -Force -Directory -Filter '.stage.*' $Dir -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
}
