"""v2 enums 테스트."""
from core.enums import Regime, Direction


class TestRegime:
    def test_values(self):
        assert Regime.TRENDING_UP == "trending_up"
        assert Regime.TRENDING_DOWN == "trending_down"
        assert Regime.RANGING == "ranging"
        assert Regime.VOLATILE == "volatile"

    def test_is_str(self):
        assert isinstance(Regime.TRENDING_UP, str)

    def test_all_members(self):
        assert len(Regime) == 4


class TestDirection:
    def test_values(self):
        assert Direction.LONG == "long"
        assert Direction.SHORT == "short"
        assert Direction.FLAT == "flat"

    def test_is_str(self):
        assert isinstance(Direction.LONG, str)

    def test_all_members(self):
        assert len(Direction) == 3
