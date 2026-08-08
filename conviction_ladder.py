"""
Investor Conviction Ladder — framework definitions.

The stage definitions and deduction rubric below are lifted from the
TEN Capital conviction-ladder plugin (`skills/conviction-ladder/references/
scoring-rubric.md` and `skills/run-full-analysis/references/
conviction-ladder-stages.md`) so the analyzer scores against the same rubric
the manual workflow uses.
"""

from __future__ import annotations

# --- Stage names, in canonical order -----------------------------------------

STAGE_NAMES = [
    "Pattern Interrupt",
    "Problem Belief",
    "Solution Credibility",
    "Founder Credibility",
    "Market Upside",
    "Differentiation & Defensibility",
    "Traction Validation",
    "Risk Framing",
    "Deal Logic",
    "Personal Commitment",
]

MAX_SCORE = 5 * len(STAGE_NAMES)  # 50


# --- Score -> status label (left-hand summary table) --------------------------

STATUS_LABELS = {
    5: "Investor-Ready",
    4: "Strong",
    3: "Adequate",
    2: "Weak",
    1: "Fails",
}


def status_for(score: int) -> str:
    return STATUS_LABELS.get(score, "—")


# --- Overall score -> verdict band -------------------------------------------

VERDICT_BANDS = [
    (43, "Strong Conviction"),
    (36, "Advance with Conditions"),
    (28, "Borderline"),
    (0, "Pass"),
]


def verdict_for(total: int) -> str:
    for floor, label in VERDICT_BANDS:
        if total >= floor:
            return label
    return "Pass"


# --- The rubric handed to the model ------------------------------------------

