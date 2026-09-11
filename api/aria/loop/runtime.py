"""Process containment for coding and verification. No unsandboxed fallback."""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import signal
import tarfile
from pathlib import Path

from aria.core.logging import scrub_secrets


class InfrastructureError(RuntimeError):
    def __init__(self, message, output=b""):
        super().__init__(message)
        self.output = output


class ProcessTimeout(TimeoutError):
    def __init__(self, output):
        super().__init__("Process wall time exceeded")
        self.output = output


async def process(argv: list[str], *, timeout: float = 30, env=None, stdin: bytes | None = None,
                  max_output: int = 1024 * 1024) -> tuple[int, bytes]:
    """Bound memory and wall time; reap the local client and its process group."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=env, start_new_session=True,
    )
    output = bytearray()

    async def collect():
        async def feed():
            if stdin is not None:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
                proc.stdin.close()
        writer = asyncio.create_task(feed())
        try:
            while chunk := await proc.stdout.read(65536):
                if len(output) + len(chunk) > max_output:
                    raise InfrastructureError("Process output limit exceeded", bytes(output))
                output.extend(chunk)
            await writer
            await proc.wait()
        finally:
            if not writer.done():
                writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    completed = False
    try:
        await asyncio.wait_for(collect(), timeout)
        completed = True
        return proc.returncode, bytes(output)
    except TimeoutError as exc:
        raise ProcessTimeout(bytes(output)) from exc
    finally:
        # collect() has already reaped a successfully completed command. Its
        # numeric process-group ID is no longer ours to signal. In particular,
        # Darwin can return EPERM here after a short-lived Git command exits.
        # Interruptions still kill the group, including when a dead leader's
        # descendants hold the output pipe open. Candidate daemons are separately
        # terminated by ContainerRuntime.stop(), including detached descendants.
        if not completed:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.wait()


class ContainerRuntime:
    """One container per worker attempt/check; Docker's PID namespace owns descendants.

    The container expires even after controller loss. Recovery removes its durable
    name before inspecting any candidate. API/LLM credentials never enter it.
    """

    def __init__(self, binary: str = "docker", state_root: Path | None = None):
        self.binary = binary
        self.state_root = state_root

    async def _docker(self, *args, timeout=30):
        code, output = await process([self.binary, *map(str, args)], timeout=timeout)
        if code:
            raise InfrastructureError(scrub_secrets(output.decode(errors="replace")[-4000:]))
        return output.decode().strip()

    async def preflight(self, image: str):
        await self._docker("info", "--format", "{{.ServerVersion}}")
        # Never pull a mutable image or contact a registry during a run.
        await self._docker("image", "inspect", image, "--format", "{{.Id}}")

    async def start(self, name: str, workspace: Path, policy, *, readonly=False,
                    assets: Path | None = None, seconds=900):
        # Copy through the daemon API into private volumes. This works with
        # Aria's Mac-local Lima engine without exposing the Mac home directory
        # to the VM or binding any controller path into candidate execution.
        seed = name + "-seed"
        volumes = [name + "-work"] + ([name + "-checks"] if assets else [])
        for volume in volumes:
            await self._docker("volume", "create", "--label", "aria.loop=true", volume)
        seed_args = ["create", "--pull=never", "--name", seed, "--network=none",
                     "--user", "0:0", "--read-only", "--cap-drop=ALL", "--cap-add=CHOWN",
                     "--security-opt=no-new-privileges", "--pids-limit=16",
                     "--label", "aria.loop=true", "--entrypoint", "/bin/chown",
                     "--mount", f"type=volume,source={name}-work,target=/workspace"]
        if assets:
            seed_args += ["--mount", f"type=volume,source={name}-checks,target=/checks"]
        await self._docker(*seed_args, policy.image, "-R", f"{os.getuid()}:{os.getgid()}",
                           "/workspace", *(["/checks"] if assets else []))
        await self._docker("cp", "--archive", str(workspace) + "/.", seed + ":/workspace", timeout=60)
        if assets:
            await self._docker("cp", "--archive", str(assets) + "/.", seed + ":/checks", timeout=60)
        await self._docker("start", "--attach", seed)
        await self._docker("rm", seed)
        args = [
            "run", "--detach", "--pull=never", "--name", name,
            "--label", "aria.loop=true", "--network=none", "--read-only", "--init",
            "--label", f"aria.loop.workspace={workspace}",
            "--label", f"aria.loop.readonly={str(readonly).lower()}",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
            f"--memory={policy.memory_mb}m", f"--cpus={policy.cpus}",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp",
            "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_CONFIG_GLOBAL=/dev/null",
            "--workdir", "/workspace", "--mount",
            f"type=volume,source={name}-work,target=/workspace,volume-nocopy" + (",readonly" if readonly else ""),
        ]
        if assets:
            args += ["--mount", f"type=volume,source={name}-checks,target=/checks,readonly,volume-nocopy"]
        args += ["--entrypoint", "/bin/sh", policy.image, "-c", f"sleep {int(seconds) + 30}"]
        await self._docker(*args)

    async def execute(self, name: str, argv: list[str], timeout: float):
        code, output = await process(
            [self.binary, "exec", name, *argv], timeout=timeout,
        )
        if code and (output.lower().startswith(b"error response from daemon:") or b"OCI runtime exec failed" in output):
            raise InfrastructureError("Container command could not be launched", output)
        return {"exit_code": code, "output": scrub_secrets(output.decode(errors="replace"))}

    async def stop(self, name: str):
        # rm -f kills the container init, and thus EVERY descendant in its PID
        # namespace, including setsid/double-fork descendants. Confirm removal;
        # daemon failure is an infrastructure stop, never permission to verify.
        code, output = await process([self.binary, "inspect", name])
        if code and b"no such" not in output.lower():
            raise InfrastructureError("Could not inspect container for termination")
        if code == 0:
            doc = json.loads(output)[0]
            if doc["Config"]["Labels"].get("aria.loop") != "true":
                raise InfrastructureError("Refusing to terminate an unmanaged container")
            await self._docker("stop", "--time", "0", name)
            stopped = json.loads(await self._docker("inspect", name))[0]
            if stopped["State"]["Running"]:
                raise InfrastructureError("Container descendants have not terminated")
            labels = doc["Config"]["Labels"]
            if labels.get("aria.loop.readonly") == "false":
                destination = Path(labels["aria.loop.workspace"]).resolve()
                if self.state_root is None or not destination.is_relative_to(self.state_root.resolve()):
                    raise InfrastructureError("Retained workspace is outside controller state root")
                code, archive = await process([self.binary, "cp", name + ":/workspace/.", "-"],
                                              timeout=60, max_output=160 * 1024 * 1024)
                if code:
                    raise InfrastructureError("Could not retain stopped worker files")
                # Keep the archive even if a worker wrote an invalid special
                # file. Never let a tar path or symlink write into controller state.
                archive_path = destination.parent / (name + ".tar")
                archive_path.write_bytes(archive)
                stage = destination.parent / (name + "-retained")
                if stage.exists():
                    shutil.rmtree(stage)
                stage.mkdir()
                invalid = False
                with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                    for item in tar.getmembers():
                        target = stage / item.name
                        if (not target.resolve().is_relative_to(stage) or not (item.isfile() or item.isdir())):
                            invalid = True
                            continue
                        if item.isdir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(tar.extractfile(item).read())
                            target.chmod(0o755 if item.mode & 0o111 else 0o644)
                previous = destination.parent / (name + "-previous")
                if destination.exists() and previous.exists():
                    # The two renames completed on a previous recovery before
                    # container removal. Reuse that retained result idempotently.
                    shutil.rmtree(stage)
                elif destination.exists():
                    destination.rename(previous)
                    stage.rename(destination)
                else:
                    stage.rename(destination)
                # The marker forces controller scope validation to reject a
                # candidate whose full tree could not safely be materialized.
                if invalid:
                    (destination / ".loop-invalid-archive").write_text("Unsupported worker archive entries; inspect retained tar")
        for container in (name, name + "-seed"):
            code, output = await process([self.binary, "rm", "--force", container])
            if code and b"no such container" not in output.lower():
                raise InfrastructureError("Could not confirm container termination")
        for volume in (name + "-work", name + "-checks"):
            code, output = await process([self.binary, "volume", "rm", volume])
            if code and b"no such volume" not in output.lower():
                raise InfrastructureError("Could not clean up private execution volume")
