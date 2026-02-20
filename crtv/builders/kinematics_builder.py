"""Build KinematicsPoint from kinematics / metric_plus rows."""

from collections import defaultdict

from crtv.domain.models import KinematicsRow, KinematicsPoint


def build_kinematics_points(rows: list[KinematicsRow]) -> list[KinematicsPoint]:
    """
    Map kinematics rows to KinematicsPoint.
    Group by (session_id, protocol_id, t_sec); map metric_key -> field.
    """
    KEY_MAP = {
        "mqi": "mqi",
        "workspace_volume": "workspace_volume",
        "smoothness": "smoothness",
        "velocity": "velocity",
        "pause_ratio": "pause_ratio",
    }
    grouped: dict[tuple[int, int, int], dict[str, float]] = defaultdict(dict)
    for r in rows:
        key = (r.session_id, r.protocol_id, r.seconds_from_start)
        field = KEY_MAP.get(r.metric_key)
        if field:
            grouped[key][field] = r.metric_value

    points: list[KinematicsPoint] = []
    for (session_id, protocol_id, t_sec), fields in grouped.items():
        points.append(KinematicsPoint(
            session_id=session_id,
            protocol_id=protocol_id,
            t_sec=t_sec,
            mqi=fields.get("mqi"),
            workspace_volume=fields.get("workspace_volume"),
            smoothness=fields.get("smoothness"),
            velocity=fields.get("velocity"),
            pause_ratio=fields.get("pause_ratio"),
        ))
    return sorted(points, key=lambda p: (p.session_id, p.t_sec))
