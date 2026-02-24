#!/usr/bin/env bash
# =============================================================
#  코인 자동 매매 시스템 — WSL 초기 환경 세팅 스크립트
#  Ubuntu 22.04 / 24.04 기준
#  실행: bash setup.sh
# =============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
step() { echo -e "\n${CYAN}[STEP $1]${NC} $2"; }
ok()   { echo -e "${GREEN}  ✔ $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $1${NC}"; }

# ── 0. 루트 체크 ─────────────────────────────────────────────
if [ "$(id -u)" = "0" ]; then
  echo -e "${RED}root로 실행하지 마세요. 일반 유저로 실행하세요.${NC}"; exit 1
fi

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
echo -e "${CYAN}프로젝트 경로: $PROJ_DIR${NC}"

# ── 1. 패키지 업데이트 ────────────────────────────────────────
step 1 "시스템 패키지 업데이트"
sudo apt-get update -qq
sudo apt-get install -y -qq curl wget git ca-certificates gnupg lsb-release software-properties-common
ok "기본 패키지 설치 완료"

# ── 2. Docker Engine ─────────────────────────────────────────
step 2 "Docker 설치 확인"
if command -v docker &>/dev/null; then
  ok "Docker 이미 설치됨: $(docker --version)"
else
  warn "Docker 설치 중..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  ok "Docker 설치 완료 — 그룹 반영을 위해 WSL 재시작 필요"
  echo -e "${YELLOW}  → 이 스크립트 종료 후: exit → wsl 명령으로 재진입 후 다시 실행${NC}"
fi

if ! docker compose version &>/dev/null 2>&1; then
  warn "Docker Compose (plugin) 설치 중..."
  DOCKER_CONFIG=${DOCKER_CONFIG:-$HOME/.docker}
  mkdir -p "$DOCKER_CONFIG/cli-plugins"
  curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
    -o "$DOCKER_CONFIG/cli-plugins/docker-compose"
  chmod +x "$DOCKER_CONFIG/cli-plugins/docker-compose"
fi
ok "Docker Compose: $(docker compose version)"

# ── 3. Python 3.12 ───────────────────────────────────────────
step 3 "Python 3.12 설치 확인"
if python3.12 --version &>/dev/null 2>&1; then
  ok "Python: $(python3.12 --version)"
else
  warn "Python 3.12 설치 중 (deadsnakes PPA)..."
  sudo add-apt-repository ppa:deadsnakes/ppa -y
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
  ok "Python 3.12 설치 완료"
fi

# ── 4. Node.js 20 ────────────────────────────────────────────
step 4 "Node.js 설치 확인"
if node --version &>/dev/null 2>&1; then
  ok "Node.js: $(node --version)"
else
  warn "Node.js 20 LTS 설치 중..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
  ok "Node.js 설치 완료"
fi

# ── 5. .env 파일 ─────────────────────────────────────────────
step 5 ".env 파일 설정"
if [ ! -f "$PROJ_DIR/.env" ]; then
  cp "$PROJ_DIR/.env.example" "$PROJ_DIR/.env"
  ok ".env.example → .env 복사 완료"
  warn ".env 파일에서 API 키 등을 설정하세요 (페이퍼 트레이딩은 비워도 됩니다)"
else
  ok ".env 파일 이미 존재함"
fi

# ── 6. 완료 ─────────────────────────────────────────────────
echo ""
echo -e "${GREEN}=============================="
echo -e " 초기 세팅 완료!"
echo -e "==============================${NC}"
echo ""
echo "다음 명령으로 시스템을 시작하세요:"
echo ""
echo -e "  ${CYAN}# Docker 전체 실행 (백엔드 + 프론트엔드 + DB)${NC}"
echo -e "  cd $PROJ_DIR"
echo -e "  docker compose up -d --build"
echo ""
echo -e "  ${CYAN}# 또는 로컬 개발 모드 (DB만 Docker)${NC}"
echo -e "  docker compose up -d postgres redis"
echo -e "  bash dev.sh"
echo ""
