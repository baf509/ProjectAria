"""
ARIA - Steward

Purpose: the loops that make ARIA the steward of a set of chartered projects,
rather than a substrate that only acts when something drives it.

Design: /home/ben/Obsidian/vault/ProjectAria/Planning/ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md

Components (each a worker registered in main.py's lifespan, each OFF by default
until its phase gate passes):

- ``service.StewardWorker``   — per chartered project: charter -> gap -> next action
- ``research.ResearchPlanner`` — proactive, deduplicated, budgeted research per project
- ``supervisor.MetaSupervisor`` — stuck detection and the escalation ladder across
  every agent kind, cloud and local
- ``outcomes.OutcomeScorer``   — the labels everything else is measured by
- ``improve.Improver``         — eval-gated self-improvement proposals
- ``pi_transcript``            — parser for pi's structured session JSONL, which is
  where a local coding agent's tool calls and token usage actually live

The division of labour this package assumes: **Hermes is Ben's channel, ARIA is
the steward.** Any loop that runs while Ben is not talking lives here; any text
he reads or writes goes through Hermes (short, typed) or the Obsidian vault
(long, editable). Nothing in this package talks to a human directly — it raises
through ``aria.notifications`` and writes plans through the vault.
"""
