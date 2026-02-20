"""LearningRateEstimator - robust trend of difficulty progression."""

from dataclasses import dataclass
from collections import defaultdict

from crtv.domain.models import PatientHistoryBundle
from crtv.features.session_summaries import SessionSignalSummarizer


@dataclass
class LearningRateReport:
    """Output of LearningRateEstimator.compute()."""

    patient_id: int
    protocol_id: int
    learning_rate: float
    confidence: float
    window_length: int
    supporting_sessions: list[int]


def _robust_slope(x: list[float], y: list[float]) -> float:
    """Simple linear slope."""
    if len(x) < 2 or len(y) < 2 or len(x) != len(y):
        return 0.0
    n = len(x)
    sx = sum(x)
    sy = sum(y)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    sx2 = sum(xi * xi for xi in x)
    denom = n * sx2 - sx * sx
    if abs(denom) < 1e-10:
        return 0.0
    return (n * sxy - sx * sy) / denom


class LearningRateEstimator:
    """Robust trend of effective difficulty progression over last N sessions."""

    def __init__(self, n_sessions: int = 10):
        self.n_sessions = n_sessions

    def compute(
        self,
        bundle: PatientHistoryBundle,
        session_summaries: dict[int, object] | None = None,
    ) -> dict[tuple[int, int], LearningRateReport]:
        """
        Per patient per protocol: learning_rate scalar, confidence, window, sessions.
        Uses difficulty progression (end value over sessions) as proxy for effective progression.
        """
        summarizer = SessionSignalSummarizer()
        if session_summaries is None:
            session_summaries = summarizer.summarize(bundle)

        by_protocol: dict[int, list[tuple[int, object]]] = defaultdict(list)
        for sid, s in session_summaries.items():
            if hasattr(s, 'protocol_id') and hasattr(s, 'difficulty_end'):
                by_protocol[s.protocol_id].append((sid, s))

        sessions_by_id = {s.session_id: s for s in bundle.sessions}
        result: dict[tuple[int, int], LearningRateReport] = {}
        for protocol_id, items in by_protocol.items():
            items_sorted = sorted(
                items,
                key=lambda x: sessions_by_id[x[0]].start_time if x[0] in sessions_by_id else "",
            )
            items_sorted = items_sorted[-self.n_sessions:]
            if len(items_sorted) < 2:
                result[(bundle.patient_id, protocol_id)] = LearningRateReport(
                    patient_id=bundle.patient_id,
                    protocol_id=protocol_id,
                    learning_rate=0.0,
                    confidence=0.0,
                    window_length=len(items_sorted),
                    supporting_sessions=[x[0] for x in items_sorted],
                )
                continue

            seq = [i for i in range(len(items_sorted))]
            eff_difficulty = []
            for _, summ in items_sorted:
                d = getattr(summ, 'difficulty_end', {}) or getattr(summ, 'difficulty_mean', {})
                if d:
                    eff_difficulty.append(sum(d.values()) / len(d) if d else 0.0)
                else:
                    eff_difficulty.append(0.0)
            lr = _robust_slope(seq, eff_difficulty)
            confidence = min(1.0, len(items_sorted) / self.n_sessions)
            result[(bundle.patient_id, protocol_id)] = LearningRateReport(
                patient_id=bundle.patient_id,
                protocol_id=protocol_id,
                learning_rate=lr,
                confidence=confidence,
                window_length=len(items_sorted),
                supporting_sessions=[x[0] for x in items_sorted],
            )
        return result
