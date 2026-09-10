# Mongo database VM

The Mac's **Mongo database VM** hosts two distinct containers. Its historical
Lima identifier is `mongot`; that identifier does **not** mean search is running.

| Layer | Identifier | Role and intended state |
| --- | --- | --- |
| VM | Lima `mongot` | Database host; keep running |
| VM supervisor | `com.ben.devbox.lima-mongot` | Keeps the shared VM running |
| Database | `devbox-mongod` / Aria `shared-mongod` | MongoDB, required and running |
| Search | `devbox-mongot` / Aria `shared-mongot` | Optional vector/BM25 search; disabled and stopped |
| Connection | Mac `127.0.0.1:27018` → guest `27017` | Database tunnel |

Stopping the VM stops MongoDB and interrupts Aria and other database clients.
Disabling search only stops the search container; it does not free the VM's RAM.
Inspect the containers separately:

```sh
limactl shell mongot docker ps -a --format '{{.Names}} {{.Status}}'
```

Use **Mongo database VM** in operator descriptions. Retain the historical Lima
identifier in executable commands and launchd configuration until a planned
migration can update the VM directory, disk references, tunnel, supervisor,
registry, backups and recovery scripts together. Renaming only a folder or
service label would leave broken references. A second VM is unnecessary for
clarity and would add resource overhead.

Verified 2026-09-09 UTC: `devbox-mongod` healthy; `devbox-mongot` exited.
