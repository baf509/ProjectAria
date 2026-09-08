# Shared ARIA/Hermes rollout coordination — 2026-09-08

Current September8 handoff: [deployment/client state](CURRENT_DEPLOYMENT_20260908.md)
supersedes older model-status updates below. Hermes has gracefully reloaded and
reconnected to118ARIA tools; installed Pi/Hermes executor and compaction checks
pass. MTP stays ON at Ben's accepted risk; no more crash investigations or soaks.
Routine-start activation and one lifecycle check remain pending. Historical
failures and stopped experiments below are preserved, not current instructions.

## MTP ON verified; short speed check complete — September 8 19:41 UTC

User-requested profileac0c9deb8bdd is ready through authenticated ARIA. Model
PID1315892/start2504089, unitMainPID1307332, container
`e622308e392e44555dda52ffcb0eb06c715a9a52390f95667fa5685a80a764b7`.
Actual launch: authorQ4_K_M draft head onCUDA0, draft depth3/p_min0.7,
MTP1, sameTopKfallback/CUDAcaptureOFF/one256K slot/q8KV/cache settings.
All three short fixed512-output thinking-off code cells passed:
4Kcold443.08PP/56.93TG (333/372 draft tokens accepted,89.52%);
8Kcold462.42PP/56.44TG;8Kwarm56.87TG,8352cached tokens.
Warm request prefill timing is NOT a full-prefill throughput measurement.
These are one sample per cell, not repeated medians or long-context qualification.

Evidence `results/user-mtpon-speed-check-20260908` SHA-verified both hosts;
raw55733c20086e2a5d87f65b6abfe1adc9b062af49129a05cdd55d953c4a2bfe88.
Benchmark finished; no observer/test remains active. Same model is running and
ARIAadmission idle. No new soak, defaults/client/release/controller/driver changes.
Author/head/lineage are unchanged; re-enablingMTP is NOT a fix for the previously
observed MTP-on crash. Overall stable deployment remains unqualified. Continue
remaining quality/context/client checks without treating restored speed as
acceptance or silently scheduling another soak Ben already ended.



## User-requested MTP restoration loading — September 8 19:34 UTC

Ben explicitly requested the author's MTP head (or a better newer one) and
MTP ON. Author HEAD remains e8bfdb53d32f; upstream qwen4exp/mtp-fix remains
337c8bb58b29; dzannotti head repository remains0b2551d19154. We already have
the recommended Q4_K_M head; no new files or engine changes were downloaded.
The same author depth3/p_min0.7/Q4 head is restored. This is NOT a newly found
fix for the earlier MTP-on crash, and that failed evidence remains relevant.

Only the owned performance client9259 and observer1283355 were interrupted.
All six cold benchmark cells completed: three repeats each at4K/32K, medians
481.10/33.58 and428.16/27.52 prefill/decode tok/s. The optional8K cache cells
were interrupted on Ben's new request; the whole benchmark is not marked passed.
ARIA then stopped model1135303 normally; unitinactive/GPU1MiB verified.

Profileac0c9deb8bdda4fdb197f6de0c3c42ad43deed63efb07a082c31a0144981854e;
binding0ce202dd81adccf21d338fc82075cdf4a8a652efbeb326a46ab7f209979e91e8;
adaptera720ba00e662 unchanged. Only MTP and explanatory metadata changed;
same Top-K fallback/CUDAcaptureOFF/256K/one slot/q8KV/32checkpoints/8GiBcache.
Backup both hosts `.work/before-user-mtpon-jl6cab24`; guarded installer receipt
`results/user-mtpon-installed-20260908.json`.149local tests pass.
Registered unitMainPID1307332 started19:34:22UTC; loading, not yet inference
verified. Next identify readiness, confirm actual MTP draft activity, measure
current speed. No new soak, client/default/release/controller/driver changes.



## User-ended soak; current speed measurements — 19:24 UTC

Ben ended the soak and requested current prefill/decode numbers. Only test
client1543 and observer1170591 stopped; model1135303 remains running, unchanged
profile9e7a5003ccdb. 542 completed response records revalidated, zero recorded
failures, approximately24minutes. NOT a full7200s pass. The old20:55 soak
deadline and19:52 observer renewal are cancelled; no replacement soak scheduled.
Explicit operator-stop evidence is preserved in the original results directory.

