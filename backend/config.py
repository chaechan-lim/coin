from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import Optional


class ExchangeConfig(BaseSettings):
    enabled: bool = True
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
    min_trade_interval_sec: int = 518400  # 6일 (cd36, 백테스트 최적)
    cooldown_after_sell_sec: int = 518400  # 6일 (cd36)
    cooldown_after_buy_sec: int = 518400  # 6일 (cd36)
    daily_buy_limit: int = 20          # 일일 매수 상한 (매도는 무제한)
    max_daily_coin_buys: int = 3       # 코인당 일일 매수 상한 (왕복 3회)
    daily_trade_limit: int = 10        # (레거시, 미사용) 하위 호환용
    min_combined_confidence: float = 0.50
    min_profit_vs_fee_ratio: float = 2.0  # expected return > 2x round-trip fee
    asymmetric_mode: bool = True  # 비대칭 전략: 하락장 매수차단 + 상승장 공격

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

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v

    @field_validator("min_combined_confidence")
    @classmethod
    def validate_confidence(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"min_combined_confidence must be 0.0-1.0, got {v}")
        return v

    @field_validator("daily_buy_limit")
    @classmethod
    def validate_daily_buy_limit(cls, v):
        if v < 1:
            raise ValueError(f"daily_buy_limit must be >= 1, got {v}")
        return v

    model_config = {"env_prefix": "TRADING_"}


class RiskConfig(BaseSettings):
    max_single_coin_pct: float = 0.40
    max_drawdown_pct: float = 0.10
    daily_loss_limit_pct: float = 0.03
    max_trade_size_pct: float = 0.30
    max_open_orders: int = 20
    rebalancing_enabled: bool = True
    rebalancing_target_pct: float = 0.35  # 리밸런싱 후 목표 비중

    @field_validator("max_single_coin_pct", "max_drawdown_pct", "daily_loss_limit_pct", "max_trade_size_pct", "rebalancing_target_pct")
    @classmethod
    def validate_pct(cls, v):
        if not 0.0 < v <= 1.0:
            raise ValueError(f"percentage must be 0.0-1.0, got {v}")
        return v

    model_config = {"env_prefix": "RISK_"}


class DatabaseConfig(BaseSettings):
    url: str = "postgresql+asyncpg://coin:coin@localhost:5432/coin_trading"
    echo: bool = False

    model_config = {"env_prefix": "DB_"}


class RedisConfig(BaseSettings):
    url: str = "redis://localhost:6379/0"

    model_config = {"env_prefix": "REDIS_"}


class NotificationConfig(BaseSettings):
    enabled: bool = False
    provider: str = "telegram"  # "telegram", "discord", "slack" (쉼표로 복수 가능: "telegram,discord")
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    slack_webhook_url: str = ""

    model_config = {"env_prefix": "NOTIFY_"}


class BinanceConfig(BaseSettings):
    enabled: bool = False
    spot_enabled: bool = False
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True
    default_leverage: int = 2
    max_leverage: int = 10
    tracked_coins: list[str] = [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "XRP/USDT",
        "BNB/USDT",
    ]
    futures_fee: float = 0.0004  # 0.04% maker/taker

    model_config = {"env_prefix": "BINANCE_"}


class BinanceTradingConfig(BaseSettings):
    """바이낸스 선물 전용 트레이딩 설정."""
    mode: str = "paper"  # "paper" or "live" (빗썸과 독립)
    evaluation_interval_sec: int = 300
    initial_balance_usdt: float = 1000.0
    min_combined_confidence: float = 0.55
    max_trade_size_pct: float = 0.35
    daily_buy_limit: int = 15
    max_daily_coin_buys: int = 3
    min_trade_interval_sec: int = 1036800  # 12일 (cd72, 백테스트 최적)
    min_sell_active_weight: float = 0.20  # 숏 진입 시 최소 참여 가중치 (2전략 이상)
    ws_price_monitor: bool = True  # WebSocket 실시간 가격 모니터 활성화

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v

    @field_validator("min_combined_confidence")
    @classmethod
    def validate_confidence(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"min_combined_confidence must be 0.0-1.0, got {v}")
        return v

    model_config = {"env_prefix": "BINANCE_TRADING_"}


class BinanceSpotTradingConfig(BaseSettings):
    """바이낸스 현물 전용 트레이딩 설정."""
    mode: str = "paper"  # "paper" or "live" (독립)
    evaluation_interval_sec: int = 300
    initial_balance_usdt: float = 500.0
    min_combined_confidence: float = 0.55
    max_trade_size_pct: float = 0.30
    daily_buy_limit: int = 20
    max_daily_coin_buys: int = 3
    cooldown_after_sell_sec: int = 518400  # 6일 (cd36)
    cooldown_after_buy_sec: int = 518400  # 6일 (cd36)
    rotation_enabled: bool = True

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v

    @field_validator("min_combined_confidence")
    @classmethod
    def validate_confidence(cls, v):
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"min_combined_confidence must be 0.0-1.0, got {v}")
        return v

    model_config = {"env_prefix": "BINANCE_SPOT_TRADING_"}


class LLMConfig(BaseSettings):
    enabled: bool = False
    api_key: str = ""
    model: str = "claude-haiku-4-5-20251001"
    fallback_model: str = "claude-sonnet-4-6"
    gemini_api_key: str = ""                         # Google Gemini API key
    gemini_fallback_model: str = "gemini-3-flash-preview"  # cross-provider fallback
    max_tokens: int = 4096  # LLM 응답 최대 토큰 (회고 짤림 방지)
    diagnostic_max_tokens: int = 512  # 진단 에이전트 응답 토큰
    daily_review_enabled: bool = True

    model_config = {"env_prefix": "LLM_"}


class DiscordBotConfig(BaseSettings):
    """Discord 봇 설정 (자연어 명령)."""
    enabled: bool = False
    bot_token: str = ""
    channel_id: int = 0           # 0이면 멘션으로만 응답
    allowed_user_ids: list[int] = []  # 빈 리스트면 write 제한 없음
    max_response_tokens: int = 2048
    model: str = ""               # 빈 문자열이면 LLMConfig.model 사용

    model_config = {"env_prefix": "DISCORD_BOT_"}


class AppConfig(BaseSettings):
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    binance: BinanceConfig = Field(default_factory=BinanceConfig)
    binance_trading: BinanceTradingConfig = Field(default_factory=BinanceTradingConfig)
    binance_spot_trading: BinanceSpotTradingConfig = Field(default_factory=BinanceSpotTradingConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    discord_bot: DiscordBotConfig = Field(default_factory=DiscordBotConfig)
    log_level: str = "INFO"

    model_config = {"env_prefix": "APP_"}


def get_config() -> AppConfig:
    return AppConfig()
