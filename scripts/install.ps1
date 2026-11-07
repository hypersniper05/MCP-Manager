# MCP-Manager installer for Windows. Run it through install.bat, which takes the same options.
# On Linux and macOS, run install.sh.
#
#   install.bat                 asks who can reach it and whether to use HTTPS, then starts it
#   install.bat --yes           no questions: this computer only, plain HTTP
#   install.bat --network       other devices on your network can reach it too
#   install.bat --https         HTTPS, with a certificate made for this computer
#   install.bat --http          plain HTTP (to go back from --https)
#   install.bat --name mcp.lan  a host name the certificate must cover (repeat for more)
#   install.bat --port 9000     another port (default 8899)
#   install.bat --no-trust      leave this computer's trust store alone
#   install.bat --trust FILE    only trust a certificate: for another device that connects
#
# Safe to run again: it keeps your admin token, your data and a certificate that still fits.

# Continue, not Stop: in Windows PowerShell a native command writing to stderr would count as a
# failure under Stop, and docker reports progress there. Exit codes are checked instead.
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path -Parent $PSScriptRoot)

$rootPem = 'certs\mcp-manager-root.pem'
$network = ''; $https = ''; $trust = $true; $yes = $false; $port = ''; $extraNames = @(); $trustOnly = ''
# no BOM, and LF: docker compose reads .env, and a BOM breaks its first line
$utf8 = New-Object System.Text.UTF8Encoding $false

function Say($text = '') { Write-Host $text }
function Die($text) { [Console]::Error.WriteLine("Stopped: $text"); exit 1 }

$argv = @($args)
for ($i = 0; $i -lt $argv.Count; $i++) {
    switch -Exact ($argv[$i]) {
        { $_ -in '-y', '--yes' } { $yes = $true; break }
        '--network' { $network = 'yes'; break }
        '--https' { $https = 'yes'; break }
        '--http' { $https = 'no'; break }
        '--no-trust' { $trust = $false; break }
        '--name' { $i++; if ($i -ge $argv.Count) { Die '--name needs a host name' }; $extraNames += $argv[$i]; break }
        '--port' { $i++; if ($i -ge $argv.Count) { Die '--port needs a number' }; $port = $argv[$i]; break }
        '--trust' { $i++; if ($i -ge $argv.Count) { Die '--trust needs a file' }; $trustOnly = $argv[$i]; break }
        { $_ -in '-h', '--help' } {
            Get-Content $PSCommandPath -TotalCount 14 | ForEach-Object { $_ -replace '^# ?', '' }
            exit 0
        }
        default { Die "unknown option $($argv[$i]) (see --help)" }
    }
}

# The answer, or the default when there is nobody to ask
function Ask($question, $default) {
    if ($yes -or [Console]::IsInputRedirected) { return $default }
    $choices = if ($default) { '[Y/n]' } else { '[y/N]' }
    $reply = Read-Host "$question $choices"
    if ($reply -match '^[Yy]') { return $true }
    if ($reply -match '^[Nn]') { return $false }
    return $default
}

function Read-Env { [IO.File]::ReadAllLines((Join-Path (Get-Location) '.env')) }
function Write-Env($lines) {
    [IO.File]::WriteAllText((Join-Path (Get-Location) '.env'), (($lines -join "`n") + "`n"), $utf8)
}
function Env-Value($key) {
    $found = Read-Env | Where-Object { $_ -like "$key=*" } | Select-Object -Last 1
    if ($found) { return $found.Substring($key.Length + 1) }
    return ''
}
# Replaces the line, or the commented-out example of it, or adds one
function Set-EnvValue($key, $value) {
    $done = $false
    $out = foreach ($line in (Read-Env)) {
        if ($line -match "^#? *$key=") {
            if (-not $done) { "$key=$value"; $done = $true }
        } else { $line }
    }
    if (-not $done) { $out = @($out) + "$key=$value" }
    Write-Env $out
}
function Drop-EnvValue($key) {
    Write-Env (Read-Env | ForEach-Object { if ($_ -like "$key=*") { "# $_" } else { $_ } })
}

# The names and addresses other devices will use; localhost is always covered
function Cert-Names {
    $names = @($extraNames)
    if ($network -eq 'yes') {
        $machine = $env:COMPUTERNAME.ToLower()
        $names += $machine, "$machine.local"
        $names += Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } |
            ForEach-Object { $_.IPAddress }
    }
    return $names | Select-Object -Unique
}

