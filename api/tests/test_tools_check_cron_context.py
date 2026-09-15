"""Prevent a passing tool canary when the cron agent will reject its model."""
from pathlib import Path
import runpy
import pytest

check = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/aria-tools-check'))['cron_model_context']


def config(context):
    return {'cron': {'model': 'cron-model', 'model_provider': 'aria'},
            'providers': {'aria': {'models': {'cron-model': {'context_length': context}}}}}


@pytest.mark.parametrize('context', [32768, 63999, None, True, '96256'])
def test_rejects_below_floor_or_unverified_cron_context(context):
    with pytest.raises(ValueError, match='at least 64000'):
        check(config(context), 64000)


@pytest.mark.parametrize('context', [64000, 65536, 96256, 131072])
def test_accepts_verified_context_that_meets_installed_hermes_floor(context):
    assert check(config(context), 64000) == context
