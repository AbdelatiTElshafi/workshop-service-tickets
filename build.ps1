# Builds dist\install.exe: compiles the service host and the installer with the C# compiler that ships
# with Windows (.NET Framework 4.x), and embeds the app files inside the installer.
# Usage: powershell -ExecutionPolicy Bypass -File build.ps1
param([string]$OutputName = "install.exe")

$ErrorActionPreference = "Stop"
if ([IO.Path]::GetFileName($OutputName) -ne $OutputName -or [IO.Path]::GetExtension($OutputName) -ne ".exe") {
    throw "OutputName must be an .exe file name."
}
$Root = $PSScriptRoot
$Csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$Dist = Join-Path $Root "dist"
$Stage = Join-Path $Dist "payload"
Remove-Item -Recurse -Force $Dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Stage, (Join-Path $Stage "static") | Out-Null

function Compile($out, $sources, $refs, $extra) {
    & $Csc /nologo /optimize+ /codepage:65001 /platform:anycpu /out:$out ($refs | ForEach-Object { "/r:$_" }) $extra $sources
    if ($LASTEXITCODE -ne 0) { throw "compile failed: $out" }
}

Compile (Join-Path $Stage "RTWorkshopService.exe") (Join-Path $Root "installer\Service.cs") @("System.ServiceProcess.dll") @("/target:exe", "/win32icon:$(Join-Path $Root 'static\rt.ico')")
Copy-Item (Join-Path $Root "server.py"), (Join-Path $Root "setup.ps1"), (Join-Path $Root "README.md") $Stage
Copy-Item (Join-Path $Root "static\*") (Join-Path $Stage "static")

$zip = Join-Path $Dist "payload.zip"
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($Stage, $zip)

Compile (Join-Path $Dist $OutputName) (Join-Path $Root "installer\Installer.cs") `
    @("System.Windows.Forms.dll", "System.Drawing.dll", "System.ServiceProcess.dll", "System.IO.Compression.dll", "System.IO.Compression.FileSystem.dll", "System.Core.dll") `
    @("/target:winexe", "/win32icon:$(Join-Path $Root 'static\rt.ico')", "/win32manifest:$(Join-Path $Root 'installer\app.manifest')",
      "/resource:$zip,payload.zip", "/resource:$(Join-Path $Root 'static\rt-logo.jpg'),logo.jpg")

Remove-Item -Recurse -Force $Stage, $zip
$exe = Get-Item (Join-Path $Dist $OutputName)
Write-Host ("built {0} ({1:N0} KB)" -f $exe.FullName, ($exe.Length / 1KB)) -ForegroundColor Green
