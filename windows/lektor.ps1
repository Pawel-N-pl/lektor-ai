<#
.SYNOPSIS
    Windows half of the lektor-ai pipeline: the heavy I/O that must not cross the WSL 9p bridge.

.DESCRIPTION
    Two stages, both pure ffmpeg:

      extract  For every .mkv in the input dir, pull out the Polish subtitle track (selected by
               language tag, never by index) and a stereo FLAC downmix of the original audio.
               Both land in the work dir next to the source files, which WSL reads directly
               through /mnt/e -- nothing is ever copied between Windows and WSL.

      mux      Take the finished lektor track produced in WSL and remux it into a new .mkv
               alongside every original stream. Stream copy only, no re-encoding.

.EXAMPLE
    .\lektor.ps1 -Stage extract
    .\lektor.ps1 -Stage mux
    .\lektor.ps1 -Stage extract -InputDir "E:\Filmy\Inny.Serial" -Force
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('extract', 'mux', 'status')]
    [string]$Stage,

    [string]$Config = (Join-Path $PSScriptRoot '..\lektor.config.json'),
    [string]$InputDir,
    [string]$Language,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

if (-not (Test-Path $Config)) { throw "Config not found: $Config" }
$cfg = Get-Content -Path $Config -Raw -Encoding UTF8 | ConvertFrom-Json

$ffmpeg  = $cfg.paths.ffmpeg_windows
$ffprobe = $cfg.paths.ffprobe_windows
foreach ($bin in @($ffmpeg, $ffprobe)) {
    if (-not (Test-Path $bin)) { throw "Binary not found: $bin -- fix paths.ffmpeg_windows / paths.ffprobe_windows in $Config" }
}

if (-not $InputDir) { $InputDir = $cfg.paths.input_dir_windows }
if (-not (Test-Path $InputDir)) { throw "Input dir not found: $InputDir" }
if (-not $Language) { $Language = $cfg.extract.subtitle_language }

$workDir = Join-Path $InputDir $cfg.paths.work_subdir
$outDir  = Join-Path $InputDir $cfg.paths.output_subdir
$null = New-Item -ItemType Directory -Force -Path $workDir

function Write-Step  ([string]$m) { Write-Host "  -> $m" -ForegroundColor DarkGray }
function Write-Ok    ([string]$m) { Write-Host "  OK  $m" -ForegroundColor Green }
function Write-Warn2 ([string]$m) { Write-Host "  !!  $m" -ForegroundColor Yellow }

function Invoke-Ffmpeg {
    param([string[]]$FfArgs, [string]$What)
    Write-Step "ffmpeg $($FfArgs -join ' ')"
    $output = & $ffmpeg -hide_banner -loglevel error -nostdin @FfArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit $LASTEXITCODE): $($output -join "`n")"
    }
}

# ---------------------------------------------------------------------------
# Stream selection -- by language tag, never by a hardcoded index.
# ---------------------------------------------------------------------------

function Get-AudioStreamCount {
    param([string]$File)

    $json = & $ffprobe -v error -print_format json -show_streams -select_streams a -- $File | Out-String
    $streams = ($json | ConvertFrom-Json).streams
    if (-not $streams) { return 0 }
    return @($streams).Count
}

function Get-SubtitleStreamIndex {
    param([string]$File, [string]$Lang)

    $json = & $ffprobe -v error -print_format json -show_streams -select_streams s -- $File | Out-String
    $streams = ($json | ConvertFrom-Json).streams
    if (-not $streams) { return $null }

    $rejects = @($cfg.extract.reject_title_patterns)

    $candidates = @()
    foreach ($s in $streams) {
        $tags = $null
        if ($s.PSObject.Properties.Name -contains 'tags') { $tags = $s.tags }

        $slang = ''
        if ($tags -and $tags.PSObject.Properties.Name -contains 'language') { $slang = $tags.language }
        if ($slang -ne $Lang) { continue }

        $title = ''
        if ($tags -and $tags.PSObject.Properties.Name -contains 'title') { $title = $tags.title }

        $rejected = $false
        foreach ($r in $rejects) { if ($title -match $r) { $rejected = $true } }

        # A forced track carries only signage, not dialogue -- useless as a lektor script.
        if ($s.PSObject.Properties.Name -contains 'disposition' -and $s.disposition.forced -eq 1) { $rejected = $true }

        $candidates += [pscustomobject]@{ Index = $s.index; Title = $title; Rejected = $rejected; Codec = $s.codec_name }
    }

    if (-not $candidates) { return $null }
    $preferred = @($candidates | Where-Object { -not $_.Rejected })
    if ($preferred.Count -gt 0) { return $preferred[0] }
    return $candidates[0]
}

# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

function Invoke-Extract {
    $files = @(Get-ChildItem -Path $InputDir -Filter *.mkv -File -Recurse |
               Where-Object { $_.FullName -notlike "*$($cfg.paths.work_subdir)*" } |
               Sort-Object Name)

    if ($files.Count -eq 0) { throw "No .mkv files under $InputDir" }
    Write-Host "`nEXTRACT -- $($files.Count) file(s), subtitle language '$Language'`n" -ForegroundColor Cyan

    foreach ($f in $files) {
        $base    = [IO.Path]::GetFileNameWithoutExtension($f.Name)
        $srtPath = Join-Path $workDir "$base.srt"
        $wavPath = Join-Path $workDir "$base.orig.flac"
        $nfoPath = Join-Path $workDir "$base.source.json"

        Write-Host $base -ForegroundColor White

        $sub = Get-SubtitleStreamIndex -File $f.FullName -Lang $Language
        if (-not $sub) {
            Write-Warn2 "no '$Language' subtitle stream -- skipped"
            Add-Content -Path (Join-Path $workDir 'error_log.txt') -Encoding UTF8 `
                -Value "$(Get-Date -Format s)`t$base`tno '$Language' subtitle stream"
            continue
        }
        $titleNote = ''
        if ($sub.Title) { $titleNote = " ($($sub.Title))" }
        Write-Step "subtitle stream 0:$($sub.Index) [$($sub.Codec)]$titleNote"

        if ($Force -or -not (Test-Path $srtPath)) {
            Invoke-Ffmpeg -What 'subtitle extract' -FfArgs @(
                '-y', '-i', $f.FullName, '-map', "0:$($sub.Index)", '-c:s', 'srt', $srtPath)
        } else { Write-Step "srt exists, skipping (use -Force to redo)" }

        if ($Force -or -not (Test-Path $wavPath)) {
            # Stereo downmix so WSL never has to read the 2.7 GB source over 9p.
            Invoke-Ffmpeg -What 'audio downmix' -FfArgs @(
                '-y', '-i', $f.FullName,
                '-map', "0:$($cfg.extract.audio_stream)",
                '-ac', "$($cfg.extract.downmix_channels)",
                '-ar', "$($cfg.extract.downmix_rate)",
                '-c:a', 'flac', '-compression_level', '5',
                $wavPath)
        } else { Write-Step "downmix exists, skipping (use -Force to redo)" }

        $wslSource = $f.FullName
        [pscustomobject]@{
            base            = $base
            source_windows  = $f.FullName
            subtitle_stream = $sub.Index
            subtitle_title  = $sub.Title
            language        = $Language
            extracted_at    = (Get-Date -Format s)
        } | ConvertTo-Json | Set-Content -Path $nfoPath -Encoding UTF8

        Write-Ok "$([IO.Path]::GetFileName($srtPath)) + $([IO.Path]::GetFileName($wavPath))"
    }

    Write-Host "`nWork dir: $workDir" -ForegroundColor Cyan
    Write-Host "Next, in WSL:  python3 wsl/lektor.py script  &&  python3 wsl/lektor.py voice`n"
}

function Invoke-Mux {
    $ext = $cfg.encode.extension
    $tracks = @(Get-ChildItem -Path $workDir -Filter "*.lektor.$ext" -File -ErrorAction SilentlyContinue | Sort-Object Name)
    if ($tracks.Count -eq 0) { throw "No *.lektor.$ext in $workDir -- run the WSL voice stage first" }

    $null = New-Item -ItemType Directory -Force -Path $outDir
    Write-Host "`nMUX -- $($tracks.Count) track(s)`n" -ForegroundColor Cyan

    foreach ($t in $tracks) {
        $base    = $t.Name -replace "\.lektor\.$ext$", ''
        $nfoPath = Join-Path $workDir "$base.source.json"
        if (-not (Test-Path $nfoPath)) { Write-Warn2 "$base -- no source.json, skipped"; continue }

        $src = (Get-Content -Path $nfoPath -Raw -Encoding UTF8 | ConvertFrom-Json).source_windows
        if (-not (Test-Path $src)) { Write-Warn2 "$base -- source gone: $src"; continue }

        $dest = Join-Path $outDir "${base}_PL_LEKTOR.mkv"
        Write-Host $base -ForegroundColor White
        if ((Test-Path $dest) -and -not $Force) { Write-Step "output exists, skipping (use -Force)"; continue }

        # -map 0 keeps every original stream and the lektor is appended AFTER all of them, so its
        # audio index is however many audio streams the source already had. Hardcoding 1 retagged
        # a commentary track as the lektor and made THAT the default on any multi-audio release.
        $nAudio = Get-AudioStreamCount -File $src
        $ffArgs = @(
            '-y', '-i', $src, '-i', $t.FullName,
            '-map', '0', '-map', '1:a:0',
            '-c', 'copy',
            "-metadata:s:a:$nAudio", 'language=pol',
            "-metadata:s:a:$nAudio", 'title=Lektor AI (PL)')
        for ($i = 0; $i -lt $nAudio; $i++) { $ffArgs += @("-disposition:a:$i", '0') }
        $ffArgs += @("-disposition:a:$nAudio", 'default', $dest)
        Invoke-Ffmpeg -What 'mux' -FfArgs $ffArgs

        $mb = [math]::Round((Get-Item $dest).Length / 1MB)
        Write-Ok "$([IO.Path]::GetFileName($dest)) (${mb} MB)"
    }

    Write-Host "`nOutput dir: $outDir`n" -ForegroundColor Cyan
}

function Invoke-Status {
    $ext = $cfg.encode.extension
    $files = @(Get-ChildItem -Path $InputDir -Filter *.mkv -File -Recurse |
               Where-Object { $_.FullName -notlike "*$($cfg.paths.work_subdir)*" } | Sort-Object Name)

    # The format expression has to be parenthesised: bare, PowerShell binds -f as an abbreviation
    # of -ForegroundColor and the whole stage dies on parameter binding before printing a row.
    Write-Host ("`n{0,-52} {1,-5} {2,-7} {3,-7} {4,-5}" -f 'EPISODE', 'SRT', 'AUDIO', 'SCRIPT', 'MUX') -ForegroundColor Cyan
    foreach ($f in $files) {
        $base = [IO.Path]::GetFileNameWithoutExtension($f.Name)
        $mark = {
            param($p)
            if (Test-Path $p) { return 'yes' } else { return '-' }
        }
        $short = if ($base.Length -gt 50) { $base.Substring(0, 50) } else { $base }
        Write-Host ("{0,-52} {1,-5} {2,-7} {3,-7} {4,-5}" -f $short,
            (& $mark (Join-Path $workDir "$base.srt")),
            (& $mark (Join-Path $workDir "$base.orig.flac")),
            (& $mark (Join-Path $workDir "$base.lektor.json")),
            (& $mark (Join-Path $outDir  "${base}_PL_LEKTOR.mkv")))
    }
    Write-Host ''
}

switch ($Stage) {
    'extract' { Invoke-Extract }
    'mux'     { Invoke-Mux }
    'status'  { Invoke-Status }
}
