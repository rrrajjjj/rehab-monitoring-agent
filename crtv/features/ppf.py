"""PPFComputer - Patient-Protocol Fit from baseline assessments."""

from dataclasses import dataclass

from crtv.domain.models import Assessment, ProtocolInfo


@dataclass
class PPFReport:
    """Output of PPFComputer.compute()."""

    patient_id: int
    ppf: dict[int, float]  # protocol_id -> fit in [0,1]
    needs_profile: dict[str, float] = None  # domain -> inferred need

    def __post_init__(self):
        if self.needs_profile is None:
            self.needs_profile = {}


ASSESSMENT_KEYS = {"Fugl-Meyer": "motor", "MoCA": "cognitive", "PHQ-9": "mood", "GAD-7": "anxiety"}


class PPFComputer:
    """Compute patient-protocol fit from baseline assessments and protocol targets."""

    def compute(
        self,
        baseline_assessments: list[Assessment],
        protocol_catalog: dict[int, ProtocolInfo],
    ) -> PPFReport:
        """
        Map assessments to needs profile (motor, cognitive, mood).
        Score ppf[protocol_id] by overlap of needs and protocol targets.
        """
        needs: dict[str, float] = {}
        for a in baseline_assessments:
            domain = ASSESSMENT_KEYS.get(a.type)
            if domain:
                score = a.score
                if a.type == "Fugl-Meyer":
                    needs["motor"] = min(1.0, score / 66.0)
                elif a.type == "MoCA":
                    needs["cognitive"] = min(1.0, score / 30.0)
                elif a.type == "PHQ-9":
                    needs["mood"] = 1.0 - min(1.0, score / 27.0)
                elif a.type == "GAD-7":
                    needs["anxiety"] = 1.0 - min(1.0, score / 21.0)
        if not needs:
            needs = {"motor": 0.5, "cognitive": 0.5}

        ppf: dict[int, float] = {}
        for pid, info in protocol_catalog.items():
            targets = set(info.targets or [])
            overlap = 0.0
            for t in targets:
                t_lower = t.lower()
                for domain, val in needs.items():
                    if domain in t_lower or t_lower in domain:
                        overlap += val
            ppf[pid] = min(1.0, overlap / max(1, len(targets))) if targets else 0.5
        patient_id = baseline_assessments[0].patient_id if baseline_assessments else 0
        return PPFReport(patient_id=patient_id, ppf=ppf, needs_profile=needs)
