# Historical ARIA systemd units

These are archived copies from the pre-2026-08-23 Corsair control plane. They
are retained for forensic and recovery reference only.

Do **not** install, enable, or repair `aria-api`, `aria-tmux`, MongoDB, Hermes,
Signal, or UI services from this directory on Corsair. The live control plane is
the Mac deployment under `/Users/ben/Services`, managed by launchd. Corsair runs
only its thin `aria-node` compatibility service and model/runtime units owned by
the private infrastructure repository.

Some files preserve old paths, accounts, ports, and DeepSeek deployments because
they are historical evidence. Their presence does not make those values current.
For current operations use the root README, `docs/ops/*`, the vault Architecture
Charter, and the live service managers.

Never copy secrets into vendored unit files. ARIA-generated model units are not
authoritative copies; model lifecycle normally goes through the restricted ARIA
actuator, with direct Corsair work limited to authorized model repair/testing.