RUBRIC = """\
# Investor Conviction Ladder — Stage Definitions & Scoring Rubric

Score each stage 1-5. Total maximum: 50.

- 5 - Fully addressed; investor-ready; all data points present and sourced
- 4 - Strong; minor gaps that are non-blocking for the investment decision
- 3 - Adequate; material gaps exist but the core thesis is present
- 2 - Weak; critical information missing or unconvincing on examination
- 1 - Absent or fundamentally inadequate for investment evaluation

## Stage 1 - Pattern Interrupt
Does the deck immediately create attention, communicate positioning, and tease
the problem - without leading with legal disclaimers?
Key signals: unique opening hook, clear "why us / why now" on the cover or
slide 2, legal slide at the back, problem teaser within the first two slides.
Deduct for: no hook (-1), legal slide on cover (-1), no positioning statement
(-1), problem buried past slide 3 (-1).

## Stage 2 - Problem Belief
Does the investor believe the problem is real, large, urgent, and unsolved by
current approaches?
Key signals: quantified prevalence/cost with sources, urgency trigger
(regulatory change, market shift, societal pressure), evidence of incumbent
failure, payor perspective (who pays, how much), distinct buyer segments.
Deduct for: TAM unsourced (-1), no urgency signal (-1), no payor data (-1),
no incumbent failure evidence (-1), buyer segmentation absent (-1).

## Stage 3 - Solution Credibility
Does the investor believe the solution works, can be built, and has a clear
path to approval or market entry?
Key signals: mechanism of action clearly explained, preclinical or technical
validation data, human/customer validation data, product specification,
regulatory pathway identified with timeline, manufacturing readiness.
Deduct for: MOA unclear (-1), no validation data (-1), no human data for a
clinical-stage valuation (-1), no regulatory pathway (-1), CMC/manufacturing
absent at a scale-relevant stage (-1).

## Stage 4 - Founder Credibility
Does the investor believe this specific team can execute the next milestone
and exit?
Key signals: named founding scientist or domain expert with verifiable
credentials, CEO with a stage-appropriate exit track record (named companies
or deals), full-time operational lead for the next phase, key execution
partner named, leadership gaps acknowledged, specific KOL endorsements.
Deduct for: CEO bio vague / no specific exits (-1), no founding scientist (-1),
full-time status unclear (-1), no execution partner (-1), gaps unacknowledged
(-1), KOL advisors unnamed or unquoted (-1).

## Stage 5 - Market Upside
Does the investor see a path to a return that justifies the risk?
Key signals: TAM with methodology and source, bottoms-up SAM logic
(units x price x penetration), revenue model or transaction structure,
near-term value creation events, comparable exits with multiples.
Deduct for: TAM asserted without methodology (-1), no SAM build-up (-1),
no revenue model (-1), no near-term catalysts (-1), no comparable exits (-1).

## Stage 6 - Differentiation & Defensibility
Does the investor believe the moat is real and durable?
Key signals: IP summary (type, jurisdiction, expiry), competitive matrix or
differentiation statement, composition-of-matter vs. method/use distinction,
regulatory exclusivity or structural moat (orphan, NCE, trade secret, network
effects, switching costs), design-around risk acknowledged.
Deduct for: IP type not specified (-1), no jurisdiction/expiry (-1), no
competitive matrix (-1), exclusivity not addressed (-1), design-around risk
ignored (-1).

## Stage 7 - Traction Validation
Does the investor see evidence that the market, regulators, and partners are
already validating the company?
Key signals: non-dilutive funding or grants secured, product/clinical
validation data from the target population, customer or partner engagement
(even under NDA), named KOL endorsements, round status with a named lead.
Deduct for: no grants or non-dilutive capital (-1), no validation data from
the target population (-1), no external engagement (-1), no named lead
investor (-1), KOL advisors unnamed (-1).

## Stage 8 - Risk Framing
Does the investor understand the primary risks and believe management has
thought about mitigation?
Key signals: dedicated risk/mitigation slide, primary technical or execution
risk named, regulatory risk addressed, commercial/go-to-market risk addressed,
financial risk (runway, note maturity, funding gap) addressed, mitigation
strategy per risk.
Deduct for: no dedicated risk slide (-1), commercial risk absent (-1),
financial risk absent (-1), regulatory risk absent (-1), no mitigations (-1),
note maturity not disclosed (-1). This stage is frequently the lowest-scored -
treat its absence as a red flag.

## Stage 9 - Deal Logic
Does the deal structure make sense given stage, traction, and milestones?
Key signals: valuation methodology (comparables, DCF, milestone-based), use of
proceeds with a line-item breakdown, instrument terms (rate, discount,
maturity, conversion trigger), milestone timeline reconciled with runway,
current round status and remaining gap.
Deduct for: valuation unanchored (-1), no comparable transactions (-1), no
use-of-proceeds detail (-1), note maturity absent (-1), milestone/runway gap
not reconciled (-1).

## Stage 10 - Personal Commitment
Does the investor believe the founders are personally committed to this
outcome?
Key signals: founder personal or financial investment disclosed, "why now"
narrative (especially for technology with prior history or failed attempts),
named acquirer or exit path with rationale, mission or personal connection to
the problem articulated.
Deduct for: no personal investment disclosed (-1), no "why now" narrative (-1),
exit path is a category not a name (-1), no mission or personal story (-1).
This stage is frequently the most underdeveloped - treat its absence as a
signal about founder investor-readiness.
"""


SYSTEM_PROMPT = """\
You are a venture investor at TEN Capital running the Investor Conviction \
Ladder framework on a startup pitch deck.

Score all ten stages against the rubric you are given, then diagnose where \
conviction breaks down.

Rules:
- Base every score, evidence note, and gap solely on what is in (or absent \
from) the deck. Do not infer data that is not shown, and do not import outside \
market knowledge as if it came from the deck.
- Cite the specific claim, figure, or slide that drives each score in the \
evidence field. Quote short phrases from the deck where possible.
- If a data point is present but unsourced, treat it as partial and deduct.
- Absence of evidence is a real finding. Say "No X found in the deck" rather \
than softening it.
- Be direct. Investors use this to decide whether to take a meeting.
"""


