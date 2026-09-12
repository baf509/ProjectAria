"""Authentication for the registered loopback CUDA/Halo model transport.

The model-only key is distinct from ARIA's admin and inference credentials.
Never attach it to an arbitrary URL, redirect, log, registry response or client.
"""
import os
from pathlib import Path
import platform
import re
import stat
from urllib.parse import urlsplit


class BackendAuthError(OSError):
    pass


def credential_path() -> Path:
    if platform.system() == "Darwin":
        return Path("/Users/ben/Services/secrets/corsair-cuda-halo-backend.key")
    return Path("/home/ben/.config/aria-model-secrets/flashnext-cuda-halo.key")


def read_private_key(path: Path) -> str:
    try:
        # O_NOFOLLOW plus fstat avoids a symlink/check-open race. No secret is
        # ever included in an exception, even when the file is malformed.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or not 32 <= info.st_size <= 129):
                raise ValueError("invalid credential file")
            key = stream.read(130).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
            raise ValueError("invalid credential format")
        return key
    except (OSError, ValueError, UnicodeError):
        raise BackendAuthError("CUDA/Halo backend credential unavailable or unsafe") from None


def backend_headers(base: str) -> dict[str, str]:
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError:
        return {}
    # Literal loopback endpoints only, on the sole registered candidate port.
    # No DNS aliases, URL credentials, query/fragment or arbitrary path scope.
    if (parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1")
            or port != 8131 or parsed.username is not None or parsed.password is not None
            or parsed.path.rstrip("/") not in ("", "/v1") or parsed.query or parsed.fragment):
        return {}
    return {"Authorization": "Bearer " + read_private_key(credential_path())}
