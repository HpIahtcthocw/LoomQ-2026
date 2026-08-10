# LoomQ 一键启动（Windows）
#
# 双击运行，或者在 PowerShell 里执行：  .\start.ps1
# 它会激活虚拟环境、读入 .env 里的凭据、起服务并打开浏览器。
#
# 什么都没配也能跑：内置参考模拟器不需要任何依赖和网络，
# 三个现成的例子照样能完整走完一遍。

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# 1 · Python：优先用仓库自带的 3.10 虚拟环境
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
  $py = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $py) {
  Write-Host ''
  Write-Host '没找到 Python。请先装 Python 3.10 或更高版本：https://www.python.org/downloads/'
  Write-Host '装的时候记得勾上 "Add Python to PATH"。'
  Read-Host '按回车退出'
  exit 1
}

# 2 · 凭据：.env 里一行一个 KEY=VALUE。没有这个文件也能跑，只是少了
#      自然语言入口和真机，模拟器与内置示例不受影响。
$envFile = Join-Path $PSScriptRoot '.env'
if (Test-Path $envFile) {
  foreach ($line in Get-Content $envFile) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
    $k, $v = $t.Split('=', 2)
    [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim().Trim('"'), 'Process')
  }
  Write-Host "已读入 .env" -ForegroundColor DarkGray
} else {
  Write-Host ''
  Write-Host '没有找到 .env，将以「只用内置模拟器」的方式启动。' -ForegroundColor DarkYellow
  Write-Host '三个现成的例子可以完整跑通；想用中文自由提问或连真机，'
  Write-Host '把 env.example.txt 复制成 .env 并填好里面的值。'
}

Write-Host ''
Set-Location (Join-Path $PSScriptRoot 'starter_kit')
& $py -m loomq.web @args
