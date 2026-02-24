# ============================================================
#  코인 자동 매매 시스템 — Windows PowerShell 실행 스크립트
#  실행 정책 허용 필요: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# ============================================================

param(
    [switch]$Docker,          # Docker Compose로 실행
    [switch]$Frontend,        # 프론트엔드도 함께 실행
    [switch]$Install          # 패키지만 설치
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Write-Step($n, $msg) {
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}

# ── Docker 모드 ──────────────────────────────────────────────
if ($Docker) {
    Write-Step "1" "Docker Compose로 전체 시스템 시작..."
    docker compose up -d --build
    Write-Host ""
    Write-Host "✅ 시스템이 시작되었습니다." -ForegroundColor Green
    Write-Host "   대시보드: http://localhost:3000" -ForegroundColor Yellow
    Write-Host "   API Docs: http://localhost:8000/docs" -ForegroundColor Yellow
    Write-Host "   로그 확인: docker compose logs -f backend" -ForegroundColor Gray
    exit 0
}

# ── 로컬 개발 모드 ───────────────────────────────────────────
Write-Step "1" ".env 파일 확인..."
if (-not (Test-Path "$Root\.env")) {
    Write-Host "  .env 파일이 없습니다. .env.example 을 복사합니다." -ForegroundColor Yellow
    Copy-Item "$Root\.env.example" "$Root\.env"
    Write-Host "  ⚠️  $Root\.env 파일을 편집하여 API 키를 설정한 후 다시 실행하세요." -ForegroundColor Red
    exit 1
}

Write-Step "2" "Python 가상환경 확인..."
$VenvPath = "$Root\backend\venv"
if (-not (Test-Path "$VenvPath\Scripts\Activate.ps1")) {
    Write-Host "  가상환경 생성 중..." -ForegroundColor Gray
    python -m venv $VenvPath
}
& "$VenvPath\Scripts\Activate.ps1"

Write-Step "3" "패키지 설치..."
pip install -r "$Root\backend\requirements.txt" -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 패키지 설치 실패. Python 3.12 이상이 설치되어 있는지 확인하세요." -ForegroundColor Red
    exit 1
}

if ($Install) {
    Write-Host "✅ 패키지 설치 완료." -ForegroundColor Green
    exit 0
}

Write-Step "4" "DB 마이그레이션..."
Set-Location "$Root\backend"
alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 마이그레이션 실패." -ForegroundColor Red
    Write-Host "  PostgreSQL이 실행 중이고 .env의 DB_URL이 올바른지 확인하세요." -ForegroundColor Yellow
    Write-Host "  (로컬 DB: postgresql+asyncpg://coin:coin@localhost:5432/coin_trading)" -ForegroundColor Gray
    exit 1
}

# 프론트엔드 별도 터미널에서 실행
if ($Frontend) {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$Root\frontend'; npm install; npm run dev"
    Write-Host "  프론트엔드 개발 서버를 별도 창에서 시작했습니다." -ForegroundColor Gray
}

Write-Step "5" "백엔드 서버 시작 (http://localhost:8000)..."
Write-Host "  API 문서: http://localhost:8000/docs" -ForegroundColor Yellow
Write-Host "  종료: Ctrl+C" -ForegroundColor Gray
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