Fresh512-output code benchmarks are now running through the same watched Mac
shell at4K/32K with three repeats and separate8K cache pairs. Output
`flashnext-author-reproduction/results/topk-mtpoff-short-ladder-20260908`.
Keep competing GPU work/API restarts staged during these actual measurements.
Hermes configured stdio MCP auth/discovery/candidate inventory and all10 raw
backend auth/CORS checks passed separately without model generation. Actual
client turns and release/cutover remain pending. No defaults, controller policy,
driver or client settings changed; no further soak is implied by old notes below.

## Native MTP-off full soak started — September 8 18:55 UTC

The unchanged 12-check tool/cache diagnostic passed on profile `9e7a5003ccdb`.
At 32K, cold/branch requests took 79.63/79.57 seconds; identical warm/return
requests took 2.65/3.21 seconds. These are functional retrieval latencies, not
a fixed-output throughput benchmark. Results are SHA-verified on both hosts:
`results/topk-mtpoff-reliability-20260908`, raw records
`c4658b48f81d161273795057621c814a253953612764029e88c63fb3cada2825`.

Full unchanged synthetic workload started **18:55:51 UTC**, targeting 7200
seconds through **20:55:51 UTC**, plus bounded in-flight request completion.
It uses the frozen original 32K Pi / 8K background fixtures, synthetic Hermes
traffic, one-slot admission and 900-second absolute request deadlines. This
is not actual client-executor acceptance. Output:
`results/topk-mtpoff-soak-2h-20260908`. No result is accepted yet.

Exact native model PID1135303 / start ticks2172699, service MainPID1125434,
container `60acdd57d3536f80dccd59210ab228c484a2ba838c884c55ba188b7df31cbd7b`.
Image, driver610, Top-K variant b1b3e67c167d and profile remain unchanged.
MTP OFF, CUDA capture OFF, native async, no sanitizer wrapper. Mac watched
soak launcher PID1542; remote read-only observer PID1170591 follows this model
until approximately19:51:59 UTC (3600-second bound). Continue observation with
a new bounded part only when that observer actually terminates. Keep API/model
restarts and competing GPU work staged during the soak. If it passes, fresh
performance, deep-context, quality, installed-client and release gates remain.
No defaults, client configuration, controller policy or commits changed.



## Native MTP-off control loading — 18:39 UTC

Reduced memcheck traffic completed11successful responses, but no final
sanitizer summary was verified after ARIA's clean18:33:02stop; no clean-memory
or model-qualification claim. All previous diagnostic processes are gone.
The same isolated Top-K fallback now loads natively with MTP OFF, preserving
256K/context/cache/placement and CUDAcaptureOFF. Profile9e7a5003ccdb,
adaptera720ba00e662, serviceMainPID1125434 started18:39:08UTC. This tests
speculation dependency/potential fallback, not a claimed author-MTP reproduction.
Next12tool/cache checks then unchanged full7200s gate if passing. Keep competing
GPU work and API restarts staged during the actual tests. No client/default,
release, driver or controller-policy changes.

## Reduced memory diagnostic active — 18:19 UTC

Corrected diagnostic launcher is running model1046812, service1043597,
profile6d6f2df654be/adaptera720ba00e662. CPU-only PID1 timeout issue fixed via
Docker`--init`, no privilege change. Full32K prefill proved too slow under
instrumentation (~18tok/s), so only that test client was deliberately cancelled;
server returned idle18:17:47 without restart. This was not a model fault/pass.
Now watched MacPID91652 runs600s of explicitly reduced1024/512token synthetic
sessions,300s absolute request deadlines, same256K model/cache settings.
Observer1053766 follows exact model; wrapper bounds model life throughabout
18:41:18UTC (+20s grace). Keep API/model changes and competing GPUwork staged
until this actual diagnostic is terminal. No release/client cutover is approved.

## Bounded model memory diagnostic loading — 18:04 UTC

Registered unitMainPID1031146 started18:04:18UTC with a diagnostic-only memory
checker, preserving failed fallback source/build/model/placement/cache geometry.
Profile6d6f2df654be/adapter3164e46e1d2e. The checker worked without extra
privileges on452isolated operator tests; model-level reproduction remains.
Wrapper bounds its model lifetime to1800seconds including loading. After
identified readiness, plan one600s synthetic mixed-client replay with unchanged
900s request deadlines. Please keep API/model restarts and competing candidate
GPUwork staged during the actual diagnostic. No release/client cutover or
controller change. Earlier555-response crash remains failed evidence.

