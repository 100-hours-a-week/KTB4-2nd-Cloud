# W1: processing-time converter fault

Preparation only. A merge runs synthetic CI; it does **not** install resources,
kill an operating Pod, upload photos, or invoke an AWS/GPU lifecycle API.

The fixed contract is `contract.json`: current BE image, 600s lease, 30s heartbeat,
3s technical retry. Keep image, 1Gi, APM, autoscaling and NodePool limits unchanged.
Do not shorten the lease before measuring the first result.

## Execution boundary

- One Seongnam JPEG20 trial, 40,302,057 bytes, one concurrent trip.
- Start with one Ready converter; keep HPA/KEDA active and record actual scale changes.
- Seek a task with at least two persisted photos and one in-flight unsaved photo.
  Match its published partition and uncommitted offset to the exact Pod IP/UID.
- Search at most 20s. A missing/stale/ambiguous state, completed task or observation
  failure prevents injection. No artificial photo delay is added to the app.
- One `pod-kill` PodChaos, zero grace, exact Pod name. Creating it is the one-shot injection; pause annotations do not defer it.
- Register name-scoped RBAC before observing. Validate partial work, target UID and freshness before the sole PodChaos CREATE; journal possible injection before sending that request.
- A CREATE timeout is an ambiguous injection, never evidence that no Pod was killed. Preserve its receipt for manual UID review; do not retry CREATE.
- Pinned upstream semantics: [Chaos Mesh 2.8.8 run guide](https://chaos-mesh.org/docs/run-a-chaos-experiment/), one-shot actions ignore pause and duration.
  The local one-shot latch is never removed; a timeout requires receipt inspection.
- This is abrupt Pod deletion, **not** an equivalent node-loss, OOM or split-brain test.
  The final UID check and Chaos API deletion are separate operations; there is no
  atomic UID precondition in the pinned Chaos pod-kill implementation.

## Installation profile

`v3_worker_chaos.py` reuses the existing Chaos controller and public webhook CA.
The profile changes its single-namespace cache from `kafka` to `photo`; it does not
make it cluster-wide or add a daemon/controller/Node. No NetworkChaos may be active
or created during this window. Normal Kafka webhook registrations remain in place.

The profile plan contains the exact original controller spec, webhook arrays,
scoped photo access and pinned PodChaos CRD. Apply and restore compare UID and values;
concurrent edits are not overwritten. A separate temporary Role grants Pod deletion
for the victim name only. Existing Kafka roles/TLS/daemon stay intact.

## Operator sequence

Use `scripts/prepare-v3-worker-fault.py --help` and each subcommand's `--help`.
All evidence directories must be empty, mode 700; files are create-only mode 600.
Run from the repository root with the validation Python environment (PyYAML pinned).

1. Agree a maintenance window and separately review live profile/credential access.
   Check model connectivity without changing GPU state. Capture a fresh `snapshot`.
2. Prepare a `profile-plan`; review its hash; `profile-apply` with the explicit hash
   and acknowledgement. Keep its private undo receipt. No kill occurs in this step.
3. `reader-bundle` compiles the pinned Java metadata reader using the checksum-pinned
   Kafka client JAR. `reader-start` requires its hash and acknowledgement of mounting
   existing `keda/v3-keda-kafka` Describe-only credentials. It reads no Secret via API.
   Select an existing Ready CPU Node with a free Pod slot. The temporary Pod requests
   25m/64Mi, limits 250m/192Mi, has no service-account token or ingress, and only DNS/
   Kafka TLS egress. It never joins a consumer group, consumes, produces or commits.
4. Prepare the sampled input header with the existing workflow Trace helper.
   Create/upload the fixed trial through the normal app; record trip ID and input
   hash. `plan --ack-maintenance` binds these to the fresh runtime snapshot.
   Start `run` while the partial processing window is still available.
5. Before `run`, the authorized external coordinator must start an independent GPU
   stop watchdog and provide a fresh private receipt: `planSha256`, exact existing
   `instanceId`, `independent: true`, live `pid`, UTC `at`, `remainingSeconds` in
   980..1500 and an unused absolute `stopFile` in the same private directory.
   The coordinator must actually honor the stop signal and maximum deadline.
   This tool only checks the receipt/PID and signals that file; **it does not prove
   EC2 stopped**. Verify and preserve the external `stopped` receipt separately.
6. `run` requires plan hash, profile/reader receipts and explicit acknowledgement of
   maintenance and one Pod kill. It owns DB/runtime evidence and cleanup. Do not
   rerun a started plan or use a new directory to evade the one-shot latch.
7. After `run`, `summarize --directory RUN --output TIMING` derives timestamp bounds
   and hashes from recorded DB/runtime files. `assess` compares the before/after
   DB samples, timing, DB summary and existing workflow Trace `observe` report.
   Its Trace ID must match the trial's Outbox carriers. No hand-written `ready=true`
   or Pod Ready alone establishes successful business recovery.
8. `dashboard --record TIMING --output DASHBOARD` prepares a native Grafana Snapshot
   dashboard with recorded photo/attempt/lease samples, boundary timestamps and
   duration bounds. Create a private Grafana Snapshot using `POST /api/snapshots` with
   `{"dashboard": DASHBOARD, "external": false, "expires": 86400}`. Open its returned
   local Snapshot URL, capture the **actual rendered panels**, and inspect labels/time range/units/cropping. This command itself is
   neither a screenshot nor proof that a production experiment occurred.

## Results and safety

- Claim delay target: 15s after eligibility (3s retry + 3s observation + 9s margin).
  Eligibility is constrained by both lease and replacement consumer availability.
- Final completion target: 240s after new claim (normal 3-sample max 200.206262s,
  rounded 210 + 30s margin). Neither target is a statistical SLO or guarantee.
- Diagnostic observation: 900s from injection request. A sample/API deadline shares
  that monotonic budget; it does not receive another full budget at every retry.
- Timestamp uncertainty remains an interval. A target crossed by its uncertainty
  interval stays unassessed. An observed lease gives a lower eligibility bound;
  an unseen renewal must not make a slow recovery look faster.
- Compare every accepted item/final attachment/file identity, attempts and derived
  hashes. Previously saved items must remain unchanged; the interrupted item must
  show a new attempt. Inspect cleanup journal counts separately.
- A killed open Span may never export. Preserve failed-attempt DB/Pod/log evidence
  and recovered Trace; do not replace missing spans with invented topology.
- Observation failures remain gaps even after a successful retry. No exact-once,
  all-S3-side-effect, native performance or operating recovery-time claim from CI.

`finally` signals the external GPU stop guard **before** cleanup. It deletes only
owned UID-matched resources, waits for PodChaos finalization before revoking its
permissions, removes the metadata reader, then restores the original profile.
Pod deletion is irreversible; removing the Chaos object does not resurrect it.
A timed-out create with unknown UID, pending finalizer or concurrent edit remains
explicitly unresolved: preserve receipts, stop further input and review manually.
Do not adopt a matching name or revoke finalizer permissions prematurely. The global
PodChaos CRD is retained; cleanup does not delete CRDs, PVCs, offsets or app data.

## CI evidence

`smoke-v3-worker-fault.py` uses cached/pinned actual Worker/Listener classes with
isolated real Kafka/MySQL, a synthetic JDBC adapter and synthetic photo IO. It halts
the JVM without drain, checks no early commit, rejects the old lease, then expires
**only the synthetic fixture lease** to finish promptly. It proves reuse/retry in
that harness, not production JPA, native conversion or a 600s operating recovery.
