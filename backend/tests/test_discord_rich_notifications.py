"""
Discord 노티 풍부화 테스트.

전수조사 결과 적용된 풍부한 metadata가 Discord 포맷터에 정상 반영되는지 검증.
"""
from services.notification.discord import DiscordAdapter


def _adapter():
    a = DiscordAdapter.__new__(DiscordAdapter)
    return a


# ── Trade Review (매매 회고) ──


def test_trade_review_full_context():
    """매매 회고 노티에 insights/recommendations/by_strategy/by_symbol 포함."""
    a = _adapter()
    meta = {
        "review_kind": "trade_review",
        "exchange": "binance_futures",
        "total_trades": 10, "buy_count": 5, "sell_count": 5,
        "win_count": 7, "loss_count": 3, "win_rate": 0.7,
        "pnl": 12.34, "profit_factor": 2.5,
        "largest_win": 5.0, "largest_loss": -2.0,
        "by_strategy": {
            "hmm_regime": {"trades": 5, "wins": 4, "win_rate": 0.8, "total_pnl": 8.5},
            "pairs_trading": {"trades": 5, "wins": 3, "win_rate": 0.6, "total_pnl": 3.84},
        },
        "by_symbol": {
            "BTC/USDT": {"trades": 6, "wins": 5, "win_rate": 0.83, "total_pnl": 10.0},
            "ETH/USDT": {"trades": 4, "wins": 2, "win_rate": 0.5, "total_pnl": 2.34},
        },
        "open_positions": [
            {"symbol": "ETH/USDT", "direction": "long", "unrealized_pnl": 1.5, "unrealized_pnl_pct": 0.5},
        ],
        "insights": ["승률 70%로 양호한 성과", "롱 포지션 우세"],
        "recommendations": ["현재 전략 유지", "포지션 사이즈 점진적 증가 검토"],
    }
    embed = a._format_strategy("매매 회고: 10건", None, meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "거래 / 승률" in field_names
    assert "수익 지표" in field_names
    assert "극단치" in field_names
    assert "전략별" in field_names
    assert any("코인별" in n for n in field_names)
    assert "보유 포지션" in field_names
    assert "인사이트" in field_names
    assert "추천" in field_names

    # insights가 본문에 들어갔는지
    insights_field = next(f for f in embed["fields"] if f["name"] == "인사이트")
    assert "승률 70%" in insights_field["value"]


def test_trade_review_no_open_positions():
    """포지션 없을 때는 보유 포지션 필드 안 나옴."""
    a = _adapter()
    meta = {
        "review_kind": "trade_review",
        "exchange": "binance_futures",
        "total_trades": 5, "buy_count": 3, "sell_count": 2,
        "win_count": 3, "loss_count": 2, "win_rate": 0.6,
        "pnl": 5.0, "profit_factor": 1.5,
        "largest_win": 3.0, "largest_loss": -1.5,
        "open_positions": [],
        "insights": [], "recommendations": [],
    }
    embed = a._format_strategy("매매 회고", None, meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "보유 포지션" not in field_names


# ── Performance Analytics (성과 분석) ──


def test_performance_analytics_windows():
    """성과 분석 노티에 7d/14d/30d 윈도우 비교 포함."""
    a = _adapter()
    meta = {
        "review_kind": "performance_analytics",
        "exchange": "binance_futures",
        "windows": {
            "7d": {"period_days": 7, "total_trades": 10, "win_count": 8, "loss_count": 2,
                   "win_rate": 0.8, "profit_factor": 5.0, "total_pnl": 15.0,
                   "largest_win": 5.0, "largest_loss": -1.0},
            "14d": {"period_days": 14, "total_trades": 20, "win_count": 14, "loss_count": 6,
                    "win_rate": 0.7, "profit_factor": 2.0, "total_pnl": 22.0},
            "30d": {"period_days": 30, "total_trades": 45, "win_count": 28, "loss_count": 17,
                    "win_rate": 0.62, "profit_factor": 1.5, "total_pnl": 35.0,
                    "largest_win": 8.0, "largest_loss": -3.0},
        },
        "by_strategy": {
            "hmm_regime": {"trades_30d": 20, "win_rate_30d": 0.7, "pnl_30d": 25.0,
                           "pnl_contribution_pct": 71.4, "trend": "improving"},
        },
        "by_symbol": {
            "BTC/USDT": {"trades_30d": 25, "win_rate_30d": 0.64, "pnl_30d": 20.0, "consecutive_losses": 0},
            "ETH/USDT": {"trades_30d": 20, "win_rate_30d": 0.6, "pnl_30d": 15.0, "consecutive_losses": 4},
        },
        "degradation_alerts": [],
        "insights": ["7일 PF 5.0으로 우수"],
        "recommendations": ["기존 전략 유지"],
    }
    embed = a._format_strategy("성과 분석", None, meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "기간별 성과" in field_names
    assert "30일 극단치" in field_names
    assert "전략별 30일" in field_names
    assert any("코인별" in n for n in field_names)

    # 연패 3+ 표시
    sym_field = next(f for f in embed["fields"] if "코인별" in f["name"])
    assert "🚨연패4" in sym_field["value"]

    # 트렌드 이모지 (improving)
    strat_field = next(f for f in embed["fields"] if "전략별" in f["name"])
    assert "📈" in strat_field["value"]


def test_performance_analytics_with_alerts():
    """성과 저하 경고 표시."""
    a = _adapter()
    meta = {
        "review_kind": "performance_analytics",
        "exchange": "binance_futures",
        "windows": {"30d": {"total_trades": 5, "total_pnl": -10.0, "win_rate": 0.2, "profit_factor": 0.3}},
        "by_strategy": {}, "by_symbol": {},
        "degradation_alerts": ["BTC/USDT 연속 5회 손실", "ETH/USDT win rate 30% 하락"],
        "insights": [], "recommendations": [],
    }
    embed = a._format_strategy("성과 분석", None, meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "성과 저하 경고" in field_names
    alert_field = next(f for f in embed["fields"] if f["name"] == "성과 저하 경고")
    assert "BTC/USDT 연속 5회 손실" in alert_field["value"]


# ── Strategy Advisor (전략 어드바이저) ──


def test_strategy_advisor_full():
    """전략 어드바이저 노티에 분석/방향/파라미터/제안 포함."""
    a = _adapter()
    meta = {
        "review_kind": "strategy_advisor",
        "exchange": "binance_futures",
        "exit_analysis": {"avg_hold_hours": 12.5, "sl_hit_rate": 0.15, "tp_hit_rate": 0.4},
        "param_sensitivities": [
            {"param_name": "tp_pct", "current_value": 15, "optimal_value": 12, "expected_pnl_change": 3.5},
            {"param_name": "sl_pct", "current_value": 8, "optimal_value": 6, "expected_pnl_change": 1.2},
        ],
        "direction_analysis": {
            "long": {"trades": 30, "win_rate": 0.7, "total_pnl": 25.0},
            "short": {"trades": 15, "win_rate": 0.4, "total_pnl": -5.0},
        },
        "analysis_summary": "롱 포지션 강세, 숏 약세",
        "suggestions": ["tp_pct 12로 조정", "숏 진입 임계값 강화"],
    }
    embed = a._format_strategy("전략 어드바이저", None, meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "분석 요약" in field_names
    assert "청산 분석" in field_names
    assert "방향별 성과" in field_names
    assert "파라미터 민감도" in field_names
    assert "💡 제안" in field_names

    sug_field = next(f for f in embed["fields"] if f["name"] == "💡 제안")
    assert "tp_pct 12로 조정" in sug_field["value"]


# ── Safe Order (자금 위험) ──


def test_safe_order_full_context():
    """SafeOrder 알림에 모든 자금 컨텍스트 포함."""
    a = _adapter()
    meta = {
        "symbol": "BTC/USDT",
        "side": "buy",
        "exchange": "binance_futures",
        "filled_qty": 0.001,
        "exec_price": 80000.0,
        "exec_cost": 80.0,
        "fee": 0.032,
        "order_id": "abc123",
        "strategy": "hmm_regime",
        "cash_before": 1000.0,
        "cash_after": 920.0,
    }
    embed = a._format_safe_order("DB 기록 실패", "DB connection lost", meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "심볼" in field_names
    assert "방향" in field_names
    assert "거래소" in field_names
    assert "체결 수량" in field_names
    assert "체결가" in field_names
    assert "체결 금액" in field_names
    assert "수수료" in field_names
    assert "주문 ID" in field_names
    assert "전략" in field_names
    assert "잔고 변화 (DB)" in field_names
    assert "🚨 즉시 조치" in field_names

    # 운영자 가이드가 포함됨
    action_field = next(f for f in embed["fields"] if f["name"] == "🚨 즉시 조치")
    assert "거래소에서 실제 포지션" in action_field["value"]


def test_safe_order_minimal():
    """SafeOrder 메타 누락돼도 알림은 발송 (즉시 조치 가이드는 항상)."""
    a = _adapter()
    embed = a._format_safe_order("긴급", None, {"symbol": "BTC/USDT"})
    assert any(f["name"] == "🚨 즉시 조치" for f in embed["fields"])


# ── Risk (매수 일시중지) ──


def test_risk_pause_with_context():
    """매수 일시중지에 사유 + 재개 조건 표시."""
    a = _adapter()
    meta = {
        "exchange": "bithumb",
        "coins": ["BTC", "ETH", "SOL"],
        "reason": "API 헬스체크 실패",
        "resume_condition": "헬스체크 복구 시 자동 재개",
    }
    embed = a._format_risk("매수 일시중지 (3개 코인)", "사유: API / 재개: 자동", meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "거래소" in field_names
    assert "사유" in field_names
    assert "재개 조건" in field_names
    assert any("영향 코인" in n for n in field_names)


def test_risk_pause_many_coins_truncated():
    """영향 코인 10개 초과 시 truncate."""
    a = _adapter()
    meta = {
        "exchange": "binance_futures",
        "coins": [f"COIN{i}" for i in range(15)],
        "reason": "API 실패",
        "resume_condition": "복구 시",
    }
    embed = a._format_risk("매수 중지", None, meta)
    coin_field = next(f for f in embed["fields"] if "영향 코인" in f["name"])
    assert "15" in coin_field["name"]  # 총 개수 표시
    assert "외 5개" in coin_field["value"]  # 5개 더 있음


def test_risk_cross_position_swap():
    """교차 포지션 전환 시 symbol/confidence 표시."""
    a = _adapter()
    meta = {
        "exchange": "binance_futures",
        "symbol": "BTC/USDT",
        "confidence": 0.75,
        "cross_qty": 0.005,
    }
    embed = a._format_risk("교차 포지션 전환", None, meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "심볼" in field_names
    assert "신뢰도" in field_names
    assert "교차 수량" in field_names


# ── Health (API 복구/중지) ──


def test_health_recovery_with_context():
    """API 복구 알림에 중지 시간 + 자동 수정 표시."""
    a = _adapter()
    meta = {
        "exchange": "bithumb",
        "auto_fixed": ["api_health"],
        "pause_duration_min": 5.5,
        "tracked_coins": ["BTC", "ETH"],
    }
    embed = a._format_health("info", "API 복구 — 매수 재개", "정상 조회", meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "거래소" in field_names
    assert "자동 수정" in field_names


def test_health_critical_pause():
    """API 중지 알림에 fail_streak + issues 표시."""
    a = _adapter()
    meta = {
        "exchange": "binance_futures",
        "fail_streak": 3,
        "issues": ["api_unreachable"],
        "tracked_coins": ["BTC", "ETH"],
        "resume_condition": "API 호출 성공 시 자동 재개",
    }
    embed = a._format_health("critical", "API 3회 실패", "BTC 가격 조회 실패", meta)
    field_names = [f["name"] for f in embed["fields"]]
    assert "거래소" in field_names
    assert "이상 항목" in field_names
    assert "연속 실패" in field_names
