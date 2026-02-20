"""Build DifficultyTick list from difficulty_modulators_plus rows."""

from collections import defaultdict

from crtv.domain.models import DifficultyRow, DifficultyTick


def build_difficulty_ticks(rows: list[DifficultyRow]) -> list[DifficultyTick]:
    """
    Group by (SESSION_ID, GAME_MODE, SECONDS_FROM_START).
    Within each group: modulators[PARAMETER_KEY] = float(PARAMETER_VALUE).
    Output sorted by t_sec.
    """
    grouped: dict[tuple[int, str, int], dict[str, float]] = defaultdict(dict)
    session_ids: set[int] = set()
    patient_ids: dict[tuple[int, str, int], int] = {}
    protocol_ids: dict[tuple[int, str, int], int] = {}
    for r in rows:
        key = (r.session_id, r.game_mode, r.seconds_from_start)
        try:
            grouped[key][r.parameter_key] = float(r.parameter_value)
        except (ValueError, TypeError):
            grouped[key][r.parameter_key] = 0.0
        session_ids.add(r.session_id)
        patient_ids[key] = r.patient_id
        protocol_ids[key] = r.protocol_id

    ticks: list[DifficultyTick] = []
    for (session_id, game_mode, t_sec), modulators in grouped.items():
        ticks.append(DifficultyTick(
            session_id=session_id,
            patient_id=patient_ids[(session_id, game_mode, t_sec)],
            protocol_id=protocol_ids[(session_id, game_mode, t_sec)],
            game_mode=game_mode,
            t_sec=t_sec,
            modulators=dict(modulators),
        ))
    return sorted(ticks, key=lambda t: (t.session_id, t.game_mode, t.t_sec))
