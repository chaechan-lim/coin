"""
Backward compatibility — SpotLongEvaluator 이름으로 기존 import 유지.

실제 구현은 engine.spot_evaluator.SpotEvaluator로 이전됨 (COIN-28).
SpotLongEvaluator는 SpotEvaluator의 alias이다.
"""

from engine.spot_evaluator import SpotEvaluator as SpotLongEvaluator, _hold_decision

__all__ = ["SpotLongEvaluator", "_hold_decision"]
