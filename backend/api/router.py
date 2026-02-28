from fastapi import APIRouter
from api.portfolio import router as portfolio_router
from api.trades import router as trades_router
from api.strategies import router as strategies_router
from api.dashboard import router as dashboard_router
from api.events import router as events_router
from api.capital import router as capital_router
from api.websocket import router as ws_router


def create_api_router() -> APIRouter:
    api_router = APIRouter(prefix="/api/v1")
    api_router.include_router(portfolio_router)
    api_router.include_router(trades_router)
    api_router.include_router(strategies_router)
    api_router.include_router(dashboard_router)
    api_router.include_router(events_router)
    api_router.include_router(capital_router)
    return api_router


def get_ws_router() -> APIRouter:
    return ws_router
