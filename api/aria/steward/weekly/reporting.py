"""One stable report per run; independent durable publication and Signal delivery."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pymongo import ReturnDocument

from aria.config import settings
from aria.core.logging import scrub_secrets
from aria.integrations.obsidian import ObsidianWriter


def render(run):
    lines = ['# Weekly platform improvement review', '',
             f"Run: `{run['_id']}` · Outcome: **{run['status']}**", '',
             f"Evidence cutoff: {run.get('cutoff')} · Coverage: {run.get('coverage', 'unavailable')}", '',
             '## Evidence coverage', '']
    for name, source in run.get('sources', {}).items():
        lines.append(f"- {name}: {source.get('status')}; inspected {source.get('inspected', 0)}, retained {source.get('retained', 0)}, eligible {source.get('eligible', 'unknown')}. {source.get('error', '')}")
    lines += ['', '## Changes and recommendations', '']
    for finding in run.get('findings', []):
        lines += [f"### {finding['title']}", '',
                  f"**{finding.get('disposition', 'suggested')}** · {finding['confidence']} confidence · {finding['target']}", '',
                  finding['problem'], '', f"Expected behavior: {finding['expected_behavior']}", '',
                  f"Proposed fix: {finding['proposed_fix']}", '', f"Evidence: {', '.join(finding['evidence_ids'])}", '',
                  f"Confidence: {finding['rationale']}", '', f"Contrary evidence: {finding['contrary_evidence'] or 'None identified'}", '',
                  f"Next step: {finding.get('reason') or finding['next_step']}", '']
        if finding.get('verification'):
            lines += [f"Candidate: `{finding['verification']['candidate']}`; trusted checks: {', '.join(finding['verification']['after'])}.", '']
        if finding.get('task_id'):
            lines += [f"Aria task: `{finding['task_id']}`", '']
        if finding.get('watch'):
            lines += [f"Regression watch: {finding['watch']}.", '']
        if finding.get('rollback'):
            lines += ['Rollback: completed; see the recorded restoration receipt.', '']
        if finding.get('deployment'):
            lines += [f"Deployment receipt: `{finding['deployment'].get('receipt', 'recorded in Aria')}`", '']
    if not run.get('findings'):
        lines += ['No justified improvements identified, or the review stopped before findings were available.', '']
    lines += ['## Run details', '', 'Review-phase summary: '+run.get('summary', ''), '',
              f"Stop reason: {run.get('error', 'none')}", '',
              f"Tokens reported: {run.get('usage', {}).get('tokens', 0)}; calls with unknown accounting: {run.get('usage', {}).get('unknown_calls', 0)}. No spending cap; cost is unknown unless reported.", '',
              f"Watched shell: `{run.get('shell_name', 'unavailable')}`", '',
              'Implementation, verification, merge and live deployment are recorded separately in the finding disposition and receipts.']
    return scrub_secrets('\n'.join(lines))


async def publish(db, run):
    report = run.get('report') or render(run)
    path = f"Weekly improvement {run['_id']}.md"
    writer = ObsidianWriter(db=db)
    result = await writer.upsert_managed(path, {'run_id': run['_id'], 'status': run['status']}, report,
        managed_keys=['run_id', 'status'], seed_keys=[], project='ProjectAria', doc_type='Analysis')
    publication = {'status': 'published', 'path': result['path']} if result and result.get('wrote') and result.get('hash_recorded') else {'status': 'failed', 'reason': 'Writer unavailable, provenance failed or human edit preserved'}
    await db.improver_runs.update_one({'_id': run['_id']}, {'$set': {'report': report, 'publication': publication}})
    return publication


async def deliver(db, run):
    """Explicitly requested weekly summary, using the existing Signal daemon.

    Persist send intent first. Ambiguous transport failures are retained as
    unknown instead of risking duplicate messages by blindly sending again.
    """
    if not settings.signal_breakglass_account or not settings.signal_breakglass_recipient:
        await db.improver_runs.update_one({'_id': run['_id']}, {'$set': {'notification': {'status': 'unconfigured'}}})
        return
    claimed = await db.improver_runs.find_one_and_update(
        {'_id': run['_id'], 'notification.status': {'$in': ['pending', 'unconfigured']}},
        {'$set': {'notification.status': 'sending'}}, return_document=ReturnDocument.AFTER)
    if not claimed:
        return
    changed = sum(x.get('disposition') == 'deployed' for x in run.get('findings', []))
    suggested = sum(x.get('disposition') != 'deployed' for x in run.get('findings', []))
    link = f"https://bens-macbook-pro.tailb286a5.ts.net/autonomy?improvement={run['_id']}"
    if run.get('error'):
        findings = f"{suggested} saved" if run.get('findings') else 'unavailable because the review stopped early'
        text = f"Aria weekly review incomplete: {scrub_secrets(run['error'])[:220]}. Findings: {findings}. Coverage: {run.get('coverage', 'unknown')}.\n{link}"
    else:
        text = f"Aria weekly review: {run['status']}. {changed} deployed, {suggested} recommendations or blocked changes. Coverage: {run.get('coverage', 'unknown')}.\n{link}"
    from aria.notifications.signal_rpc import send_weekly_summary
    try:
        await send_weekly_summary(text, run_id=run['_id'])
        state = {'status': 'sent', 'at': datetime.now(timezone.utc)}
    except Exception:
        state = {'status': 'unknown', 'reason': 'Send acknowledgement unavailable; inspect before retrying'}
    await db.improver_runs.update_one({'_id': run['_id']}, {'$set': {'notification': state}})
