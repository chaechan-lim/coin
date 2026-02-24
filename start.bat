@echo off
REM ============================================================
REM  코인 자동 매매 시스템 — Windows 로컬 개발 실행 스크립트
REM  (Docker 없이 직접 실행할 때 사용)
REM ============================================================

echo [1/4] .env 파일 확인 중...
if not exist ".env" (
    echo .env 파일이 없습니다. .env.example 을 복사합니다.
    copy .env.example .env
    echo .env 파일을 편집하여 API 키 등을 설정하세요.
    pause
    exit /b 1
)

echo [2/4] Python 가상환경 확인 중...
if not exist "backend\venv\Scripts\activate.bat" (
    echo 가상환경을 생성합니다...
    python -m venv backend\venv
)
call backend\venv\Scripts\activate.bat

echo [3/4] 패키지 설치 중...
pip install -r backend\requirements.txt -q

echo [4/4] DB 마이그레이션 및 서버 시작...
cd backend
alembic upgrade head
if errorlevel 1 (
    echo Alembic 마이그레이션 실패. PostgreSQL이 실행 중인지 확인하세요.
    pause
    exit /b 1
)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
