"""Bounded model-setting contracts, independent of candidate code and launchers.

These are eligibility checks, not authority to restart a model. A controller must
also own admission, validate the live preimage, and pass a host canary/rollback.
Runtime/weights, context geometry, GPU selection and reasoning stay fixed.
"""
from __future__ import annotations

import re
import shlex


MODEL_PROFILES = {
    'red-paro-int5': {
        'source': 'red-r9700/paro-int5-isolated/profile.env',
        'format': 'env',
        'settings': {'MAXSEQS': (1, 8), 'CHUNK': (4096, 8192)},
        'choices': {'CHUNK': {4096, 8192}},
        'host': 'red-linux',
        'model': 'Red-Qwen3.8-27B-PARO-INT5',
        'runtime': 'radiance',
        'unit': 'red-paro-int5-isolated.service',
    },
    'corsair-ninfer': {
        'source': 'ninfer3090/run-serve.sh',
        'format': 'argv',
        'settings': {'--max-pending-requests': (1, 2)},
        'choices': {},
        'host': 'corsair-ai',
        'model': 'NInfer-3090-Qwen3.8-27B',
        'runtime': 'ninfer',
        'unit': 'ninfer-3090.service',
    },
}


def validate_model_change(profile_id: str, before: str, after: str) -> dict:
    """Only numeric reductions in existing approved fields; never execute text.

    All other source text/tokens must match the reviewed preimage. The reduction
    policy bounds resource growth, but still requires measured quality/latency
    gates: smaller batches and queues can also make a service worse.
    """
    profile = MODEL_PROFILES[profile_id]
    changes = {}
    if profile['format'] == 'env':
        old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
        if len(old) != len(new):
            raise ValueError('Launcher structure changed')
        seen = set()
        for index, (a, b) in enumerate(zip(old, new)):
            match = re.fullmatch(r'([A-Z][A-Z0-9_]*)=([0-9]+)(\r?\n)?', a)
            if not match or match[1] not in profile['settings']:
                if a != b:
                    raise ValueError('Unapproved launcher content changed')
                continue
            name = match[1]
            if name in seen:
                raise ValueError('Duplicate setting: '+name)
            seen.add(name)
            replacement = re.fullmatch(re.escape(name)+r'=([0-9]+)(\r?\n)?', b)
            if not replacement:
                raise ValueError('Setting must be a literal integer: '+name)
            if a != b:
                changes[name] = (int(match[2]), int(replacement[1]))
            new[index] = a
        if seen != set(profile['settings']):
            raise ValueError('Missing registered setting')
    else:
        old, new = (shlex.split(text.replace('\\\n', ''), comments=True)
                    for text in (before, after))
        if len(old) != len(new):
            raise ValueError('Launcher structure changed')
        restored = after
        for name in profile['settings']:
            if old.count(name) != 1 or new.count(name) != 1:
                raise ValueError('Missing or duplicate setting: '+name)
            index = old.index(name)+1
            if index >= len(old) or new[index-1] != name:
                raise ValueError('Setting moved: '+name)
            if not old[index].isdigit() or not new[index].isdigit():
                raise ValueError('Setting must be a literal integer: '+name)
            if old[index] != new[index]:
                changes[name] = (int(old[index]), int(new[index]))
            new[index] = old[index]
            pattern = re.compile(r'(?<!\S)('+re.escape(name)+r'[ \t]+)([0-9]+)(?=\s|$)')
            old_matches, new_matches = list(pattern.finditer(before)), list(pattern.finditer(restored))
            if len(old_matches) != 1 or len(new_matches) != 1:
                raise ValueError('Ambiguous literal setting: '+name)
            match = new_matches[0]
            restored = restored[:match.start(2)]+old_matches[0][2]+restored[match.end(2):]
        if new != old:
            raise ValueError('Unapproved launcher content changed')
        if restored != before:
            raise ValueError('Launcher text outside numeric settings changed')
    if not changes:
        raise ValueError('No approved settings changed')
    for name, (a, b) in changes.items():
        low, high = profile['settings'][name]
        if not low <= b <= high or b >= a:
            raise ValueError('Only bounded reductions are qualified: '+name)
        if name in profile['choices'] and b not in profile['choices'][name]:
            raise ValueError('Unqualified setting value: '+name)
    return changes


def promotion_blockers(evidence: dict) -> list[str]:
    """Common deployment gate. Absent evidence never counts as a passing test.

    The operator controller produces this attestation; candidate-supplied booleans
    are never a trusted attestation. Kept explicit so target onboarding cannot
    mistake static tests or a 200 response for production qualification.
    """
    required = (
        'scope_passed', 'baseline_failure_reproduced', 'candidate_checks_passed',
        'live_preimage_matches', 'identity_matches', 'admission_held',
        'inflight_drained', 'consumer_canary_passed', 'rollback_passed',
        'recovery_passed', 'later_edit_preserved',
    )
    reasons = [name for name in required if evidence.get(name) is not True]
    if type(evidence.get('samples')) is not int or evidence['samples'] < 10:
        reasons.append('insufficient_samples')
    return reasons
