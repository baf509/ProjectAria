You are auditing a Linear ticket to decide whether the work it asks for has ALREADY been implemented, based on evidence gathered from the machine the work would have happened on.

TICKET:
Title: {title}
Description: {description}

EVIDENCE (recent activity for the project this ticket belongs to):
{evidence}

Decide whether this ticket's work is already done. Be conservative: only report high confidence when the evidence CONCRETELY shows the described work happening (matching commits, matching design docs, matching activity notes). Vague thematic overlap is NOT implementation.

Respond with ONLY a JSON object, no markdown fence:
{{"implemented": true|false, "confidence": 0.0-1.0, "evidence": "one or two sentences citing the specific evidence items that support your verdict"}}
