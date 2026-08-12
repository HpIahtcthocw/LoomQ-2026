#requires -version 5
<#
网页入口的回归截图。先起服务：
    cd starter_kit; python -m loomq.web --port 8899 --no-browser

真机那张要真的排两分钟队，所以不自动跑。先用界面或 API 跑一次真机，
拿到任务编号，再用它回放同一份结果——编号在服务重启前一直有效：
    .\tools\shoot_webui.ps1 -Job cadb9b0c6990
#>
param(
  [string]$Job = '',
  [int]$Port = 8899
)

$chrome = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { Write-Error 'no chrome/edge found'; exit 1 }

$out = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path + '\starter_kit\evidence\files\webui'
New-Item -ItemType Directory -Force -Path $out | Out-Null
$base = "http://127.0.0.1:$Port/"

$shots = @(
  @{ q = '';                                f = '01-opening';  w = 1600; h = 1000 }
  @{ q = '?stage=pick&clock=41';            f = '02-pick';     w = 1600; h = 1000 }
  @{ q = '?example=1&clock=68';             f = '03-result';   w = 1600; h = 1400 }
  @{ q = '?stage=ask&clock=95';             f = '04-ask';      w = 1600; h = 1000 }
  @{ q = '?answers=1,0,2&clock=214';        f = '05-quiz';     w = 1600; h = 2400 }
  @{ q = '?answers=1,0,2&clock=214&cert=1'; f = '06-cert';     w = 1600; h = 1100 }
  @{ q = '?example=1&clock=68';             f = '07-mobile';   w = 412;  h = 1500 }
)
if ($Job) {
  $shots += @{ q = "?job=$Job&clock=248"; f = '08-hardware'; w = 1600; h = 1560 }
}

foreach ($s in $shots) {
  $file = Join-Path $out "$($s.f).png"
  & $chrome --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 `
    --window-size="$($s.w),$($s.h)" --virtual-time-budget=12000 `
    --screenshot="$file" ($base + $s.q) 2>$null | Out-Null
  $size = if (Test-Path $file) { (Get-Item $file).Length } else { 0 }
  Write-Host ("{0,-14} {1,9:N0} bytes" -f $s.f, $size)
}

if (-not $Job) {
  Write-Host ''
  Write-Host '没截真机那张。跑一次真机拿到任务编号后加 -Job <编号> 再跑一遍。'
}
