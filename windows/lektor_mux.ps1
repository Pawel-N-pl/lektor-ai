# Mux a finished 5.1 lektor track back into its episode.
# Runs natively on Windows so the 2.7 GB write never crosses the WSL 9p bridge.
#
#   lektor_mux.ps1 -Dir E:\Filmy\Serial.S01   -> every *.lektor51.eac3 with no output yet
#   lektor_mux.ps1 -Dir ... -Match S02E02   -> just that episode
#   lektor_mux.ps1 -Dir ... -Force          -> rebuild outputs that already exist
param(
    [Parameter(Mandatory = $true)]
    [string]$Dir,           # the episode directory, the Windows way: E:\Filmy\Serial.S01
    [string]$Match = '',
    [string]$Suffix = '',   # e.g. -Suffix _v2 when the old output is open in a player
    [string]$FFmpeg = 'ffmpeg',   # a full path when it is not on PATH
    [switch]$Force
)

$ff = $FFmpeg
$work = Join-Path $Dir '_lektor'
$outd = Join-Path $work 'output'
New-Item -ItemType Directory -Force -Path $outd | Out-Null

$tracks = @(Get-ChildItem -Path $work -Filter '*.lektor51.eac3' -File | Sort-Object Name)
if ($Match) { $tracks = @($tracks | Where-Object { $_.Name -like "*$Match*" }) }
if ($tracks.Count -eq 0) { throw "no *.lektor51.eac3 in $work" }

foreach ($t in $tracks) {
    $base = $t.Name -replace '\.lektor51\.eac3$', ''
    $src = Join-Path $Dir "$base.mkv"
    if (-not (Test-Path $src)) { Write-Host "skip $base (no source mkv)" -ForegroundColor Yellow; continue }

    # Episode tag out of the release name, so the output is not a 70-character mouthful.
    $tag = if ($base -match '(S\d{2}E\d{2})') { $Matches[1] } else { $base }
    $out = Join-Path $outd "Ted.Lasso.${tag}_PL_LEKTOR${Suffix}.mkv"
    if ((Test-Path $out) -and -not $Force) { Write-Host "skip $tag (already muxed)" -ForegroundColor DarkGray; continue }

    Write-Host "mux $tag" -ForegroundColor Cyan
    $ffArgs = @(
        '-hide_banner', '-loglevel', 'error', '-y',
        '-i', $src, '-i', $t.FullName,
        '-map', '0', '-map', '1:a:0', '-c', 'copy',
        '-metadata:s:a:1', 'language=pol',
        '-metadata:s:a:1', 'title=Lektor AI (PL) 5.1',
        '-disposition:a:0', '0',
        '-disposition:a:1', 'default',
        $out
    )
    & $ff @ffArgs
    if ($LASTEXITCODE -eq 0) {
        '  {0:N0} MB  {1}' -f ((Get-Item $out).Length / 1MB), (Split-Path $out -Leaf)
    } else {
        Write-Host "  FAIL $LASTEXITCODE" -ForegroundColor Red
    }
}
