# Eodyne Rehabilitation Monitoring — User Guide

This app helps rehabilitation clinicians spot which stroke patients on the NEST platform need attention this week. It also includes two chatbots: one for clinicians to ask about a specific patient, one for patients and caregivers to ask general stroke-recovery questions.

Open the app at the URL your team provided (typically `http://localhost:8001` in local development).

---

## The landing page

Three tiles. Click one to go there.

- **Demo** — a curated set of patients whose weekly triage cards have been pre-computed. Use this to show the product.
- **All patients** — search any patient in the connected database and generate a card for any week on demand.
- **Ops** — admin area. Edit chatbot prompts, swap models, manage the knowledge base.

---

## Demo

The demo has two modes — switch using the buttons in the top-right corner.

### Clinician View

- **Left: Patient list.** Each row is a patient. The colored dot is an attention level: red = needs review now, amber = worth looking at, grey = routine.
- Click a patient to open their detail page.

On the detail page:

- **Week tabs** across the top. Each tab is one weekly checkpoint. Most recent is on the right. Click to switch weeks.
- **Observations panel (left).** Findings the system surfaced for this week. Color matches the attention level. Click an observation to highlight its supporting plots on the right.
- **Evidence panel (right).** Charts showing the metrics behind the observations — adherence, session performance, self-reports over time.
- **"Ask about this card" button (top right).** Opens a chat drawer where you can ask natural-language questions about this specific patient and week ("why is this patient flagged?"). The clinician chatbot only uses the data in the card — it won't speculate.
- **Back to patients** returns to the list.

### Patient Check-ins

A patient-friendly weekly update generated from the same data.

- **Left: Patient list.** Same patients as the clinician view.
- Click one to open their check-ins.

On the detail page:

- **Week tabs** — same idea, most recent on the right.
- **Check-in body.** Written for the patient, not the clinician. Typical sections: what went well this week, what to work on, encouragement.
- **Green "Ask" button (bottom-right corner).** Opens the stroke-care companion. This chatbot is for patients and caregivers — it answers general questions about stroke recovery ("my dad cries a lot, is that normal?", "how do I help with transfers?") and cites the guideline it's drawing from ("the World Health Organization suggests…"). It will not give medical advice.

  **Standalone vs. Integrated** — at the top of the chat window:

  - **Standalone** (default) — the bot holds a generic conversation. No patient data is shared with it. Use this when a caregiver has a general question.
  - **Integrated** — a dropdown appears with every patient in the check-in list. Pick one and the bot assumes it's talking with that specific patient (or their caregiver) and will weave their weekly summary into its replies. Changing the selected patient starts a fresh conversation so the framing lines up.

  **One greeting per session.** The welcome message appears once when you first open the chat. Minimizing (×) and reopening does **not** re-greet — the conversation picks up where you left it.

  **Reset** — the small Reset button in the header clears the conversation and re-greets. Use it to start over or when switching to a new topic.

---

## All patients

For when the patient you care about isn't in the curated demo set.

1. **Search.** Type a patient ID (like `3639`) or part of their username (like `ana`). Click Search. Matches appear below.
2. **Pick a patient.** Click a result. A grid of that patient's active weeks appears.
3. **Pick a week.** Click one of the date chips. Most recent is at the top.
4. **Click Generate card.** The system runs the full triage pipeline for that patient and week. Takes 10–30 seconds.
5. The new card appears in the **Generated cards** list below.

Each generated card shows patient ID, week ending date, a one-line headline, and a disposition badge (e.g. TRIAGE = needs review). Click a card to expand the detail panel. Click **Delete** to remove it.

Cards here are kept separate from the curated demo set, so you can experiment without affecting the demo.

---

## Ops

For the team maintaining the chatbots and the triage pipeline. Three tabs at the top.

### Clinician chat tab

Controls the chatbot that appears on the clinician detail page.

- **System prompt** — the instructions the AI reads before every reply. Edit to change its tone, scope, or rules.
- **Knowledge base** — extra reference text appended to the system prompt (metric definitions, shorthand glossaries, etc.).
- **Model** — which LLM to use.
- **Version label (optional)** — a note on this change ("tighter tone", "fixed typo").
- **Save as new version** — creates a new version and makes it active immediately. Old versions are kept.
- **Version history** — every saved version with its date. Click **Preview** to load a version into the editor without activating it. Click **Revert** to make an older version active again.

