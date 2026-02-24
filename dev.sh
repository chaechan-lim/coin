#!/usr/bin/env bash
# =============================================================
#  코인 자동 매매 시스템 — WSL 로컬 개발 서버 실행 스크립트
#  (DB/Redis는 Docker, Python/Node는 로컬 WSL에서 실행)
#  실행: bash dev.sh
# =============================================================
set -e

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJ_DIR/backend"
FRONTEND_DIR="$PROJ_DIR/frontend"
VENV_DIR="$BACKEND_DIR/.venv"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
step() { echo -e "\n${CYAN}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}  ✔ $1${NC}"; }

# ── DB/Redis 확인 ────────────────────────────────────────────
step "DB/Redis 상태 확인"
if ! docker compose ps postgres 2>/dev/null | grep -q "healthy"; then
  echo "  DB가 실행되지 않았습니다. 시작 중..."
  docker compose up -d postgres redis
  echo "  DB 준비 대기 중..."
  sleep 8
fi
ok "DB/Redis 준비됨"

# ── 가상환경 ─────────────────────────────────────────────────
step "Python 가상환경 확인"
if [ ! -f "$VENV_DIR/bin/activate" ]; then
  python3.12 -m venv "$VENV_DIR"
  ok "가상환경 생성 완료"
fi
source "$VENV_DIR/bin/activate"
ok "가상환경 활성화: $VENV_DIR"

# ── 패키지 ───────────────────────────────────────────────────
step "Python 패키지 설치/확인"
pip install -q -r "$BACKEND_DIR/requirements.txt"
ok "패키지 설치 완료"

# ── DB 마이그레이션 ──────────────────────────────────────────
step "Alembic 마이그레이션"
cd "$BACKEND_DIR"
alembic upgrade head
ok "DB 마이그레이션 완료"

# ── 프론트엔드 의존성 ─────────────────────────────────────────
step "프론트엔드 의존성 확인"
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  (cd "$FRONTEND_DIR" && npm install)
  ok "npm install 완료"
else
  ok "node_modules 이미 존재"
fi

# ── 프론트엔드 백그라운드 실행 ────────────────────────────────
step "프론트엔드 개발 서버 시작 (백그라운드)"
(cd "$FRONTEND_DIR" && npm run dev > /tmp/frontend.log 2>&1) &
FRONTEND_PID=$!
ok "프론트엔드 PID: $FRONTEND_PID (http://localhost:5173)"

# ── 백엔드 실행 ──────────────────────────────────────────────
step "백엔드 서버 시작"
echo -e "${YELLOW}  API Docs: http://localhost:8000/docs${NC}"
echo -e "${YELLOW}  Dashboard: http://localhost:5173${NC}"
echo -e "${YELLOW}  종료: Ctrl+C${NC}"
echo ""

# Ctrl+C 시 프론트엔드도 같이 종료
trap "kill $FRONTEND_PID 2>/dev/null; echo ''; echo '서버 종료됨'" INT TERM

cd "$BACKEND_DIR"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
