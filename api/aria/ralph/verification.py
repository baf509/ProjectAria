"""Replaceable trusted check executor; scheduling and acceptance stay in the controller."""


class ProcessVerifier:
    async def run(self, runtime, container_name, check):
        """Execute an operator-approved definition in the prepared read-only sandbox."""
        return await runtime.execute(container_name, check.argv, check.timeout_seconds)