ANALYSIS_PROMPT = """\
Run a full Investor Conviction Ladder analysis on the attached pitch deck.

{rubric}

---

For each of the ten stages produce:
- score: 1-5, applying the deduction guide above
- evidence: what the deck actually shows for this stage (specific: figures, \
named people, quoted claims). 1-2 sentences.
- gap: what is missing or unconvincing. 1-2 sentences. If nothing is missing, \
say so plainly.
- question: the single most useful question an investor should ask the \
founders to close this gap in a live meeting. Open-ended, specific to this gap.
- fix: the concrete change to the deck that would raise this score. Name the \
slide or the content to add.

Then produce the deal momentum diagnosis:
- conviction_breaks_at: the earliest stage where an investor's conviction \
breaks, named as "Stage N (Name)", plus one sentence on why, plus one sentence \
on which later stage compounds the doubt.
- partner_meeting: whether this deck earns a partner meeting as it stands.
- biggest_red_flag: the single most disqualifying thing about this deck.
- highest_leverage: the one change that would move conviction the most.
- increase_p_invest: the specific additions that would most raise the \
probability of investment.

Also extract company_name exactly as the deck presents it.

The report is a single page, so keep every field tight enough to read at a \
glance: evidence, gap, question, and fix should each be under 200 characters, \
and each diagnosis field under 300 characters. Cut adjectives before you cut \
specifics - a short sentence naming a real figure beats a long one hedging.
"""


# --- Structured-output schema -------------------------------------------------

STAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "number": {
            "type": "integer",
            "enum": list(range(1, 11)),
            "description": "Stage number, 1-10.",
        },
        "name": {
            "type": "string",
            "enum": STAGE_NAMES,
            "description": "Stage name, exactly as listed.",
        },
        "score": {
            "type": "integer",
            "enum": [1, 2, 3, 4, 5],
            "description": "Score for this stage.",
        },
        "evidence": {"type": "string"},
        "gap": {"type": "string"},
        "question": {"type": "string"},
        "fix": {"type": "string"},
    },
    "required": ["number", "name", "score", "evidence", "gap", "question", "fix"],
    "additionalProperties": False,
}

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "conviction_breaks_at": {"type": "string"},
        "partner_meeting": {
            "type": "string",
            "enum": ["Yes", "Likely", "Unlikely", "No"],
        },
        "biggest_red_flag": {"type": "string"},
        "highest_leverage": {"type": "string"},
        "increase_p_invest": {"type": "string"},
    },
    "required": [
        "conviction_breaks_at",
        "partner_meeting",
        "biggest_red_flag",
        "highest_leverage",
        "increase_p_invest",
    ],
    "additionalProperties": False,
}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "stages": {"type": "array", "items": STAGE_SCHEMA},
        "diagnosis": DIAGNOSIS_SCHEMA,
    },
    "required": ["company_name", "stages", "diagnosis"],
    "additionalProperties": False,
}


def build_prompt() -> str:
    return ANALYSIS_PROMPT.format(rubric=RUBRIC)


def normalize(analysis: dict) -> dict:
    """Sort stages into canonical order, force canonical names, add the total.

    The schema constrains the model to valid stage numbers and names, but not
    to emitting each one exactly once - so validate that here rather than
    rendering a report with a missing or duplicated stage.
    """
    stages = analysis.get("stages", [])
    by_number = {}
    for stage in stages:
        by_number[stage["number"]] = stage

    missing = [n for n in range(1, 11) if n not in by_number]
    if missing:
        raise ValueError(
            f"Model returned {len(by_number)} distinct stages; missing "
            f"stage(s) {missing}. Re-run the analysis."
        )

    ordered = []
    for number in range(1, 11):
        stage = dict(by_number[number])
        stage["name"] = STAGE_NAMES[number - 1]  # canonical, ignore drift
        stage["status"] = status_for(stage["score"])
        ordered.append(stage)

    analysis["stages"] = ordered
    analysis["overall_score"] = sum(s["score"] for s in ordered)
    analysis["verdict"] = verdict_for(analysis["overall_score"])
    return analysis
