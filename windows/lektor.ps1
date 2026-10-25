<#
.SYNOPSIS
    Windows half of the lektor-ai pipeline: the heavy I/O that must not cross the WSL 9p bridge.

.DESCRIPTION
    Two stages, both pure ffmpeg:

      extract  For every .mkv in the input dir, pull out the Polish subtitle track (selected by
               language tag, never by index) and, in a stereo layout only, a FLAC downmix
               of the original audio - a surround mix reads the film's own track instead.
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

    [string]$Config = (Join-Path $PSScriptRoot '..\lektor.toml'),
    [string]$InputDir,
    [string]$Language,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# Normalised once, so the '..' in the default does not turn up in every error message.
$Config = [IO.Path]::GetFullPath($Config)

function Write-Step  ([string]$m) { Write-Host "  -> $m" -ForegroundColor DarkGray }
function Write-Ok    ([string]$m) { Write-Host "  OK  $m" -ForegroundColor Green }
function Write-Warn2 ([string]$m) { Write-Host "  !!  $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# TOML
#
# Both halves of the pipeline read the same config, and when it stopped being one JSON file this
# half stopped being able to open it at all: PowerShell ships no TOML reader, and the
# ConvertFrom-Json that used to sit here throws on the first `[section]` it meets.
#
# What follows parses the subset the two config files actually contain - table headers including
# dotted ones, quoted keys, strings, numbers, booleans and single-line arrays. Not multi-line
# strings, inline tables or dates: none appear in either file, and supporting them would only be
# a second implementation to keep honest against the Python one that does the real reading.
# ---------------------------------------------------------------------------

function Remove-TomlComment {
    <# Everything from an unquoted '#' onwards is a comment. Inside a string it is a character. #>
    param([string]$Text)

    $quote = ''
    for ($i = 0; $i -lt $Text.Length; $i++) {
        $c = [string]$Text[$i]
        if ($quote -ne '') {
            if ($c -eq '\' -and $quote -eq '"') { $i++; continue }
            if ($c -eq $quote) { $quote = '' }
            continue
        }
        if ($c -eq '"' -or $c -eq "'") { $quote = $c; continue }
        if ($c -eq '#') { return $Text.Substring(0, $i) }
    }
    return $Text
}

function Split-TomlOutsideQuotes {
    <# Split on one separator character, ignoring the ones inside strings and nested brackets. #>
    param([string]$Text, [string]$Separator)

    $parts = @()
    $sb = [Text.StringBuilder]::new()
    $quote = ''
    $depth = 0
    for ($i = 0; $i -lt $Text.Length; $i++) {
        $c = [string]$Text[$i]
        if ($quote -ne '') {
            $null = $sb.Append($c)
            if ($c -eq '\' -and $quote -eq '"') {
                $i++
                if ($i -lt $Text.Length) { $null = $sb.Append([string]$Text[$i]) }
                continue
            }
            if ($c -eq $quote) { $quote = '' }
            continue
        }
        if ($c -eq '"' -or $c -eq "'") { $quote = $c; $null = $sb.Append($c); continue }
        if ($c -eq '[') { $depth++ }
        if ($c -eq ']') { $depth-- }
        if ($c -eq $Separator -and $depth -eq 0) { $parts += $sb.ToString(); $null = $sb.Clear(); continue }
        $null = $sb.Append($c)
    }
    $parts += $sb.ToString()
    return $parts
}

function Get-TomlScalar {
    param([string]$Text)

    $t = $Text.Trim()
    if ($t.Length -eq 0) { return '' }

    if ($t[0] -eq "'") {                      # literal string: no escapes at all
        $end = $t.IndexOf("'", 1)
        if ($end -lt 0) { return $t.Substring(1) }
        return $t.Substring(1, $end - 1)
    }
    if ($t[0] -eq '"') {                      # basic string: the usual backslash escapes
        $sb = [Text.StringBuilder]::new()
        for ($i = 1; $i -lt $t.Length; $i++) {
            $c = [string]$t[$i]
            if ($c -eq '\') {
                $i++
                if ($i -ge $t.Length) { break }
                $e = [string]$t[$i]
                switch ($e) {
                    'n'     { $null = $sb.Append("`n") }
                    'r'     { $null = $sb.Append("`r") }
                    't'     { $null = $sb.Append("`t") }
                    '"'     { $null = $sb.Append('"') }
                    '\'     { $null = $sb.Append('\') }
                    default { $null = $sb.Append('\'); $null = $sb.Append($e) }
                }
                continue
            }
            if ($c -eq '"') { break }
            $null = $sb.Append($c)
        }
        return $sb.ToString()
    }

    if ($t -eq 'true')  { return $true }
    if ($t -eq 'false') { return $false }

    $asInt = 0
    if ([int]::TryParse($t, [ref]$asInt)) { return $asInt }
    $asDouble = 0.0
    $style = [Globalization.NumberStyles]::Float
    $invariant = [Globalization.CultureInfo]::InvariantCulture
    if ([double]::TryParse($t, $style, $invariant, [ref]$asDouble)) { return $asDouble }
    return $t
}

function Get-TomlValue {
    param([string]$Text)

    $t = $Text.Trim()
    if ($t.Length -eq 0 -or $t[0] -ne '[') { return Get-TomlScalar $t }

    $close = $t.LastIndexOf(']')
    $inner = if ($close -gt 0) { $t.Substring(1, $close - 1) } else { $t.Substring(1) }
    $items = @()
    foreach ($piece in (Split-TomlOutsideQuotes $inner ',')) {
        if ($piece.Trim().Length -gt 0) { $items += , (Get-TomlValue $piece) }
    }
    return , $items                           # the comma keeps a one-item array an array
}

function Get-TomlName {
    <# A key or a table-header segment, with its quotes taken off. #>
    param([string]$Text)

    $t = $Text.Trim()
    if ($t.Length -ge 2 -and ($t[0] -eq '"' -or $t[0] -eq "'") -and $t[-1] -eq $t[0]) {
        return $t.Substring(1, $t.Length - 2)
    }
    return $t
}

function ConvertFrom-Toml {
    param([string]$Path)

    $root = @{}
    $table = $root
    foreach ($raw in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        $line = $raw.Trim()
        if ($line.Length -eq 0 -or $line[0] -eq '#') { continue }

        if ($line[0] -eq '[') {
            $close = $line.IndexOf(']')
            if ($close -lt 0) { continue }
            $table = $root
            foreach ($segment in (Split-TomlOutsideQuotes $line.Substring(1, $close - 1) '.')) {
                $name = Get-TomlName $segment
                if (-not $table.ContainsKey($name)) { $table[$name] = @{} }
                $table = $table[$name]
            }
            continue
        }

        $halves = Split-TomlOutsideQuotes $line '='
        if ($halves.Count -lt 2) { continue }
        $key = Get-TomlName $halves[0]
        $value = ($halves[1..($halves.Count - 1)] -join '=')
        $table[$key] = Get-TomlValue (Remove-TomlComment $value)
    }
    return $root
}

function Merge-TomlTable {
    <# `Over` wins, key by key, all the way down. Mirrors lektorlib.config._merge. #>
    param([hashtable]$Base, [hashtable]$Over)

    $out = @{}
    foreach ($k in $Base.Keys) { $out[$k] = $Base[$k] }
    foreach ($k in $Over.Keys) {
        if ($out.ContainsKey($k) -and $out[$k] -is [hashtable] -and $Over[$k] -is [hashtable]) {
            $out[$k] = Merge-TomlTable $out[$k] $Over[$k]
        } else {
            $out[$k] = $Over[$k]
        }
    }
    return $out
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# lektor.toml is gitignored, so a checkout nobody has configured yet has only the template. Read
# it rather than refusing to start - the same fallback the Python half makes.
$cfgPath = $Config
if (-not (Test-Path $cfgPath)) {
    $template = "$Config.example"
    if (-not (Test-Path $template)) { throw "Config not found: $Config (nor $template)" }
    Write-Warn2 "no $([IO.Path]::GetFileName($Config)) yet, reading $([IO.Path]::GetFileName($template))"
    $cfgPath = $template
}

$cfgDir  = Split-Path -Parent $Config
$cfgStem = [IO.Path]::GetFileNameWithoutExtension($Config)
$cfgExt  = [IO.Path]::GetExtension($Config)
$tuningPath = Join-Path $cfgDir "$cfgStem.tuning$cfgExt"

$cfg = ConvertFrom-Toml $cfgPath
if (Test-Path $tuningPath) { $cfg = Merge-TomlTable (ConvertFrom-Toml $tuningPath) $cfg }

function Resolve-Binary {
    <# A full path, or a bare name found on PATH - which is what winget and choco both give you. #>
    param([string]$Configured, [string]$Key)

    if (-not $Configured) { throw "paths.$Key is empty in $Config" }
    if (Test-Path $Configured) { return (Resolve-Path $Configured).Path }
    $found = Get-Command $Configured -CommandType Application -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw "Binary not found: $Configured -- fix paths.$Key in $Config, or put it on PATH"
}

$ffmpeg  = Resolve-Binary $cfg.paths.ffmpeg_windows  'ffmpeg_windows'
$ffprobe = Resolve-Binary $cfg.paths.ffprobe_windows 'ffprobe_windows'

if (-not $InputDir) { $InputDir = $cfg.paths.input_dir }
if (-not $InputDir) { $InputDir = $cfg.paths.input_dir_windows }
if (-not (Test-Path $InputDir)) { throw "Input dir not found: $InputDir" }
if (-not $Language) { $Language = $cfg.extract.subtitle_language }

$workDir = Join-Path $InputDir $cfg.paths.work_subdir
$outDir  = Join-Path $InputDir $cfg.paths.output_subdir
$null = New-Item -ItemType Directory -Force -Path $workDir

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

        $layout = $cfg.mix.channel_layout
        $surround = $layout -and $layout -notin @('stereo', '2.0', '')
        if ($surround) {
            # Mirrors lektorlib.media.stage_extract: pick_source rejects any source under six
            # channels, so in a surround layout nothing ever reads this file, and it is the
            # 300-400 MB, minutes-long half of the step. Flip mix.channel_layout to stereo and
            # the next extract writes it, because that is the one layout that mixes over it.
            Write-Step "no stereo downmix: mix.channel_layout is '$layout' and only a stereo mix reads that file"
        } elseif ($Force -or -not (Test-Path $wavPath)) {
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

function Get-TrackExtension {
    <#
        Which file the WSL side actually wrote. The encode profile follows the channel layout:
        a 5.1 mix is E-AC-3 because ac3 at 256k cannot carry six channels credibly, and the two
        halves of the pipeline have to agree on the filename or the mux finds nothing at all.
        Mirrors lektorlib.mix.encode_profile / is_surround.
    #>
    $layout = $cfg.mix.channel_layout
    if ($layout -and $layout -notin @('stereo', '2.0', '') -and $cfg.encode.surround) {
        return $cfg.encode.surround.extension
    }
    return $cfg.encode.extension
}

function Invoke-Mux {
    $ext = Get-TrackExtension
    $tracks = @(Get-ChildItem -Path $workDir -Filter "*.lektor.$ext" -File -ErrorAction SilentlyContinue | Sort-Object Name)
    if ($tracks.Count -eq 0) { throw "No *.lektor.$ext in $workDir -- run the WSL voice stage first" }

    $null = New-Item -ItemType Directory -Force -Path $outDir
    Write-Host "`nMUX -- $($tracks.Count) track(s)`n" -ForegroundColor Cyan

    foreach ($t in $tracks) {
        $base    = $t.Name -replace "\.lektor\.$ext$", ''
        $nfoPath = Join-Path $workDir "$base.source.json"
        if (-not (Test-Path $nfoPath)) { Write-Warn2 "$base -- no source.json, skipped"; continue }

        $nfo = Get-Content -Path $nfoPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $src = $nfo.source_windows
        if (-not (Test-Path $src)) { Write-Warn2 "$base -- source gone: $src"; continue }
        # Said, not done: the WSL mux (lektorlib.media.mux_args) appends a translated film's
        # <base>.srt as a 'Polski (AI)' stream when translate.subtitles is 'mux'. This one does
        # not, and nobody has a PowerShell box here to test a third input against, so it warns
        # rather than ship a .mkv the user believes carries those subtitles.
        if ($nfo.subtitle_origin -eq 'subs' -and "$($cfg.translate.subtitles)" -eq 'mux') {
            Write-Warn2 "$base -- translate.subtitles is 'mux', but this mux does not add the Polish subtitles; run 'python3 wsl/lektor.py mux' for them"
        }

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
    $ext = Get-TrackExtension
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
