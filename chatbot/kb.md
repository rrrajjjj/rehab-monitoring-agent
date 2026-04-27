# Clinical Reference — Stroke Telerehabilitation

## Metrics & Definitions

- **Adherence**: Share of prescribed sessions the patient completes. Measured by days, sessions, and minutes. High = patient sticking with the plan; low = possible fatigue, pain, motivation issues, or scheduling barriers.
- **Delta-DM (ΔDM) / Learning Rate**: After every session the system adjusts difficulty modulators (DMs) to keep exercises challenging. Delta-DM is how much those settings changed. Positive ΔDM = patient is improving and difficulty is rising. Near-zero ΔDM over several sessions = plateau (patient stopped improving on that exercise).
- **Patient–Protocol Fit (PPF)**: Compares the patient's clinical scores (MoCA for cognition, ARAT for arm function) against what each exercise trains. High fit = the exercise targets the patient's deficits; low fit = mismatch.
- **Performance**: Normalized score (0–1) on each session. Reflects how well the patient executed the exercise at the current difficulty.
- **Difficulty**: Normalized difficulty level (0–1) the system set for the session. Rising difficulty with stable performance = good learning.
- **Attention Level**: Triage urgency. 1 = on track, 2 = needs review, 3 = may need intervention.

## Self-Report Scales (0–10)

- **Mood**: Patient's emotional state. Persistent low mood may reduce engagement.
- **Pain**: Higher values indicate more pain. High pain + low adherence often indicates symptom-driven disengagement, not motivational.
- **Energy**: Fatigue indicator. Low energy with declining session duration suggests fatigue-driven dropout.

## Drift Event Types

- **ADHERENCE_DRIFT**: Adherence dropped significantly vs. prior window.
- **PLATEAU**: ΔDM near zero for multiple sessions on an exercise — patient stopped improving.
- **REGRESSION**: Performance declining despite stable or rising difficulty.
- **OVERCHALLENGE**: Difficulty too high relative to performance — patient struggling.
- **UNDERCHALLENGE**: Performance very high at current difficulty — exercise too easy.
- **FATIGUE_CYCLE**: Cyclical pattern of declining performance within sessions or across days.
- **DATA_ISSUE**: Missing sessions, gaps, or inconsistencies in the data.

## Clinical Interpretation Cues

- Low adherence + high pain → likely symptom-driven; consider pain management before protocol changes.
- Plateau + high fit → consider advancing difficulty or switching to a complementary exercise.
- Regression + stable adherence → investigate interference, exacerbation, or medication changes.
- Overchallenge + low mood → risk of disengagement; reduce difficulty or offer encouragement.
- Underchallenge + high adherence → patient ready for progression.
- Fatigue cycle + low energy → consider shorter sessions or redistributing across more days.

## Protocol Catalog

### Motor-Focused
| Protocol | ID | ARAT Targets |
|---|---|---|
| Hockey | 219 | Grasp; Grip; Gross movement |
| Ducks | 209 | Grasp; Grip; Gross movement |
| Balloons (AR) | 231 | Grasp; Grip; Gross movement |
| Driving | 217 | Grasp; Grip; Gross movement |
| Buffet (AR) | 210 | Grasp; Grip; Gross movement |
| Training (AR) | 200 | Grasp; Grip; Gross movement |
| Canvas wiping (AR) | 227 | Grasp; Grip; Gross movement |
| Do the dishes (AR) | 225 | Grasp; Grip; Gross movement |

### Cognitive-Focused
| Protocol | ID | MoCA Targets |
|---|---|---|
| Guess What | 202 | Attention; Visuospatial; Memory; Naming; Language; Abstraction; Orientation |
| Memoseq | 201 | Attention; Memory sequencing; Executive function |
| Pianoseq | 203 | Attention; Motor integration; Processing speed |
| Potpourri | 222 | Attention; Visuospatial; Language; Abstraction |
| Memory (TI) | 224 | Memory; Semantic recall; Categorization |
| Shopping (VE) | 215 | Attention; Memory; Motor planning |
| Balloons Basic | 231 | Memory; Motor integration; Sustained attention |

### Dual-Domain (Motor + Cognitive)
| Protocol | ID | ARAT Targets | MoCA Targets |
|---|---|---|---|
| Blobs | 214 | Grasp; Pinch | Visual-spatial; Executive function; Motor integration; Conceptual abstraction |
| Twister | 223 | Reach; Object manipulation | Attention; Distractor inhibition; Response to stimulus |
| Fishing day (AR) | 221 | Reach; Grasp; Gross movement | Attention; Memory |
| Shelves (AR) | 208 | Reach; Grasp; Gross movement | — |
| Place it | 204 | Reach; Grasp; Pinch | — |
| Alphabet (AR) | 205 | Grasp; Gross movement | Language; Symbol manipulation; Semantic memory |
| Tubes (AR) | 226 | Reach; Grasp; Pinch; Gross movement | Attention; Motor planning |
| Spheroid (AR) | 218 | Grasp; Gross movement | Attention; Memory |

### Diagnostic Only (not for training)
| Protocol | ID | Purpose |
|---|---|---|
| Circle (V) | 220 | Motor baseline |
| Circle (H) | 228 | Motor baseline |
| Quality control (AR) | 230 | Executive function |
| Constellations (AR) | 233 | Memory function |

These diagnostic protocols are administered every ~4 weeks to track baseline function. They are not prescribed as training exercises.