### Patient chat tab

Controls the stroke-care companion on the patient view. Same layout as the clinician tab but without a Knowledge base field (the patient chatbot uses the PDF library below instead of inline reference text).

Below the editor:

- **Knowledge base panel** — lists every PDF currently in the library along with a human-readable source label (WHO, ASA, etc.), file size, and whether it's been indexed yet.
  - **Upload PDFs** — add one or more PDF files. New files show a "pending" badge until you reindex.
  - **Delete** (next to a file) — removes the PDF. Its content stays in the index until you reindex.
  - **Reindex** — rebuilds the searchable index. Unchanged files are skipped automatically (fast). Tick **force** to rebuild everything from scratch (slow, uses API credits).
  - A progress log streams during indexing so you can see what's happening. When it finishes, the summary shows how many chunks were created.

- **Tests panel** — a small regression suite of ground-truth Q/A pairs you can score any version against.
  - **Add case** — enter a question and the ground-truth answer you expect the bot to cover, then click **Add case**. Cases are shared across all versions.
  - **Run tests** (next to a version in the Version history) — sends every case through that version's prompt + model, then asks a judge LLM to score each answer 1-5 on tone, usefulness, and whether the ground-truth facts are included. The average appears next to the version (green ≥ 4, amber 3, red < 3). Re-running overwrites the previous score for that version.
  - **Inspect** — opens the full result set for that run: question, ground truth, LLM answer, and the rating. Change the rating with the dropdown if you disagree with the judge; the average updates immediately and is flagged as `manual`.
  - Only one run can be in flight at a time.

### Triage prompt tab

Controls the system prompt used to generate the clinician-facing triage cards (the observations, headline, and disposition that appear in the Demo and All Patients views).

- **Triage card generation prompt** — the full instructions the triage LLM reads before every card. This prompt defines how findings are phrased, the attention levels, the plot reference IDs, and the expected JSON output. Edit carefully — a malformed prompt can break card generation.
- **Version label (optional)** — a short note ("tightened plateau wording", "added fatigue cue").
- **Save as new version** — creates a new version and makes it active. Old versions are kept.
- **Version history** — Preview to load a version into the editor; Revert to make it active again.

Changes take effect on the next card that is generated. Previously generated cards are not regenerated automatically (clear `.llm_cache/` if you want fresh output on a cached patient/week).

---

## Quick reference — who uses what

| Situation | Go to |
|---|---|
| Show the product to a stakeholder | **Demo** |
| Review a specific patient you heard about | **All patients** → search |
| Ask "why is this patient flagged?" | **Demo → Clinician View → Ask about this card** |
| A caregiver asks a general recovery question | **Demo → Patient Check-ins → Ask button** |
| Tighten the clinician chatbot's tone | **Ops → Clinician chat** |
| Add a new guideline PDF to the patient chatbot | **Ops → Patient chat → Upload PDFs → Reindex** |
| Change how triage cards are worded | **Ops → Triage prompt** |
| Compare prompt versions objectively | **Ops → Patient chat → Tests panel**, then **Run tests** on each version |
| Revert a prompt change that made things worse | **Ops → (matching tab) → Version history → Revert** |

---

## Troubleshooting

- **"Could not find that patient"** on /all — the app is connected to the wrong data source. Ask engineering to verify `CRTV_DATA_BACKEND` and DB credentials.
- **Chat replies "CRTV_OPENAI_API_KEY not set"** — the OpenAI key is missing or expired in the server config.
- **Reindex takes forever** — large PDFs with many pages can take several minutes. Progress streams in real time; if the log stops updating, something stalled. Check server logs.
- **A new PDF shows "pending" forever** — click Reindex. Uploading alone doesn't embed; indexing is a separate step.
- **Chatbot gives a vague answer** — usually means the question didn't retrieve a good passage. Rephrase more concretely, or add a PDF that covers that topic and reindex.
