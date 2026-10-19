<#
Sissy Censor System updater for Windows (Linux and macOS: scs-update).

Lives in the install folder (%USERPROFILE%\scs) and runs hourly from the
scheduled task the installer adds. Each run does what scs-update does:
reads updates.* through the container, pulls SCS_CHANNEL at most every
updates.check_hours, and while the Sissy Browser is closed recreates the
container on a new release, keeping it only once it reports healthy. The
result goes to logs\update.json (shown in the admin panel) and a history to
update.log. Nothing read from the container or the folders it can write
(config, logs, mitmproxy) is evaluated.

  powershell -ExecutionPolicy Bypass -File scs-update.ps1          what the task runs
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

$script:Enabled = $true; $script:CheckSeconds = 86400; $script:WaitForBrowser = $true
function Read-Policy {
    $out = docker compose exec -T $Service python -m censor_proxy.updates policy 2>$null
    if ($LASTEXITCODE -ne 0) { $out = docker compose run --rm -T --no-deps $Service python -m censor_proxy.updates policy 2>$null }
    if ($LASTEXITCODE -ne 0) { Log "couldn't read updates.* from config.yaml; using the defaults"; return }
    foreach ($line in @($out)) {
        switch -Regex ($line) {
            '^enabled=([01])$' { $script:Enabled = $Matches[1] -eq '1' }
            '^wait_for_browser=([01])$' { $script:WaitForBrowser = $Matches[1] -eq '1' }
            '^check_hours=([0-9]+(\.[0-9]+)?)$' { $script:CheckSeconds = [int]([double]$Matches[1] * 3600) }
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
    Copy-Item -Force compose.yaml .compose.yaml.previous
    Move-Item -Force (Join-Path $stage 'compose.yaml') compose.yaml
    Kv-Set $EnvFile 'SCS_IMAGE' $target
    docker compose up -d *>> $LogFile
    if ($LASTEXITCODE -eq 0 -and (Wait-Healthy)) {
        Get-ChildItem -Force $stage | ForEach-Object { Move-Item -Force $_.FullName (Join-Path $Dir $_.Name) }
        Remove-Item -Recurse -Force $stage, .compose.yaml.previous -ErrorAction SilentlyContinue
        Kv-Set $State 'PENDING' ''
        if ($previous -and $previous -ne $target) { docker image rm $previous *> $null }
        $bad = Kv-Get $State 'BAD'
        if ($bad -and $bad -ne $target) { docker image rm $bad *> $null }
        Kv-Set $State 'BAD' ''
        Write-Status 'updated' "Updated to $($script:Available)."
        return
    }
    Log "release $($script:Available) didn't become healthy; its last log lines:"
    docker compose logs --no-color --tail 40 $Service *>> $LogFile
    Move-Item -Force .compose.yaml.previous compose.yaml
    Kv-Set $EnvFile 'SCS_IMAGE' $previous
    docker compose up -d *>> $LogFile
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    Kv-Set $State 'PENDING' ''
    Kv-Set $State 'BAD' $target
    Write-Status 'failed' ("Release $($script:Available) didn't start, so the proxy is back on $(Version-Of $previous). " +
                           "It will be skipped until a newer one is out; details in $LogFile.")
}

function Check {
    $channel = Kv-Get $EnvFile 'SCS_CHANNEL'
    if (-not $channel) { Write-Status 'error' "SCS_CHANNEL isn't set in $EnvFile."; return $false }
    $repo = $channel.Substring(0, $channel.LastIndexOf(':'))
    docker pull -q $channel *>> $LogFile
    if ($LASTEXITCODE -ne 0) { Write-Status 'error' "Couldn't download $channel (offline, or Docker Hub unreachable)."; return $false }
    Kv-Set $State 'LAST_CHECK' ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    $latest = docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' $channel 2>$null |
        Where-Object { $_.StartsWith("$repo@sha256:") } | Select-Object -First 1
    if (-not $latest) { Write-Status 'error' "Downloaded $channel but found no digest for it."; return $false }
    $script:Available = Version-Of $latest
    if ($latest -eq (Kv-Get $EnvFile 'SCS_IMAGE')) {
        Kv-Set $State 'PENDING' ''
        $script:Available = ''
        Write-Status 'up_to_date' 'Running the latest release.'
    } elseif ($latest -eq (Kv-Get $State 'BAD')) {
        Kv-Set $State 'PENDING' ''
        Write-Status 'skipped' "Release $($script:Available) failed to start here earlier; waiting for a newer one."
    } else {
        Kv-Set $State 'PENDING' $latest
    }
    return $true
}

# One run at a time; a lock left by a crash expires after two hours.
$lock = Join-Path $Dir '.update.lock'
if ((Test-Path $lock) -and ((Get-Item $lock).LastWriteTime -lt (Get-Date).AddHours(-2))) { Remove-Item -Force $lock }
try { $lockStream = [System.IO.File]::Open($lock, 'CreateNew', 'Write', 'None') } catch { exit 0 }
try {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { Write-Status 'error' "Docker isn't running or can't be reached."; exit 1 }
    $st = docker inspect -f '{{.State.Status}}' (Container) 2>$null
    if ($st -notin @('running', 'restarting') -and -not $Now) { Log "the proxy isn't running ($st); nothing to do"; exit 0 }

    Read-Policy
    if (-not $script:Enabled -and -not $Now) { Write-Status 'disabled' 'Automatic updates are off.'; exit 0 }

    $last = Kv-Get $State 'LAST_CHECK'
    $nowTs = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ($Now -or $last -notmatch '^[0-9]+$' -or ($nowTs - [long]$last) -ge $script:CheckSeconds) {
        if (-not (Check)) { exit 1 }
    }
    $pending = Kv-Get $State 'PENDING'
    if (-not $pending) { exit 0 }
    $script:Available = Version-Of $pending
    if (-not $Now -and $script:WaitForBrowser -and (Test-BrowserOpen)) {
        Write-Status 'pending' "Release $($script:Available) is downloaded and switches in once the Sissy Browser is closed."
        exit 0
    }
    Apply $pending
} finally {
    $lockStream.Dispose()
    Remove-Item -Force $lock -ErrorAction SilentlyContinue
    Get-ChildItem -Force -Directory -Filter '.stage.*' $Dir -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
}
