"""Fixed noninteractive privilege contract. No cached sudo ticket is required."""
from pathlib import Path
import os
import stat
import subprocess

HELPER = Path('/Library/PrivilegedHelperTools/com.aria.service-restart')
LABELS = {
    'com.ben.devbox.aria-api': 'aria-api',
    'com.ben.devbox.aria-ui': 'aria-ui',
    'com.ben.devbox.aria-agent-node': 'aria-node',
    'com.ben.devbox.hermes-gateway': 'hermes',
}


def verify_installation():
    for path in [HELPER, *HELPER.parents]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Restart helper and its parents must be root-owned and not writable by other users')
    if not os.access(HELPER, os.X_OK):
        raise RuntimeError('Restart helper is not executable')


def invoke(operation, label):
    if operation not in {'check', 'restart'} or label not in LABELS:
        raise ValueError('Unregistered restart operation or service')
    try:
        verify_installation()
        subprocess.run(['/usr/bin/sudo', '-n', str(HELPER), operation, LABELS[label]],
                       check=True, capture_output=True, timeout=30,
                       env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Unattended restart unavailable. Run the one-time scripts/install-aria-restart-helper installer in an administrator terminal.') from exc


def preflight(labels):
    for label in labels:
        invoke('check', label)
