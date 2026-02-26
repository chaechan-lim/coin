from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class ExchangeConfig(BaseSettings):
    name: str = "bithumb"
    api_key: str = ""
    api_secret: str = ""
    rate_limit_per_sec: int = 8

    model_config = {"env_prefix": "EXCHANGE_"}


class TradingConfig(BaseSettings):
    mode: str = "paper"  # "paper" or "live"
    evaluation_interval_sec: int = 300  # 5 minutes
    initial_balance_krw: float = 500_000
    tracked_coins: list[str] = [
        "BTC/KRW",
        "ETH/KRW",
        "XRP/KRW",
        "SOL/KRW",
        "ADA/KRW",
    ]
    min_trade_interval_sec: int = 3600  # 1 hour per coin
    cooldown_after_buy_sec: int = 1800  # 30 min
    daily_buy_limit: int = 20          # 일일 매수 상한 (매도는 무제한)
    max_daily_coin_buys: int = 3       # 코인당 일일 매수 상한 (왕복 3회)
    daily_trade_limit: int = 10        # (레거시, 미사용) 하위 호환용
    min_combined_confidence: float = 0.50
    min_profit_vs_fee_ratio: float = 2.0  # expected return > 2x round-trip fee

    # 거래량 급등 로테이션 설정 (tracked_coins와 별도 — 서지 전용)
    rotation_enabled: bool = True
    rotation_coins: list[str] = [
        "DOGE/KRW", "AVAX/KRW", "DOT/KRW", "LINK/KRW", "TRX/KRW",
        "ATOM/KRW", "ETC/KRW", "XLM/KRW", "ALGO/KRW", "NEAR/KRW",
        "SAND/KRW", "MANA/KRW", "AXS/KRW", "AAVE/KRW", "BCH/KRW",
        "USDT/KRW", "USDC/KRW",  # 스테이블코인 (헤지)
    ]
    surge_threshold: float = 3.0       # 서지 감지 임계 배수 (백테스트 C 결과 적용)
    rotation_cooldown_sec: int = 7200  # 로테이션 최소 간격 (2시간)

    model_config = {"env_prefix": "TRADING_"}


class RiskConfig(BaseSettings):
    max_single_coin_pct: float = 0.40
    max_drawdown_pct: float = 0.10
    daily_loss_limit_pct: float = 0.03
    max_trade_size_pct: float = 0.20
    max_open_orders: int = 20

    model_config = {"env_prefix": "RISK_"}


class DatabaseConfig(BaseSettings):
    url: str = "postgresql+asyncpg://coin:coin@localhost:5432/coin_trading"
    echo: bool = False

    model_config = {"env_prefix": "DB_"}


class RedisConfig(BaseSettings):
    url: str = "redis://localhost:6379/0"

    model_config = {"env_prefix": "REDIS_"}


class NotificationConfig(BaseSettings):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    enabled: bool = False

    model_config = {"env_prefix": "NOTIFY_"}


class BinanceConfig(BaseSettings):
    enabled: bool = False
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    default_leverage: int = 3
    max_leverage: int = 10
    tracked_coins: list[str] = [
        "BTC/USDT",
        "ETH/USDT",
    ]
    futures_fee: float = 0.0004  # 0.04% maker/taker

    model_config = {"env_prefix": "BINANCE_"}


class BinanceTradingConfig(BaseSettings):
    """바이낸스 선물 전용 트레이딩 설정."""
    mode: str = "paper"  # "paper" or "live" (빗썸과 독립)
    evaluation_interval_sec: int = 300
    initial_balance_usdt: float = 1000.0
    min_combined_confidence: float = 0.50
    max_trade_size_pct: float = 0.15
    daily_buy_limit: int = 15
    max_daily_coin_buys: int = 3

    model_config = {"env_prefix": "BINANCE_TRADING_"}


class AppConfig(BaseSettings):
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    binance: BinanceConfig = Field(default_factory=BinanceConfig)
    binance_trading: BinanceTradingConfig = Field(default_factory=BinanceTradingConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    log_level: str = "INFO"

    model_config = {"env_prefix": "APP_"}


def get_config() -> AppConfig:
    return AppConfig()