## Fallback soak FAILED; model off — 17:46 UTC

The isolated fallback exited139 at17:45:59UTC after a CUDA illegal-memory
access and Xid31. Soak is terminal:555 responses,3 failed SSE events,
1179.577seconds, full_duration=false/all_passed=false. Model921006, client77711
and observers926445/964505 are gone; unitfailed/MainPID0/GPUmemory1MiB.
The active-test API maintenance hold and19:26 finish estimate below are
superseded. Preserve exact failed artifacts/evidence; no unchanged candidate
restart or release/client cutover. Next model work is targeted memory-fault
diagnosis, not another blind soak. No Ralph/controller policy changes.

## Fallback full soak running — 17:31 UTC

The isolated fallback passed12/12 model-level tool/cache/retrieval checks.
Its full7200-second frozen synthetic soak started17:26:18UTC, MacPID77711
(uv77710), modelPID921006, profilef110896e466a. The expected window ends
19:26:18UTC; in-flight requests retain their900-second deadlines. Please keep
API/model restarts and competing candidate GPU jobs staged until its actual
terminal result. No separate20-minute pre-soak was run. The earlier failed
diagnostics remain intact and are not resumed. Hardware observation has a
bounded read-only continuation through19:45UTC, without duplicate polling.
No release, client cutover, controller-policy change or commit is implied.

## Top-K fallback candidate loading — 17:16 UTC

Ben's synchronous snapshot is verified and localizes the wait to CUDA CUB
DeviceTopK initialization. The failed replay is terminal, archived; ARIA
stopped model808624. Both earlier model-preservation holds are superseded.
The model delivery session has installed an isolated one-file sort-fallback
candidate (profilef110896e466a), with452/452 operator tests passing. Service
MainPID912793 is loading for fresh functional/cache and bounded mixed-session
checks. Please keep competing candidate GPU work and control-plane restarts
staged while those checks run. No release/default/client cutover is authorized
by the narrow operator result; controller policy and unrelated work are intact.

## Diagnostic stalled; preserve new process — 16:56 UTC

The synchronous diagnostic stopped advancing16:53:05UTC after70responses.
ModelPID808624 remainsRsl with GPUutilization0%; client67956 is still awaiting
unchanged900-second deadlines. Preserve model808624 and exact artifacts for
Ben's new snapshot command: `bash /tmp/flashnext-capture-sync-stall-20260908.sh`.
No replacement GPU run, model restart or release/client cutover. Observe the
existing client to its terminal result; do not retry the failing configuration.
The earlierPID59775 capture completed and remains archived on both machines.

## Bounded diagnostic is running — 16:50 UTC

The diagnostic replay actually started16:48:25UTC for1200seconds in watched
Mac shell `claude-flashnext-3090-hardware-check`, process67956 (uv67947).
ModelPID808624, profile02eabbce9290. Expected launch window ends17:08:25UTC;
in-flight requests retain their original900-second absolute deadlines. Keep
API/model restarts and competing candidate GPU jobs staged until its observed
terminal result. No release acceptance follows from synchronous-debug timing.

## Capture complete; bounded diagnostic loading — 16:42 UTC

Ben completed the identity-bound driver610 stack capture. Evidence is archived
on both machines; ARIA then stopped PID59775 and released its GPU memory.
The preservation hold below is superseded. The model delivery session is now
loading a diagnostic-only synchronous-CUDA profile (02eabbce9290), service
MainPID798762, for a1200-second frozen synthetic Hermes/Pi replay after readiness.
No release or client cutover is permitted from this timing-altered diagnostic.

Please keep API/model changes and competing Flash Next GPU work staged during
this bounded test; see the model project's STATUS_20260907.md for its actual
start/terminal state. No controller-policy changes or commits are in scope.
The old PID59775 capture script must not be reused for a different process.

## Failed soak terminal; preserve model for snapshot — 16:21UTC

All three clients timed out naturally; summaryall_passedfalse, elapsed1514.730s,
full_duration_reachedfalse. BenchmarkPID53151 is gone; admission is idle with
zeroqueued. Completed evidence is retained on bothhosts. The17:52 schedule
and associated active-benchmark APImaintenance hold are superseded. There is
no active GPU benchmark to resume or retry.

