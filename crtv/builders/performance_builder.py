"""Build PerformancePoint list from performance_estimators_plus rows."""

from collections import defaultdict

from crtv.domain.models import PerformanceRow, PerformancePoint


def build_performance_points(rows: list[PerformanceRow]) -> list[PerformancePoint]:
    """
    One scalar per (session_id, game_mode, t_sec).
    If multiple rows share same key, aggregate by mean.
    """
    grouped: dict[tuple[int, str, int], list[float]] = defaultdict(list)
    protocol_ids: dict[tuple[int, str, int], int] = {}
    for r in rows:
        key = (r.session_id, r.game_mode, r.seconds_from_start)
        try:
            grouped[key].append(float(r.parameter_value))
        except (ValueError, TypeError):
            grouped[key].append(0.0)
        protocol_ids[key] = r.protocol_id

    points: list[PerformancePoint] = []
    for (session_id, game_mode, t_sec), values in grouped.items():
        perf = sum(values) / len(values) if values else 0.0
        points.append(PerformancePoint(
            session_id=session_id,
            protocol_id=protocol_ids[(session_id, game_mode, t_sec)],
            game_mode=game_mode,
            t_sec=t_sec,
            performance=perf,
        ))
    return sorted(points, key=lambda p: (p.session_id, p.game_mode, p.t_sec))
