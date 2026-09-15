"""Adversarial cases for model profile and deployment qualification boundaries."""
import pytest

from aria.steward.weekly.platform_contracts import (
    MODEL_PROFILES, promotion_blockers, validate_model_change,
)


RED = 'IMAGE=pinned@sha256:fixed\nMAXLEN=262144\nMAXSEQS=8\nCHUNK=8192\nGPUS=0,1\n'
NINFER = '#!/bin/bash\nexec /pinned/ninfer-serve /weights/model.ninfer \\\n --host 127.0.0.1 --max-context 96256 --max-pending-requests 2 --vision\n'


def test_model_reduction_requires_actual_bounded_change():
    assert validate_model_change('red-paro-int5', RED, RED.replace('MAXSEQS=8', 'MAXSEQS=4')) == {'MAXSEQS': (8, 4)}
    assert validate_model_change('corsair-ninfer', NINFER, NINFER.replace('requests 2', 'requests 1')) == {'--max-pending-requests': (2, 1)}
    with pytest.raises(ValueError, match='No approved'):
        validate_model_change('red-paro-int5', RED, RED)


@pytest.mark.parametrize('replacement', [
    ('MAXLEN=262144', 'MAXLEN=524288'),
    ('GPUS=0,1', 'GPUS=1,0'),
    ('MAXSEQS=8', 'MAXSEQS=16'),
    ('CHUNK=8192', 'CHUNK=6000'),
    ('MAXSEQS=8', 'MAXSEQS=$(touch /tmp/should-not-exist)'),
    ('MAXSEQS=8', 'MAXSEQS=4; echo injected'),
    ('IMAGE=pinned', 'IMAGE=latest'),
])
def test_red_rejects_resource_runtime_and_shell_changes(replacement):
    with pytest.raises(ValueError):
        validate_model_change('red-paro-int5', RED, RED.replace(*replacement))


@pytest.mark.parametrize('candidate', [
    NINFER.replace('--max-context 96256', '--max-context 131072'),
    NINFER.replace('127.0.0.1', '0.0.0.0'),
    NINFER.replace('requests 2', 'requests 1; curl malicious.invalid'),
    NINFER.replace('requests 2', 'requests -1'),
    NINFER.replace('requests 2', 'requests 1 --max-pending-requests 2'),
    NINFER.replace('--vision', ''),
    NINFER.replace('--host', '\n--host'),
])
def test_ninfer_rejects_unreviewed_flags_and_executable_changes(candidate):
    with pytest.raises(ValueError):
        validate_model_change('corsair-ninfer', NINFER, candidate)


def test_reductions_do_not_become_blanket_permission_to_raise_later():
    lowered = RED.replace('MAXSEQS=8', 'MAXSEQS=4')
    with pytest.raises(ValueError, match='reductions'):
        validate_model_change('red-paro-int5', lowered, RED)


def test_health_alone_cannot_qualify_deployment_and_bool_is_not_sample_count():
    assert 'admission_held' in promotion_blockers({'healthy': True, 'samples': 100})
    evidence = {key: True for key in promotion_blockers({}) if key != 'insufficient_samples'}
    evidence['samples'] = 10
    assert promotion_blockers(evidence) == []
    for key in list(evidence):
        invalid = {**evidence, key: False}
        assert promotion_blockers(invalid)
    assert promotion_blockers({**evidence, 'samples': True}) == ['insufficient_samples']
    assert promotion_blockers({**evidence, 'samples': 9}) == ['insufficient_samples']


def test_halogen_is_not_registered_until_it_has_a_canonical_profile():
    assert set(MODEL_PROFILES) == {'red-paro-int5', 'corsair-ninfer'}


@pytest.mark.parametrize('outside_scope', [False, True])
async def test_workspace_enforces_model_contract_before_running_checks(tmp_path, outside_scope):
    import sys
    from aria.loop.git import git
    from aria.steward.weekly.execution import Workspace
    from aria.steward.weekly.policy import Target
    from .test_loop import TrustedProcessRuntime
    repo = tmp_path/'repo'
    (repo/'ninfer3090').mkdir(parents=True)
    source = repo/'ninfer3090/run-serve.sh'
    source.write_text(NINFER)
    await git('-C',repo,'init','-b','main')
    await git('-C',repo,'add','.')
    await git('-C',repo,'-c','user.name=Test','-c','user.email=test@local','commit','-m','before')
    checks = tmp_path/'checks'
    checks.mkdir()
    target = Target(repository=str(repo),branch='main',assets=str(checks),
                    allowed_paths=['ninfer3090/run-serve.sh'],model_profile='corsair-ninfer',
                    image='sha256:'+'a'*64,
                    checks={'queue':{'version':'1','argv':[sys.executable,'-c',
                        "from pathlib import Path; assert '--max-pending-requests 1' in Path('/workspace/ninfer3090/run-serve.sh').read_text()"]}},
                    acceptance_check_ids=['queue'])
    workspace = Workspace(tmp_path/'work','models',target,'run','attempt',runtime=TrustedProcessRuntime())
    await workspace.prepare()
    candidate = NINFER.replace('requests 2','requests 1')
    if outside_scope:
        candidate = candidate.replace('96256','131072')
    (workspace.path/'ninfer3090/run-serve.sh').write_text(candidate)
    if outside_scope:
        with pytest.raises(ValueError,match='Unapproved'):
            await workspace.verify('queue')
    else:
        result = await workspace.verify('queue')
        assert result['before']['queue']['exit_code'] != 0
        assert result['after']['queue']['exit_code'] == 0
