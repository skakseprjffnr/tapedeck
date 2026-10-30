<#
.SYNOPSIS
    Builds Tapedeck for Windows.

.DESCRIPTION
    Makes Tapedeck.exe with mpv, yt-dlp and Deno bundled, then packs it two ways:
        windows\out\Tapedeck-<version>-windows-setup.exe     (installer, no admin needed)
        windows\out\Tapedeck-<version>-windows-portable.zip  (unzip and run)

    Run it on Windows from the project folder:
        powershell -ExecutionPolicy Bypass -File windows\build.ps1

    Needs Python 3.11 or newer and an internet connection. The installer also
    needs Inno Setup 6 (https://jrsoftware.org/isdl.php); without it you still
    get the portable zip. GitHub Actions runs this same script (see
    .github\workflows\windows.yml).
#>
param([string]$Version = "")

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # downloads are much faster without the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
$Work = Join-Path $Root "build\windows"
$Out = Join-Path $PSScriptRoot "out"
$App = Join-Path $Work "dist\Tapedeck"
$Tools = Join-Path $App "tools"

function Step([string]$Text) {
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe exited with code $LASTEXITCODE"
    }
}

function Get-File([string]$Url, [string]$Path) {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing -Headers @{ "User-Agent" = "tapedeck-build" }
            return
        } catch {
            if ($attempt -eq 3) {
                throw "Couldn't download $Url ($_)"
            }
            Start-Sleep -Seconds (5 * $attempt)
        }
    }
}

if (-not $Version) {
    $match = Select-String -Path (Join-Path $Root "tapedeck.py") -Pattern '^VERSION = "([^"]+)"'
    if (-not $match) {
        throw "Couldn't read VERSION from tapedeck.py"
    }
    $Version = $match.Matches[0].Groups[1].Value
}
Step "Building Tapedeck $Version"
if (Test-Path $Work) {
    Remove-Item -Recurse -Force $Work
}
New-Item -ItemType Directory -Force -Path $Work, $Out | Out-Null

Step "Setting up Python"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python 3.11 or newer is needed: https://www.python.org/downloads/windows/"
}
Invoke-Native $python.Source @("-m", "venv", (Join-Path $Work "venv"))
$py = Join-Path $Work "venv\Scripts\python.exe"
Invoke-Native $py @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Invoke-Native $py @("-m", "pip", "install", "--quiet", "pyside6-essentials", "pyinstaller")

Step "Freezing Tapedeck.exe"
Invoke-Native $py @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
    "--name", "Tapedeck",
    "--icon", (Join-Path $PSScriptRoot "tapedeck.ico"),
    "--distpath", (Join-Path $Work "dist"),
    "--workpath", (Join-Path $Work "pyinstaller"),
    "--specpath", $Work,
    "--exclude-module", "tkinter",
    (Join-Path $Root "tapedeck.py")
)

Step "Bundling yt-dlp, Deno and mpv"
New-Item -ItemType Directory -Force -Path $Tools | Out-Null
Get-File "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" (Join-Path $Tools "yt-dlp.exe")

$denoZip = Join-Path $Work "deno.zip"
Get-File "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" $denoZip
Expand-Archive -Path $denoZip -DestinationPath $Tools -Force

$headers = @{ "User-Agent" = "tapedeck-build"; "Accept" = "application/vnd.github+json" }
if ($env:GITHUB_TOKEN) {
    $headers["Authorization"] = "Bearer $($env:GITHUB_TOKEN)"
}
$release = Invoke-RestMethod -Uri "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest" -Headers $headers
# Plain x86_64 runs on every 64-bit PC; the "-v3" builds need newer CPUs.
$asset = $release.assets | Where-Object { $_.name -match '^mpv-x86_64-\d{8}.*\.7z$' } | Select-Object -First 1
if (-not $asset) {
    $asset = $release.assets | Where-Object { $_.name -match '^mpv-x86_64.*\.7z$' } | Select-Object -First 1
}
if (-not $asset) {
    throw "No 64-bit mpv build found in $($release.html_url)"
}
Write-Host "    mpv build: $($asset.name)"
$mpvArchive = Join-Path $Work $asset.name
Get-File $asset.browser_download_url $mpvArchive

$sevenZip = Get-Command 7z -ErrorAction SilentlyContinue
if ($sevenZip) {
    $sevenZip = $sevenZip.Source
} else {
    $sevenZip = Join-Path $Work "7zr.exe"
    Get-File "https://www.7-zip.org/a/7zr.exe" $sevenZip
}
$mpvDir = Join-Path $Work "mpv"
Invoke-Native $sevenZip @("x", "-y", "-bso0", "-o$mpvDir", $mpvArchive)
$mpvExe = Get-ChildItem -Path $mpvDir -Filter "mpv.exe" -Recurse | Select-Object -First 1
if (-not $mpvExe) {
    throw "mpv.exe wasn't in $($asset.name)"
}
Copy-Item $mpvExe.FullName $Tools
Get-ChildItem -Path $mpvExe.DirectoryName -Filter "*.dll" | Copy-Item -Destination $Tools

foreach ($exe in @("yt-dlp.exe", "deno.exe", "mpv.exe")) {
    if (-not (Test-Path (Join-Path $Tools $exe))) {
        throw "$exe is missing from $Tools"
    }
}
Invoke-Native (Join-Path $Tools "yt-dlp.exe") @("--version")
Invoke-Native (Join-Path $Tools "deno.exe") @("--version")

$notice = @"
Tapedeck $Version for Windows

Tapedeck bundles these programs, each under its own license:

  mpv            https://mpv.io (GPLv2 or later)
                 This build: $($asset.name)
                 Source and build scripts: https://github.com/shinchiro/mpv-winbuild-cmake
  yt-dlp         https://github.com/yt-dlp/yt-dlp (Unlicense)
  Deno           https://deno.com (MIT)
  Qt 6, PySide6  https://www.qt.io (LGPLv3), used unmodified

Tapedeck plays YouTube audio through yt-dlp and mpv for personal use.
Doing so may go against YouTube's Terms of Service.
"@
Set-Content -Path (Join-Path $App "THIRD-PARTY.txt") -Value $notice -Encoding UTF8

Step "Packing the portable zip"
$zip = Join-Path $Out "Tapedeck-$Version-windows-portable.zip"
if (Test-Path $zip) {
    Remove-Item $zip
}
Compress-Archive -Path $App -DestinationPath $zip

Step "Building the installer"
$iscc = $null
foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA "Programs"))) {
    if (-not $base) {
        continue  # e.g. no "Program Files (x86)" on 32-bit Windows
    }
    $candidate = Join-Path $base "Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) {
        $iscc = $candidate
        break
    }
}
if (-not $iscc) {
    $found = Get-Command iscc -ErrorAction SilentlyContinue
    if ($found) {
        $iscc = $found.Source
    }
}
if ($iscc) {
    Invoke-Native $iscc @("/Q", "/DAppVersion=$Version", "/DSourceDir=$App", "/DOutputDir=$Out",
                          (Join-Path $PSScriptRoot "tapedeck.iss"))
} else {
    Write-Warning "Inno Setup 6 isn't installed, so only the portable zip was made. Get it from https://jrsoftware.org/isdl.php"
}

Step "Done"
Get-ChildItem $Out | ForEach-Object {
    Write-Host ("    {0}  ({1:N0} MB)" -f $_.Name, ($_.Length / 1MB))
}
