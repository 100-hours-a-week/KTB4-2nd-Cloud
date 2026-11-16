# W1 / W1K: processing-time converter fault

Preparation only. A merge runs synthetic CI; it does **not** install resources,
kill an operating Pod, upload photos, or invoke an AWS/GPU lifecycle API.

Legacy W1 uses Pod deletion. W1K uses runtime container SIGKILL; opt in explicitly.
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
for the victim name only. Existing Kafka roles/TLS stay intact. Only the legacy PodKill path leaves the daemon placement unchanged; W1K follows the separate placement sequence below.

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
   Create the empty trial and record its ID first, but do not accept/upload the photos yet.
   Start `run`, then release the prepared input only when its `input-ready.json`
   matches this Plan. A fixed sleep from process start is not an arming signal.
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
isolated real Kafka/MySQL, a synthetic JDBC adapter and synthetic photo IO. An external
`docker kill --signal=KILL` kills the waiting JVM container; exit137 without OOM,
no early commit and rejection of the old lease are required. The harness then expires
**only the synthetic fixture lease** to finish promptly. It proves reuse/retry in
that harness, not production JPA, native conversion or a 600s operating recovery.


## W1K: runtime kill and lease-expiry recovery

W1-r8 preserved all20 outputs, but its selected in-flight photo finished before the
process exited; all attempts remained1. That proves completed output preservation,
not recovery of that interrupted photo. Keep the recorded result, do not relabel it.

Pinned source distinguishes the two failure models:

- [PodKill](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.8/controllers/chaosimpl/podchaos/podkill/impl.go)
  calls Kubernetes Pod deletion. Even grace0 is not a runtime SIGKILL guarantee;
  [kubelet](https://github.com/kubernetes/kubernetes/blob/v1.36.5/pkg/kubelet/kuberuntime/kuberuntime_container.go)
  retains a minimum stop window. A graceful worker can finish/release work in it.
- [ContainerKill](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.8/controllers/chaosimpl/podchaos/containerkill/impl.go)
  calls Chaos Daemon. Its pinned
  [containerd client](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.8/pkg/chaosdaemon/crclients/containerd/client.go)
  sends `SIGKILL`. The selector is one exact photo Pod and `containerNames: [backend]`.
  Container lookup is name based, not an atomic containerID precondition; qualify
  the recorded old container's death after execution, not merely AllInjected.

### Placement and execution

1. While the normal Kafka Controller profile and its system-host daemon are active,
   collect a fresh `snapshot`, then `daemon-plan --snapshot ...`. Review the exact
   DaemonSet UID/spec, original system Node UID, victim Node UID/runtime and hash.
2. Separately authorize `daemon-apply --approve-daemon-sha256 ...
   --ack-one-host-privileged-daemon --receipt ...`. It temporarily moves the existing
   **same** DaemonSet to that one Ready CPU Worker. No added Node or DaemonSet;
   desired daemon count1, unchanged image/50m64Mi request/300m128Mi limit/TLS/socket.
   During reconciliation old/new daemon Pods can briefly overlap. Readiness and a
   matching target-node EndpointSlice are required before arming input.
3. This daemon is **privileged, hostPID, with host-wide containerd authority**. Exact
   scheduling and experiment names restrict intended targets, not kernel authority.
   Review this expanded host scope before live apply. No Pod-delete Role is installed
   for W1K. Existing daemon RPC discovery/TLS are reused without reading Secret values.
4. Apply the existing `profile-plan`/`profile-apply` photo Controller profile. No
   NetworkChaos/other PodChaos may coexist. Start the metadata reader last, after
   daemon/Controller readiness; its1050s lifetime must cover20s search+900s observation.
   W1K rejects a reader over90s old, using the reader Node's UTC reference.
5. Prepare a fresh Plan with `plan --failure-mode container-kill --daemon-plan ...`.
   Plan pins the exact victim and approved daemon Plan; changed Pod/container/Node,
   app template, TLS/spec or receipt blocks injection. Start the independent GPU
   stop guard and `run --daemon-receipt ...
   --ack-maintenance-and-one-container-kill` with the existing Plan/profile/reader
   flags. Release input only after the matching `input-ready.json`.
6. Search the existing20s window for2–4 saved photos and one unsaved active attempt
   with a live claim; verify the same uncommitted partition owner and member hash.
   Never delay actual photo conversion or shorten the600s lease to manufacture a hit.
7. Create exactly one PodChaos. No automatic repeat after a miss, timeout or uncertain
   response. Before success assessment require all of:
   - Chaos record injected for the exact `photo/Pod/backend`;
   - old containerID exited137, not OOMKilled, in the same Pod UID;
   - a **new DB read after observing termination** still has the selected photo
     unsaved, unchanged attempt and the old RUNNING claim with positive lease;
   - new containerID and a new Kafka member hash (same Pod UID/IP is valid);
   - old lease expiry and new claim observed as intervals, selected attempt retried,
     saved photos unchanged, all20 accepted identities present exactly once in DB;
   - no observed early lease release, no observation gaps/manual intervention, and
     matched async Trace evidence (the killed unexported Span itself may be absent).
8. `summarize` keeps DB/observer clock bounds. Use the exact pre-injection DB sample
   whose hash is `selectedProofSha256` for `assess --before`; a different sample or
   another photo's retry cannot establish the selected-photo result. Crossing a timing
   target remains unassessed. Lease clear/release and expiry recovery are separate paths.
9. Cleanup signals GPU stop first, finalizes the owned PodChaos, removes the reader,
   restores the daemon to its original system-host spec/UID/TLS, then the Kafka
   Controller profile. Daemon retarget failure journals intent and attempts restore.
   A CLI preflight failure signals the guard; before entering `run`, the external
   coordinator must also invoke `reader-stop`, `daemon-restore`, `profile-restore`
   from saved receipts. Foreign edits/unknown CR UID retain an explicit manual review.

The15s eligibility-to-claim and240s claim-to-completion targets and900s diagnostic
budget remain unchanged. This case measures lease recovery, not node loss, disk loss
or split brain. CI uses external Docker SIGKILL with actual BE Worker/Listener classes;
it does **not** prove Chaos Daemon RPC compatibility on the operating EKS/containerd
version, real600s expiry, production JPA, native conversion or operating recovery time.
Those require the separately approved live W1K result. Existing images/specs/APM,
HPA/KEDA, offsets/history and GPU configuration are preserved.
