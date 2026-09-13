"""Isolated PARO int5 option for authorized qualification on Red.

Hardware artifacts: CorsairModelHost/red-r9700/paro-int5-isolated.
ARIA task: 6aa71005e93eb203d99ca56c. This file is activated only by its
dedicated qualification gateway, without changing production routes.
"""
SLUG = 'Red-Qwen3.8-27B-PARO-INT5-Isolated'


def make_spec(spec_type):
    ssh = ('ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
           '-o', 'UserKnownHostsFile=/Users/ben/.ssh/known_hosts_red_linux',
           '-o', 'HostKeyAlias=red-linux', 'ben@red-linux.tailb286a5.ts.net')
    return spec_type(
        slug=SLUG,
        description='Isolated Launch80 Qwen3.8 27B PARO int5 candidate, '
                    'per-group int8 activations, zero-point epilogue, TP2 and DFlash2 FP8. '
                    'Qualification in progress; existing Red options are preserved.',
        runtime_repo='https://codeberg.org/ggz14/radiance-vllm-mxfp4',
        runtime_ref='9b8db2d497092c5020f9eff751578359df876e0d',
        runtime_family='vllm',
        backend_device='2 x gfx1201 (Radeon AI PRO R9700, 32 GiB each)',
        devices=('Red R9700 0000:03:00.0', 'Red R9700 0000:06:00.0'),
        host_machine='machine:red', deployment='red-r9700/paro-int5-isolated',
        container_name='red-paro-int5-isolated', port=18079,
        onbox=False, startable=False, allow_force_start=False, auto_route=False,
        memory_pool='remote',
        not_startable_reason='Isolated qualification; use the documented Red systemd unit.',
        exclusive_with=('Red-Qwen3.8-27B-MXFP4', 'Red-Qwen3.8-Flash-Next-MXFP4',
                        'Red-Qwen3.8-27B-PARO-MXFP4', 'Red-Qwen3.8-Flash-Next-GPTQ'),
        remote_start_command=ssh+('systemctl --user start red-paro-int5-isolated',),
        remote_stop_command=ssh+('systemctl --user stop red-paro-int5-isolated',),
        remote_health_url='http://127.0.0.1:18079/health',
        remote_model_id='red-qwen3.8-27b-paro-int5-isolated',
        remote_ready_deadline=1800.0,
        endpoint_override='http://127.0.0.1:18079/v1',
        consumers_note='Explicit qualification only; no automatic routing or client defaults. '
                       'Pinned int5 checkpoint 668cb0d71371dbebb10785f166af302e7e718b95. '
                       '262144 context, eight slots, DFlash depth seven; independent mutable paths.',
    )