**Preserve modelPID59775 and its exact artifacts for Ben's requested stack
capture; do not stop/restart/replace it or promote the candidate.** Only the
read-only hardwareobserverPID700323 remains active, boundeduntil~16:29UTC.
The capture requires Ben's sudo authentication; no privilegedsnapshot yet.
No controller changes, client/default cutover, commits or publication.

## Model stalled; diagnostic preservation boundary — 16:14UTC

The fresh soak is no longer making progress. Its311response records stop at
16:02:35UTC; same modelPID59775, unchanged healthyARIAboot0f2ba6c103d840efbaa4d0888ffed0a6.
The client process53151 is still alive awaiting its unchanged request deadlines.
This is not a control-plane restart. Do not promote, restart, replace or send
new GPU work to the candidate. Preserve it for the identity-bound symbolized
stack snapshot requested from Ben. Command is
`bash /tmp/flashnext-capture-driver610-stall-20260908.sh` on the Mac.

The new snapshot binding passed CPU and remote no-attach symbol checks; no
privileged capture or root-cause claim yet. All client/default/release edits
stay staged. The expected17:52 soak-completion/cutover schedule below no longer
applies. Model delivery status/evidence remain in the author-reproduction root.

## Fresh full-duration soak started — 15:52UTC

**Please do not restart ARIA, Hermes or the Corsair model, or run competing
Flash Next GPU work, during15:52–17:52 UTC (11:52a.m.–1:52p.m.Eastern).**

The rollout's final completion report, terminal benchmark jobs and unchanged
API process for13minutes resolved the observed maintenance overlap. No separate
confirmation from Ben was received or is claimed. A fresh unchanged7200-second
test has now started in `claude-flashnext-3090-hardware-check`, under the model
project's `results/driver610-author256-soak-2h-quiet-20260908/`. The interrupted
run remains intact. Same PID59775, profileebaaa8da7a718, APIboot
0f2ba6c103d840efbaa4d0888ffed0a6; admission was idle before launch. No model
restart or configuration change. Passing this test remains unproven.

Further changes stay staged until the test completes. The model delivery
session will renew the read-only hardware observer after its current bounded
segment ends~16:06:40 UTC. Do not treat the earlier waiting note as current.

## Soak interrupted; quiet-window confirmation needed — 15:44UTC

The test announced below is no longer running. It stopped after271.543 seconds
with a Pi tool-call URLError while ARIA was shutting down. ARIA returned ready
at15:32:54 UTC and restarted again at15:37:46 UTC (currentboot
0f2ba6c103d840efbaa4d0888ffed0a6). The Corsair model remains PID59775; no
model-side stall is established. Preserve the incomplete test under the model
project's `results/driver610-author256-soak-2h-20260908/`.

The operations rollout now reports completion in `HERMES_OPERATIONS_20260908.md`.
The deployment session has requested confirmation of a restart-free window
before starting a fresh full-duration soak. No test is being silently retried.
Please keep further API/Hermes/model changes staged until rollout ownership and
the next two-hour window are coordinated. Read-only diagnostics and local tests
may continue. No release, client cutover, controller-policy change or commit.

## Current integration boundary — 15:27UTC

The concurrent rollout installed exactly the combined server/operations hashes
below and restarted Hermes (newPID44731) and ARIA (boot1d175245d96e4948a024d44ae002016d,
ready15:24:36UTC). ARIA's restart occurred after all164 generations completed;
the model PID59775 never changed. Deployed-path verification now PASS:
`/tmp/aria-hermes-exposure-20260908.4m70u1/deployed-registry.json`.
Actual gateway readiness reports118registered/selected, no missing requirements;
`model_visible` is stillnull, so no actual Signal model turn is claimed.

**DO NOT RESTART ARIA/HERMES/MODEL OR RUN COMPETING GPU BENCHMARKS DURING THE
NEXT TWO-HOUR SOAK (~15:28–17:28UTC).** Semantic evaluation finished155/164Plus,
matching the best matched prior. The model delivery session is starting the
final mixed-workload stability test in its existing hardware-check shell.
No further runtime/API cutover is needed for the seven read-only tools.
Their future edits and new operations should remain staged until the soak ends.

