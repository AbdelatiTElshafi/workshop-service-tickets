# Prepares everything the app needs inside this folder:
#   tools\python  - Python (embeddable) + psycopg
#   tools\pgsql   - PostgreSQL binaries
#   data\pgdata   - the database (data\config.json holds its random password and the ports)
# Modes:
#   dev       (default) start PostgreSQL for start.bat; used while developing
#   install   called by install.exe (elevated): services RTWorkshopDB + RTWorkshop, first admin, firewall
#   uninstall called by the uninstaller: removes the services and the firewall rule
# Safe to run again: every step is skipped when it is already done.
param([string]$Mode = "dev", [int]$AppPort = 0, [switch]$AllowLan)

# native tools write warnings to stderr; failures are detected through $LASTEXITCODE instead
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root   = $PSScriptRoot
$Tools  = Join-Path $Root "tools"
$Py     = Join-Path $Tools "python"
$PyExe  = Join-Path $Py "python.exe"
$Bin    = Join-Path $Tools "pgsql\bin"
$Data   = Join-Path $Root "data"
$PgData = Join-Path $Data "pgdata"
$Config = Join-Path $Data "config.json"
$DbService  = "RTWorkshopDB"
$AppService = "RTWorkshop"
$FwRule     = "RT Workshop"
$PyVer  = "3.13.7"
$PyZipUrl = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$PgZipUrl = "https://get.enterprisedb.com/postgresql/postgresql-17.6-1-windows-x64-binaries.zip"
$NetworkService = "*S-1-5-20"   # by SID, so it works on Arabic/English Windows alike

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$Quiet = [bool]$env:RT_QUIET   # start.bat: no progress text, errors in a popup
# progress lines are "##<percent>|<message>" so the installer window can show them
function Step($pct, $msg) { if (-not $Quiet) { Write-Output "##$pct|$msg" } }
trap {
    $msg = $_.Exception.Message
    if ($Quiet) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show("مشكلة في تشغيل قاعدة البيانات:`n`n$msg", "RT Workshop", "OK", "Error", "Button1", "RtlReading") | Out-Null
    }
    Write-Output "##ERR|$msg"
    exit 1
}
function Download($url, $out) {
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest $url -OutFile $out -UseBasicParsing -ErrorAction Stop; return }
        catch { if ($i -eq 3) { throw "التنزيل فشل — اتأكد إن النت شغال وجرّب تاني. ($($_.Exception.Message))" }; Start-Sleep 3 }
    }
}
function Port-Busy($port) { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }
function Read-Config { Get-Content $Config -Raw | ConvertFrom-Json }
function Save-Config($cfg) { $cfg | ConvertTo-Json | Out-File -Encoding ascii $Config }
function Set-Prop($obj, $name, $value) { $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force }

# ---------------------------------------------------------------- uninstall
if ($Mode -eq "uninstall") {
    foreach ($svc in $AppService, $DbService) {
        if (Get-Service $svc -ErrorAction SilentlyContinue) {
            Stop-Service $svc -Force -ErrorAction SilentlyContinue
            sc.exe delete $svc | Out-Null
        }
    }
    Remove-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue
    Write-Output "##100|اتشال"
    exit 0
}

New-Item -ItemType Directory -Force $Tools, $Data, (Join-Path $Data "logs") | Out-Null

# ---------------------------------------------------------------- Python (embeddable, private to this app)
if (-not (Test-Path $PyExe)) {
    Step 5 "بنزّل مكونات البرنامج (1 من 3)..."
    $zip = Join-Path $Tools "python.zip"
    Download $PyZipUrl $zip
    Expand-Archive $zip -DestinationPath $Py -Force -ErrorAction Stop
    Remove-Item $zip
    # enable site-packages so pip and psycopg work in the embeddable build
    $pth = Get-ChildItem $Py -Filter "python*._pth" | Select-Object -First 1
    (Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pth.FullName -Encoding ascii
}
cmd /c "`"$PyExe`" -c `"import pip`" >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Step 20 "بجهّز مكونات البرنامج..."
    $gp = Join-Path $Tools "get-pip.py"
    Download $GetPipUrl $gp
    & $PyExe $gp --no-warn-script-location --disable-pip-version-check 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    Remove-Item $gp
    if ($code -ne 0) { throw "تجهيز المكونات فشل (pip)" }
}
cmd /c "`"$PyExe`" -c `"import psycopg`" >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Step 30 "بنزّل مكونات البرنامج (2 من 3)..."
    & $PyExe -m pip install --disable-pip-version-check --no-warn-script-location "psycopg[binary]" 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "تنزيل المكونات فشل (psycopg)" }
}

# ---------------------------------------------------------------- PostgreSQL binaries
if (-not (Test-Path (Join-Path $Bin "pg_ctl.exe"))) {
    $zip = Join-Path $Tools "postgresql.zip"
    if (-not (Test-Path $zip)) {
        Step 40 "بنزّل مكونات البرنامج (3 من 3) — ده أكبر جزء وممكن ياخد وقت..."
        Download $PgZipUrl $zip
    }
    Step 70 "بفك ضغط الملفات (ممكن ياخد كام دقيقة)..."
    # Windows tar copes with the long paths in this zip; pgAdmin, docs and debug symbols are not needed
    tar.exe -xf $zip -C $Tools --exclude "pgsql/pgAdmin 4" --exclude "pgsql/symbols" --exclude "pgsql/doc" --exclude "pgsql/StackBuilder"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $Bin "pg_ctl.exe"))) { throw "فك ضغط الملفات فشل" }
    Remove-Item $zip
}

