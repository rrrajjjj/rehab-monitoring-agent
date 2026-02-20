"""Unit tests for difficulty builder."""

from crtv.domain.models import DifficultyRow
from crtv.builders.difficulty_builder import build_difficulty_ticks


def test_difficulty_tick_completeness():
    """Difficulty ticks grouped by (session, game_mode, t_sec)."""
    rows = [
        DifficultyRow(session_id=1, patient_id=1, protocol_id=101, game_mode="default", seconds_from_start=0, parameter_key="speed", parameter_value="0.5"),
        DifficultyRow(session_id=1, patient_id=1, protocol_id=101, game_mode="default", seconds_from_start=0, parameter_key="targets", parameter_value="5"),
        DifficultyRow(session_id=1, patient_id=1, protocol_id=101, game_mode="default", seconds_from_start=300, parameter_key="speed", parameter_value="0.6"),
    ]
    ticks = build_difficulty_ticks(rows)
    assert len(ticks) == 2
    t0 = next(t for t in ticks if t.t_sec == 0)
    assert t0.modulators["speed"] == 0.5
    assert t0.modulators["targets"] == 5
    t300 = next(t for t in ticks if t.t_sec == 300)
    assert t300.modulators["speed"] == 0.6
