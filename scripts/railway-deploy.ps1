# Deploy DauThauBot lên Railway (CLI + token, không cần mở dashboard nhiều bước).
# Cách dùng:
#   1. Tạo token: https://railway.com/account/tokens
#   2. PowerShell:
#        cd c:\Users\lehuu\Desktop\Tool
#        $env:RAILWAY_TOKEN = "your-token-here"
#        .\scripts\railway-deploy.ps1
#   3. Lần đầu: script tạo project "DauThauBot" (nếu chưa link), set biến từ .env, deploy.
#
# Lưu ý: Tắt bot local trước khi deploy (cùng TELEGRAM_BOT_TOKEN → lỗi 409).

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not $env:RAILWAY_TOKEN) {
    Write-Host "Thieu RAILWAY_TOKEN. Tao tai: https://railway.com/account/tokens" -ForegroundColor Red
    Write-Host '  $env:RAILWAY_TOKEN = "..."; .\scripts\railway-deploy.ps1'
    exit 1
}

if (-not (Test-Path ".env")) {
    Write-Host "Khong tim thay .env — copy tu .env.example va dien TELEGRAM_*" -ForegroundColor Red
    exit 1
}

$cli = "npx"
$cliArgs = @("-y", "@railway/cli@4.5.4")

function Invoke-Railway {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & $cli @cliArgs @Args
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "=== DauThauBot — Railway deploy ===" -ForegroundColor Cyan

# Link hoac tao project
if (-not (Test-Path ".railway")) {
    Write-Host "Chua link project — tao moi 'DauThauBot'..." -ForegroundColor Yellow
    Invoke-Railway @("init", "-n", "DauThauBot")
} else {
    Write-Host "Da co .railway — dung project da link."
}

# Bien bat buoc cho volume SQLite
$railwayOnly = @{
    "DATA_DIR"                   = "/data"
    "USE_PLAYWRIGHT"             = "true"
    "PLAYWRIGHT_HEADLESS"        = "true"
    "CRAWL_PER_KEYWORD"          = "true"
    "LOG_TO_STDOUT"              = "true"
}

# Doc .env (bo qua comment, dong trong)
$fromEnv = @{}
Get-Content ".env" -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()
    if ($val.StartsWith('"') -and $val.EndsWith('"')) { $val = $val.Substring(1, $val.Length - 2) }
    if ($val.StartsWith("'") -and $val.EndsWith("'")) { $val = $val.Substring(1, $val.Length - 2) }
    if ($key -and $val) { $fromEnv[$key] = $val }
}

$skipKeys = @("RAILWAY_TOKEN", "PATH", "PYTHONPATH")
$setArgs = @()
foreach ($kv in $railwayOnly.GetEnumerator()) {
    $setArgs += "--set"
    $setArgs += "$($kv.Key)=$($kv.Value)"
}
foreach ($kv in $fromEnv.GetEnumerator()) {
    if ($skipKeys -contains $kv.Key) { continue }
    if ($railwayOnly.ContainsKey($kv.Key)) { continue }
    $setArgs += "--set"
    $setArgs += "$($kv.Key)=$($kv.Value)"
}

Write-Host "Cap nhat bien moi truong tren Railway ($($setArgs.Count / 2) bien)..." -ForegroundColor Yellow
$varCmd = @("variables") + $setArgs
Invoke-Railway @varCmd

# KEYWORDS tren volume: huong dan sau deploy
if (Test-Path "config\keywords.yaml") {
    Write-Host ""
    Write-Host "Sau khi Online: gan Volume mount /data, roi upload keywords:" -ForegroundColor Yellow
    Write-Host "  npx @railway/cli shell"
    Write-Host "  cat > /data/keywords.yaml   (paste noi dung config/keywords.yaml)"
    Write-Host "Hoac dat KEYWORDS_YAML_PATH=/data/keywords.yaml trong Variables."
}

Write-Host "Deploy image (railway up) — build Docker ~5-15 phut..." -ForegroundColor Yellow
Invoke-Railway @("up", "--detach", "--ci")

Write-Host ""
Write-Host "Xong. Mo dashboard Railway -> Deployments -> xem log co:" -ForegroundColor Green
Write-Host "  [run_railway] v3-inline-scheduler-utc"
Write-Host "  railway_main: scheduler + Telegram bot"
Write-Host "Tren Telegram: /ping"
Write-Host ""
Write-Host "QUAN TRONG: Service -> Volumes -> Add volume, mount path: /data" -ForegroundColor Yellow
