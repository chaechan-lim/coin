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
    min_trade_interval_sec: int = 518400  # 6일 (빗썸 비활성, 미사용)
    cooldown_after_sell_sec: int = 518400  # 6일 (빗썸 비활성, 미사용)
    cooldown_after_buy_sec: int = 518400  # 6일 (빗썸 비활성, 미사용)
    daily_buy_limit: int = 20          # 일일 매수 상한 (매도는 무제한)
    max_daily_coin_buys: int = 3       # 코인당 일일 매수 상한 (왕복 3회)
    daily_trade_limit: int = 10        # (레거시, 미사용) 하위 호환용
    min_combined_confidence: float = 0.50
    min_profit_vs_fee_ratio: float = 2.0  # expected return > 2x round-trip fee
    asymmetric_mode: bool = True  # 비대칭 전략: 하락장 매수차단 + 상승장 공격
    paired_exit: bool = True      # 페어링 매도: 진입 전략의 SELL만 허용 (투표 매도 비활성)

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
    default_leverage: int = 3
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
    min_trade_interval_sec: int = 86400  # 24시간 (cd6, 7전략 백테스트 최적 PF 1.07)
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
    min_combined_confidence: float = 0.50  # Optuna binance 최적화 (2026-03-13)
    max_trade_size_pct: float = 0.20
    daily_buy_limit: int = 20
    max_daily_coin_buys: int = 3
    cooldown_after_sell_sec: int = 216000  # 60시간 (cd15, Optuna 바이낸스 최적화)
    cooldown_after_buy_sec: int = 216000  # 60시간 (cd15)
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


class SurgeTradingConfig(BaseSettings):
    """서지/모멘텀 트레이딩 전용 설정 (바이낸스 선물, 단기 매매)."""
    enabled: bool = False
    mode: str = "paper"  # "paper" or "live"
    leverage: int = 3
    initial_balance_usdt: float = 150.0
    max_concurrent: int = 3
    position_pct: float = 0.08
    sl_pct: float = 2.5                  # SL % (COIN-20: 2.0→2.5, SL 과다 손절 완화)
    tp_pct: float = 3.0                  # TP % (COIN-20: 4.0→3.0, 현실적 목표가)
    trail_activation_pct: float = 0.5    # 트레일링 활성화 % (COIN-20: 1.0→0.5, 조기 수익 확보)
    trail_stop_pct: float = 0.8
    max_hold_minutes: int = 120
    vol_threshold: float = 4.0           # 거래량 배수 임계값 (백테스트 PF 1.42)
    price_threshold: float = 1.0         # 가격 변동 % 임계값
    long_only: bool = False
    daily_trade_limit: int = 15
    scan_symbols_count: int = 30
    cooldown_per_symbol_sec: int = 3600  # 60분 (백테스트 최적 PF 1.71)
    scan_interval_sec: int = 5

    # COIN-20: 진입 필터 강화
    min_score: float = 0.55              # 최소 서지 점수 (0.40→0.55, 낮은 conf 진입 차단)
    rsi_overbought: float = 75.0         # RSI 과매수 차단 (85→75, 이미 오른 뒤 진입 방지)
    rsi_oversold: float = 25.0           # RSI 과매도 차단 (15→25, 이미 빠진 뒤 진입 방지)
    consecutive_sl_cooldown_sec: int = 10800  # 연속 SL 쿨다운 180분 (동일 코인 2+연속 SL 시)
    min_atr_pct: float = 0.5             # 최소 ATR% (횡보장 fake surge 차단)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v

    model_config = {"env_prefix": "SURGE_TRADING_"}


class FuturesV2Config(BaseSettings):
    """선물 엔진 v2 전용 설정."""
    enabled: bool = False
    mode: str = "paper"
    leverage: int = 3
    max_leverage: int = 5

    # Tier 1
    tier1_coins: list[str] = [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
    ]
    tier1_max_position_pct: float = 0.15
    tier1_eval_interval_sec: int = 60
    tier1_base_risk_pct: float = 0.02
    tier1_min_confidence: float = 0.4
    tier1_cooldown_seconds: int = 93600  # 26h (백테스트 최적)

    # Long evaluator — 현물 4전략 기반 (COIN-26)
    tier1_long_eval_interval_sec: int = 300       # 5분 (현물과 동일)
    tier1_long_min_confidence: float = 0.50       # 현물 라이브 동일
    tier1_long_cooldown_hours: float = 60.0       # 현물 Optuna 최적 (cd15)
    tier1_long_sl_atr_mult: float = 5.0           # ATR 배수 기반 SL (5× ATR)
    tier1_long_tp_atr_mult: float = 14.0          # ATR 배수 기반 TP (14× ATR)
    tier1_long_trail_activation_atr_mult: float = 3.0   # 트레일링 활성화 (3× ATR)
    tier1_long_trail_stop_atr_mult: float = 1.5         # 트레일링 스탑 (1.5× ATR)

    # Direction-specific Tier1 cooldowns — SL/TP 후 같은 방향 재진입 금지 (COIN-27)
    tier1_sl_long_cooldown_hours: float = 12.0    # 롱 SL/TP → 롱 재진입 금지 12h
    tier1_sl_short_cooldown_hours: float = 26.0   # 숏 SL/TP → 숏 재진입 금지 26h

    # Tier 2 (COIN-23: 필터 추가 + 파라미터 조정)
    tier2_enabled: bool = True
    tier2_max_concurrent: int = 3          # 5 → 3 (노이즈 거래 제거)
    tier2_max_position_pct: float = 0.05
    tier2_max_hold_minutes: int = 120
    tier2_scan_interval_sec: int = 60
    tier2_vol_threshold: float = 5.0
    tier2_price_threshold: float = 1.5
    tier2_sl_pct: float = 3.5             # 2.0 → 3.5 (3x에서 가격 1.17% 여유)
    tier2_tp_pct: float = 4.5             # 4.0 → 4.5
    tier2_trail_activation_pct: float = 1.5  # 1.0 → 1.5
    tier2_trail_stop_pct: float = 1.0     # 0.8 → 1.0
    tier2_daily_trade_limit: int = 20
    tier2_cooldown_per_symbol_sec: int = 3600  # 1800 → 3600 (60분)
    # COIN-23: 신규 필터 파라미터
    tier2_rsi_overbought: float = 75.0
    tier2_rsi_oversold: float = 25.0
    tier2_min_atr_pct: float = 0.5
    tier2_exhaustion_pct: float = 8.0
    tier2_min_score: float = 0.55
    tier2_consecutive_sl_cooldown_sec: int = 10800  # 180분

    # Regime detector
    regime_adx_enter: float = 27.0
    regime_adx_exit: float = 23.0
    regime_confirm_count: int = 2
    regime_min_duration_h: int = 3

    # Balance guard
    balance_divergence_warn_pct: float = 3.0
    balance_divergence_pause_pct: float = 5.0
    balance_check_interval_sec: int = 300

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v):
        if v not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got '{v}'")
        return v

    model_config = {"env_prefix": "FUTURES_V2_"}


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
    surge_trading: SurgeTradingConfig = Field(default_factory=SurgeTradingConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    futures_v2: FuturesV2Config = Field(default_factory=FuturesV2Config)
    discord_bot: DiscordBotConfig = Field(default_factory=DiscordBotConfig)
    log_level: str = "INFO"

    model_config = {"env_prefix": "APP_"}


def get_config() -> AppConfig:
    return AppConfig()
