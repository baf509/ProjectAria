# HANDOFF — Qwen3.8-Flash-Next on Corsair → Mac control plane (2026-08-26)

Written from Corsair (`ben@corsair-ai`). The Corsair side is done and the registry entry is
committed in this repo. Three steps need the `devboxsvc` service account on this Mac, which
Corsair cannot reach (no read access to `/Users/devboxsvc`, no passwordless sudo).

## What is already done

- Corsair: `infrastructure/qwen3.8-flash-next/` (serve.sh, README, SHA256SUMS), vendored unit
  `systemd/qwen3.8-flash-next.service` installed as a user unit, `endpoints.env`
  `QWEN38_FLASH_URL=http://127.0.0.1:8120/v1`, `QWEN38_FLASH_MODEL=qwen3.8-flash-next`.
  Model is running on `:8120` (Halo, 256K ctx, 1 slot). Radiance stays on `:8080`.
- Registry: `ModelServerSpec(slug="Qwen3.8-Flash-Next-IQ4_XS-Halo", port=8120,
  systemd_unit="qwen3.8-flash-next.service", memory_pool=POOL_HALO, ...)` added to
  `api/aria/infrastructure/model_servers.py` and to `_HALO_BIG` (so it is mutually exclusive
  with DS4 DwarfStar and the other Halo residents). Also installed into Corsair's actuator
  runtime copy (`~/.local/share/aria-model-actuator/api/.../model_servers.py`), so
  `aria-model-actuator {status,start,stop} Qwen3.8-Flash-Next-IQ4_XS-Halo` works now.
- ⚠️ Registry tests could NOT be run from `ben`'s environment on this Mac (no python with
  pytest + the aria deps). The module parses and imports; `test_registry_has_no_duplicate_slugs`
  and `test_exclusivity_is_symmetric` were checked by hand via the actuator venv on Corsair.
  Run `api/tests/test_model_servers.py` from the devboxsvc environment before deploying.

## To do as devboxsvc

1. **Deploy the registry change to the running ARIA** — `/Users/devboxsvc/Services/apps/bin/run-aria-api`
   runs its own copy of ProjectAria; sync this commit into it and restart
   `com.ben.devbox.aria-api` (launchd). Verify:
   `GET /api/v1/infrastructure/model-servers` lists `Qwen3.8-Flash-Next-IQ4_XS-Halo` as running.
2. **Add `:8120` to the Corsair model forwards** (`run-corsair-model-forwards`,
   `com.ben.devbox.corsair-forwards`) alongside 8080/8112, and restart it. Until then nothing on
   the Mac can reach the model.
3. **Hermes default model → Flash-Next.** In devboxsvc's `~/.hermes/config.yaml`
   (`com.ben.devbox.hermes-gateway`): add a provider, e.g.

   ```yaml
   qwen38-flash:
     base_url: http://127.0.0.1:8120/v1     # via the forward from step 2
     api_key: EMPTY
     models:
       - name: qwen3.8-flash-next
         context_window: 262144
   ```

   and set the default model to `custom:qwen38-flash` / `qwen3.8-flash-next`. Keep the existing
   `qwen38-r9700` provider (Radiance) as the vision-capable fallback. Restart the gateway.
   Notes for the provider: thinking is on by default; pass
   `chat_template_kwargs: {enable_thinking: false}` for non-thinking calls; recommended
   non-thinking sampling `temperature 0.7, top_p 0.8, top_k 20, presence_penalty 1.5`;
   thinking `temperature 1.0, top_p 0.95, top_k 20`. No vision (no mmproj published yet).
4. ARIA's `steward_model` / `LLAMACPP_URL` target was deliberately left on Radiance.

## State to reconcile

- DS4 `DS4-0731-Q8Protected-Halo-DwarfStar` was stopped on Corsair at 09:44 with
  `systemctl --user stop` (not via ARIA) and is still stopped — it cannot coexist with
  Flash-Next on the Halo. Radiance was also stopped/restarted locally for the dual-GPU test and
  is back up. If ARIA's view of either is stale, a `status` through the actuator refreshes it.
