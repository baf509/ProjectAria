"""Operator-selected Red PARO int5 deployment; independent model/runtime paths retained."""
SLUG = 'Red-Qwen3.8-27B-PARO-INT5'


def make_spec(spec_type):
    ssh = ('ssh', '-F', '/Users/ben/Services/config/red-model-ssh.conf', 'red-linux-model')
    return spec_type(
        slug=SLUG,
        description='Launch80 Qwen3.8 27B PARO int5 in its isolated runtime, '
                    'per-group int8 activations, zero-point epilogue, TP2 and DFlash2 FP8. '
                    'Qualified 2026-09-13: five functional checks and full throughput suite passed; '
                    'HumanEval+ 148/164 versus existing 27B 150/164 (inconclusive accuracy gap), lower throughput. '
                    'Selected by Ben as the Red and Hermes default on 2026-09-14.',
        runtime_repo='https://codeberg.org/ggz14/radiance-vllm-mxfp4',
        runtime_ref='9b8db2d497092c5020f9eff751578359df876e0d',
        runtime_family='vllm',
        backend_device='2 x gfx1201 (Radeon AI PRO R9700, 32 GiB each)',
        devices=('Red R9700 0000:03:00.0', 'Red R9700 0000:06:00.0'),
        host_machine='machine:red', deployment='red-r9700/paro-int5-isolated',
        container_name='red-paro-int5-isolated', port=8094,
        onbox=False, startable=True, allow_force_start=False, auto_route=True,
        memory_pool='remote',
        wake_command=('/Users/ben/Services/apps/bin/wake-red-model',),
        exclusive_with=('Red-Qwen3.8-27B-MXFP4', 'Red-Qwen3.8-Flash-Next-MXFP4',
                        'Red-Qwen3.8-27B-PARO-MXFP4', 'Red-Qwen3.8-Flash-Next-GPTQ'),
        remote_start_command=ssh+('start-paro-int5',),
        remote_stop_command=ssh+('stop-paro-int5',),
        remote_health_url='http://127.0.0.1:8094/health',
        remote_model_id='red-qwen3.8-27b-paro-int5-isolated',
        remote_ready_deadline=1800.0,
        endpoint_override='http://127.0.0.1:8094/v1',
        consumers_note='Red default, Hermes default and available in all managed Pi installations. '
                       'Pinned int5 checkpoint 668cb0d71371dbebb10785f166af302e7e718b95. '
                       '262144 context, eight slots, DFlash depth seven; independent mutable paths.',
    )