This session is delivering the Corsair RTX 3090 + Strix Halo qualification and
Ben's additional read-only diagnostic exposure request. A concurrent workstream
is changing `mcp/operations.py`, its `mcp/server.py` loader, benchmark service,
and `integrations/hermes/aria-readiness`. Those changes are preserved. This note
does not accept, deploy, approve or take ownership of their admin operations.

## Work owned by this session

- Seven read-only tools in `mcp/server.py`: operator_snapshot, inference_backend,
  inference_usage, inference_traces, benchmark_status, ralph_status, get_task.
- Correct stale Corsair GPU, auto/default routing, slot ownership and disabled
  retrieval descriptions. Keep legitimate R9700 hardware on other nodes intact.
- Redact upstream HTTP error bodies, return structured diagnostic envelopes,
  mark additions read-only, bound inputs/results, preserve unknown vs healthy.
- `tool_contract_status` now captures the server file hash at import instead of
  reporting a new on-disk file from an old running process. The concurrent
  workstream subsequently added a separate loaded operations hash in contract
  2026-09-08.2; the updated verification probe checks both identities.
- Tests: `api/tests/test_mcp_exposure.py` and minimal compatibility changes to
  `api/tests/test_mcp.py`; 62 scoped tests pass, most recently in 7.02 seconds.
- `integrations/hermes/verify-aria-exposure.py` tests actual installed Hermes
  registration/dispatch, configured credentials, and native tool search/describe.
- `environment-hint.md` retains API-auth guidance and adds diagnostic routing.
  Do not overwrite the live hint blindly; it already has extra auth guidance.

## Live test evidence and scope

`/tmp/aria-hermes-exposure-20260908.4m70u1/staged-registry-3.json` passes ten
read-only calls through installed Hermes registry, staged source and live API.
Server SHA d7885c0c36738ce0f10dca7fd5e6e8253144e32bf59a154fe3539e88504747c3.
114 bridge tools were discovered because the concurrent 16 operation additions
were also present, plus four Hermes utilities = 118 registered entries.
**Only the seven diagnostic additions and contract were exercised. This is not
acceptance of all 114 tools, an existing Signal session or admin consent.**

`staged-registry-4.json` additionally passes actual Signal platform selection
and native tool_search/tool_describe for all seven additions. Operations file
pin15eb2e205fce60ee8c1d57da1871bd0b9448184f558900fa24091422a2918854 was checked
before and after the fresh-process test; no operations were invoked.
`staged-registry-5.json` passes the same tests against contract2026-09-08.2,
serverSHA6f88461228204c97fba9e3bd84cc3670d2474fc13f439a823e0ec28502cd74c2,
and the operations hash returned by the live MCP process. Registration takes
5.101s total including the ten API reads. This is not a throughput benchmark.
Initial probe failures were instrument parsing of unstructured multi-item
results. New diagnostics now return one structured envelope. Failure evidence
is retained; no model request was made by these probes.

ARIA task 6aa0233942b6580c5f1742c1 tracks Ben's added explicit goal. The thread
goal tracker rejects a second active goal; deployment goal remains unfinished.

## Deployment coordination boundary

No bridge files, Hermes config/plugin, API files or gateway process have been
installed/restarted by this session for the exposure task yet. Existing live
bridge was contract 2026-09-02.1, SHA a430192d0fbf787c4995d14466869b07021d89929cd8edbe3fa1b0c0c9581527.
Please coordinate one rollout owner; do not independently replace shared files.
Prefer an explicit `/reload-mcp` with its normal consent over a gateway restart.
Fresh-process discovery is not proof that the existing Signal process reloaded.
No admin credential or controller policy should be changed implicitly.

**Do not restart ARIA or any model while qualification is active.** Its Mac
watched shell is `claude-flashnext-3090-hardware-check`; task
6a9d2c610014ac24f49238a8. It passed 8/8 near-245K reliability checks and is now
generating 164 HumanEval+ solutions on the same PID59775/profile
ebaaa8da7a718f06623389c11f1d93bf4eb9824fe382b28140c72c03d4ac76dd.
Remote observer `claude-flashnext-cuda-build` is on telemetry part3 until about
16:06:40UTC. Semantic evaluation, 2-hour soak and final cutover remain.
No commits, resets, publication or Ralph authoritative changes for this handoff.
