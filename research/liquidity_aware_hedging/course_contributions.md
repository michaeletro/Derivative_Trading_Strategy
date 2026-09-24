# Course requirements versus proposed contributions

## Source basis
The user supplied `AMS518_Syllabus_Fall_2026.pdf` and `AMS_520_Fall2026 (1).pdf`.
The mapping below uses those documents, not private lecture notes or detailed
project instructions that have not been supplied. The PDFs are not redistributed
in the repository; meeting links/passcodes and other unrelated details are omitted.

| Source | Supported requirement | Proposed evidence |
|---|---|---|
| AMS 518, p.2, Project | Every student does and presents a numerical case study using learned techniques; peer reviews/presentations | Distinct CVaR optimization case study, derivation, solver tests, numerical results and review |
| AMS 518, p.2, Grades | Project 40%; project review 5% | Separate completion checklist; not a predicted grade |
| AMS 518, p.2, Objectives/Topics | Constrained systems, transaction costs, hedging, replication, CVaR regression/estimation | Explicit loss/constraints, convexity qualifications, numerical hedge comparison |
| AMS 518, p.3, AI Policy | Homework without AI; project AI use clearly acknowledged | `AI_USAGE.md` for this project only; no homework solutions included |
| AMS 520, p.2, Projects/Deliverables | Groups up to three, equal contribution assumed; interactive Colab/Jupyter report; three presentations; GitHub code/docs, no data credentials | Phase notebooks, presentation material, contribution ledger and credential-free code |
| AMS 520, pp.2–3, Outline/Presentations | Regression/generalization/optimization and sequential learning; Phase I Lecture10, Phase II Lecture22, Phase III final-exam day | Literature/data/cleaning, then EDA/first empirical results, then final held-out evaluation |

## Decisions requiring instructors, not software
- Permission to share pre-existing code and datasets across two courses, with
  distinct assessed analyses: **not yet established**.
- AMS 520-specific AI-use conditions: **not specified in the supplied syllabus**.
- Detailed AMS 518 project requirements referenced by the syllabus: **not supplied**.
- Group membership and actual individual contributions: **not supplied**. Do not
  invent teammates, equal contributions already completed, or approval dates.

## Pre-existing versus new
Baseline: `bca275c8c9886448d82d8215ac0a846f764264fd`, the read-only broker/research
platform and local browser sign-in. This foundation adds proposed course framing,
direct displayed-depth recording/reconstruction/export and a Phase I notebook.
CVaR optimization, fitted liquidity prediction and LSV are planned, not completed.

## Approval request draft (not sent)
I propose using a shared research platform for two distinguishable course analyses.
AMS 518 would assess constrained CVaR hedging with transaction costs; AMS 520 would
assess order-book liquidity prediction, temporal validation and its downstream
value for hedge decisions. I would disclose shared/pre-existing infrastructure,
new course contributions, actual individual contributions and AI assistance.
May I use the common repository/data while submitting distinct reports and
presentations? Please also clarify any additional AI-use or group requirements.

## Contribution ledger
Before submission, add dated entries with contributor, code/analysis/writing
scope, validation performed, and reviewed commit. AI-generated implementation is
not evidence that the student has independently understood or validated it.
