"""SpotLongEvaluator backward compat — spot_long_evaluator.py 에서 import 호환성 테스트.

실제 SpotEvaluator 테스트는 test_spot_evaluator.py에서 수행.
이 파일은 backward compat alias가 올바르게 동작하는지만 확인한다.
"""

from unittest.mock import MagicMock

from engine.direction_evaluator import DirectionEvaluator
from engine.spot_long_evaluator import SpotLongEvaluator, _hold_decision
from engine.spot_evaluator import SpotEvaluator


class TestBackwardCompatAlias:
    """SpotLongEvaluator가 SpotEvaluator의 alias인지 확인."""

    def test_alias_is_same_class(self):
        """SpotLongEvaluator는 SpotEvaluator와 동일한 클래스여야 한다."""
        assert SpotLongEvaluator is SpotEvaluator

    def test_isinstance_direction_evaluator(self):
        """SpotLongEvaluator(alias)가 DirectionEvaluator 프로토콜을 구현한다."""
        from unittest.mock import AsyncMock

        evaluator = SpotLongEvaluator(
            strategies=[MagicMock()],
            combiner=MagicMock(),
            market_data=AsyncMock(),
        )
        assert isinstance(evaluator, DirectionEvaluator)

    def test_hold_decision_re_exported(self):
        """_hold_decision이 spot_long_evaluator에서 re-export된다."""
        d = _hold_decision("test", "strategy")
        assert d.is_hold
        assert d.reason == "test"