function Trust-File($file) {
    $path = (Resolve-Path $file).Path
    try {
        # the current user's store: no administrator needed, and Windows asks you to confirm
        $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $path
        $store = New-Object Security.Cryptography.X509Certificates.X509Store 'Root', 'CurrentUser'
        $store.Open('ReadWrite')
        try { $store.Add($cert) } finally { $store.Close() }
    } catch {
        Say "Could not add it to this computer's trust store ($($_.Exception.Message))."
        Say "Apps will refuse the connection until you trust $path by hand: see `"HTTPS on your own network`" in the README."
        return
    }
    Say "This computer now trusts $path."
    # Node apps, Claude Code among them, read their own list of roots, not the system's
    $have = [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')
    if ($have -and $have -ne $path) {
        Say "NODE_EXTRA_CA_CERTS is already set to $have, so it stays. Node apps need it to name $path."
    } else {
        [Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $path, 'User')
        Say 'Node apps will trust it too, once they are started again.'
    }
}

if ($trustOnly) {
    if (-not (Test-Path $trustOnly)) { Die "no such file: $trustOnly" }
    Trust-File $trustOnly
    exit 0
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Die 'Docker is not installed. Get Docker Desktop from https://docs.docker.com/get-docker/, or see "Without Docker" in the README.'
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Die 'Docker Compose is missing: update Docker Desktop.' }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Die 'Docker is not running. Start Docker Desktop and run this again.' }

if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$token = Env-Value 'MCPM_ADMIN_TOKEN'
$newToken = $false
if (-not $token) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = 'mcpm_admin_' + ([Convert]::ToBase64String($bytes) -replace '\+', '-' -replace '/', '_' -replace '=', '')
    Set-EnvValue 'MCPM_ADMIN_TOKEN' $token
    $newToken = $true
}

if (-not $network) {
    $network = if (Ask 'Should other devices on your network reach it? Only this computer is the safest.' $false) { 'yes' } else { 'no' }
}
if (-not $https) {
    if ($network -eq 'yes') {
        $https = if (Ask 'Use HTTPS? Recommended: app keys then cross your network encrypted.' $true) { 'yes' } else { 'no' }
    } else {
        $https = if (Ask 'Use HTTPS? Not needed on this computer alone, and some apps refuse a private certificate.' $false) { 'yes' } else { 'no' }
    }
}

Set-EnvValue 'MCPM_PUBLISH' $(if ($network -eq 'yes') { '0.0.0.0' } else { '127.0.0.1' })
if ($port) { Set-EnvValue 'MCPM_PORT' $port }
$port = Env-Value 'MCPM_PORT'
if (-not $port) { $port = '8899' }

docker compose build
if ($LASTEXITCODE -ne 0) { Die 'the image did not build: see the lines above.' }

$scheme = 'http'
if ($https -eq 'yes') {
    $scheme = 'https'
    New-Item -ItemType Directory -Force certs | Out-Null
    $names = @(Cert-Names)
    $printed = (docker compose run --rm -T mcp-manager python -m mcp_manager.tls --dir /data/tls @names) -join "`n"
    $pem = [regex]::Match($printed, '(?s)-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----').Value
    if (-not $pem) { Die 'the certificate was not made: see the lines above.' }
    [IO.File]::WriteAllText((Join-Path (Get-Location) $rootPem), $pem + "`n", $utf8)
    Set-EnvValue 'MCPM_TLS_CERT' '/data/tls/tls-cert.pem'
    Set-EnvValue 'MCPM_TLS_KEY' '/data/tls/tls-key.pem'
} elseif ((Env-Value 'MCPM_TLS_CERT') -like '/data/tls/*') {
    # a certificate of your own, set by hand, is yours to remove
    Drop-EnvValue 'MCPM_TLS_CERT'
    Drop-EnvValue 'MCPM_TLS_KEY'
}

# Recreated even if no setting changed: the running manager holds the old certificate in memory.
# Three tries, because Docker can take a moment to let go of a port it was just publishing.
foreach ($attempt in 1..3) {
    docker compose up -d --force-recreate
    if ($LASTEXITCODE -eq 0) { break }
    if ($attempt -lt 3) { Start-Sleep -Seconds 3 }
}
if ($LASTEXITCODE -ne 0) { Die 'it did not start: see the lines above.' }

$tries = 0
do {
    Start-Sleep -Seconds 1
    docker compose exec -T mcp-manager python -c "import socket; socket.create_connection(('127.0.0.1', 8899), 2)" *> $null
    $tries++
    if ($tries -ge 60) { Die 'it did not start within a minute: docker compose logs mcp-manager' }
} while ($LASTEXITCODE -ne 0)

if ($https -eq 'yes' -and $trust) {
    Say
    Say "To connect without warnings, this computer has to trust $rootPem."
    Say 'Its private key is gone, so it can vouch for this manager and nothing else.'
    if (Ask 'Trust it now? Windows will ask you to confirm.' $true) { Trust-File $rootPem }
}

Say
Say 'MCP-Manager is running.'
Say "  Admin panel:   ${scheme}://localhost:$port/"
Say "  MCP endpoint:  ${scheme}://localhost:$port/mcp"
if ($newToken) {
    Say "  Admin token:   $token"
    Say '                 (kept in .env; this is the only time it is shown)'
} else {
    Say '  Admin token:   the one in .env'
}
if ($network -eq 'yes') {
    Say '  Reachable from your network too.'
} else {
    Say '  Reachable from this computer only. To change that: install.bat --network'
}
if ($https -eq 'yes') { Say '  Other devices need the certificate: see "HTTPS on your own network" in the README.' }
Say
Say 'Next: open the admin panel, add your MCP servers, and create a key for each app.'
