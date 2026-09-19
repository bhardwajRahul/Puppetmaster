from __future__ import annotations

"""Jev question text, endpoint, and thresholds. Tune only this file."""

MODEL = "~typesafe/jev-latest"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
TIMEOUT_SECONDS = 2.0

CONFLICT_THRESHOLD = 0.50
ALREADY_ANSWERED_THRESHOLD = 0.70
ADMISSION_THRESHOLD = 0.50
STOP_THRESHOLD = 0.50

PAIR_CAP = 20
CLAIM_CHARS = 700
EVIDENCE_LOCI = 6
GOAL_CHARS = 700
TOKEN_BUDGET = 30000
CHARS_PER_TOKEN = 4

CONTRADICT_INSTRUCTIONS = (
    "Do these two FINDING claims contradict each other about the same "
    "repository fact, such that a later reader cannot treat both as true?"
)

ALREADY_ANSWERED_INSTRUCTIONS = (
    "Do these existing claims already answer the new goal well enough that "
    "a new analysis swarm would be redundant?"
)

ADMISSION_INSTRUCTIONS = (
    "Is this claim a repository fact with evidence, rather than process "
    "or prompt restatement?"
)

STOP_INSTRUCTIONS = (
    "Is the job goal still unanswered given these claims?"
)