# ---------------------------------------------------------------- database cluster
if (-not (Test-Path (Join-Path $PgData "PG_VERSION"))) {
    Step 88 "بجهّز قاعدة البيانات..."
    $dbPort = if ($env:RT_PG_PORT) { [int]$env:RT_PG_PORT } else { 5440 }
    while (Port-Busy $dbPort) { $dbPort++ }
    $pw = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
    $pwFile = Join-Path $Data "pw.tmp"
    [IO.File]::WriteAllText($pwFile, $pw)
    & (Join-Path $Bin "initdb.exe") -D $PgData -U postgres --pwfile=$pwFile -E UTF8 --locale=C -A scram-sha-256 2>&1 | Out-Null
    $code = $LASTEXITCODE
    Remove-Item $pwFile
    if ($code -ne 0) { throw "تجهيز قاعدة البيانات فشل (initdb)" }
    @{ db_host = "127.0.0.1"; db_port = $dbPort; db_user = "postgres"; db_password = $pw; db_name = "workshop" } |
        ConvertTo-Json | Out-File -Encoding ascii $Config
}
$cfg = Read-Config
$env:PGPASSWORD = $cfg.db_password
$pgOpts = "-p $($cfg.db_port) -c listen_addresses=127.0.0.1"

# ---------------------------------------------------------------- start the database
Step 92 "بشغّل قاعدة البيانات..."
if ($Mode -eq "install") {
    if (-not (Get-Service $DbService -ErrorAction SilentlyContinue)) {
        # a copy started by start.bat (dev mode) must stop before the service takes over
        cmd /c "`"$(Join-Path $Bin 'pg_ctl.exe')`" stop -D `"$PgData`" -m fast >nul 2>&1"
        icacls $Data /grant "${NetworkService}:(OI)(CI)M" /T /C /Q | Out-Null
        & (Join-Path $Bin "pg_ctl.exe") register -N $DbService -D $PgData -S auto -o $pgOpts 2>&1 | ForEach-Object { "$_" }
        if ($LASTEXITCODE -ne 0) { throw "تسجيل خدمة قاعدة البيانات فشل" }
        sc.exe description $DbService "RT Workshop - database" | Out-Null
        sc.exe failure $DbService reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
    }
    Start-Service $DbService -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 30 -and -not (Port-Busy $cfg.db_port); $i++) { Start-Sleep 1 }
    if (-not (Port-Busy $cfg.db_port)) { throw "قاعدة البيانات ماقامتش" }
} else {
    & (Join-Path $Bin "pg_ctl.exe") status -D $PgData | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # through cmd with output to nul: a PowerShell pipe here would wait forever on the postgres child process
        cmd /c "`"$(Join-Path $Bin 'pg_ctl.exe')`" start -D `"$PgData`" -w -l `"$(Join-Path $Data 'postgres.log')`" -o `"$pgOpts`" >nul 2>&1"
        if ($LASTEXITCODE -ne 0) { throw "قاعدة البيانات ماقامتش - شوف data\postgres.log" }
    }
}
$exists = & (Join-Path $Bin "psql.exe") -h 127.0.0.1 -p $cfg.db_port -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$($cfg.db_name)'"
if ("$exists".Trim() -ne "1") {
    & (Join-Path $Bin "createdb.exe") -h 127.0.0.1 -p $cfg.db_port -U postgres $cfg.db_name
    if ($LASTEXITCODE -ne 0) { throw "إنشاء قاعدة البيانات فشل" }
}
if ($Mode -ne "install") { Step 100 "قاعدة البيانات جاهزة."; exit 0 }

# ---------------------------------------------------------------- install: settings, first admin, app service, firewall
if ($AppPort -gt 0) { Set-Prop $cfg "app_port" $AppPort }
Set-Prop $cfg "app_host" $(if ($AllowLan) { "0.0.0.0" } else { "127.0.0.1" })
Save-Config $cfg
$port = [int]$cfg.app_port

if (Get-Service $AppService -ErrorAction SilentlyContinue) { Stop-Service $AppService -Force -ErrorAction SilentlyContinue }

Step 95 "بعمل حساب مدير النظام..."
$out = & $PyExe (Join-Path $Root "server.py") --init 2>&1 | ForEach-Object { "$_" }
$code = $LASTEXITCODE
$out
if ($code -ne 0) { throw "تجهيز البرنامج فشل: $($out | Select-Object -Last 1)" }

Step 97 "بسجّل البرنامج عشان يشتغل مع الجهاز..."
$svcExe = Join-Path $Root "RTWorkshopService.exe"
if (-not (Get-Service $AppService -ErrorAction SilentlyContinue)) {
    sc.exe create $AppService binPath= "`"$svcExe`"" start= auto depend= $DbService DisplayName= "RT Workshop" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "تسجيل خدمة البرنامج فشل" }
    sc.exe description $AppService "RT Workshop - service tickets" | Out-Null
    sc.exe failure $AppService reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
}
Remove-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue
if ($AllowLan) {
    New-NetFirewallRule -DisplayName $FwRule -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private,Domain | Out-Null
}
Start-Service $AppService -ErrorAction SilentlyContinue

Step 99 "بتأكد إن البرنامج شغال..."
$ok = $false
for ($i = 0; $i -lt 40 -and -not $ok; $i++) {
    try { $ok = (Invoke-WebRequest "http://127.0.0.1:$port/api/company" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { Start-Sleep 1 }
}
if (-not $ok) { throw "البرنامج ماقامش على بورت $port — شوف data\logs\server.log" }
Step 100 "خلاص!"
