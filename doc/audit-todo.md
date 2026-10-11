# Remaining audit items

Uncrossed items remain unresolved.

1. ~~**SkyPilot SSH-key isolation.**~~ Closed by decision: accept the upstream
   SkyPilot behavior; no repository fix planned. SkyPilot reuses a per-user SSH
   key and can copy it to RunPod workers. This is not a confirmed exploit on this
   stack.
2. ~~**Hub replacement on Tailscale key renewal.**~~ Fixed in
   `infrastructure/hub_server.py`: ignore rendered `userData` changes while using
   the cloud-init template hash as a VM replacement trigger. Keep the official
   providers, single-use keys, and key-first dependency. Accepted limitation:
   VM-only replacement can reuse a consumed key unless refresh observes its
   invalidity or the operator explicitly replaces the key too. Creation and SSH
   connection error hooks print a recovery command targeting both resources.
   Verified on 2026-10-10 with Pulumi 3.268.0, Tailscale 0.29.1, and Hetzner 1.43.0
   using isolated previews, synthetic state, and a fake Tailscale API: invalid
   and missing keys replace only the key, template edits replace the VM, and
   forced VM replacement emits the hint. A controlled localhost SSH failure
   using Command 1.2.1 preserved the original error and printed recovery guidance.
   Validation covers planning and hints, not live VM bootstrap or replacement.
3. ~~**Concurrent SkyPilot configuration writes.**~~ Fixed in
   `resources/skypilot_config.py`: compare the exact stored YAML before updating;
   insert an absent row only if another writer has not already inserted it.
   Policy application and removal reread on conflict, with five write attempts
   maximum. SQL failures and timeouts still stop immediately, and successful
   writes still verify the policy field. Verified on 2026-10-11 using mocked
   lifecycle tests and isolated PostgreSQL 17.9, including a stale update waiting
   for a competing transaction's row lock. No deployed cluster was changed.
4. ~~**Code cleanup.**~~ Removed redundant comments, banners, and verbose
   docstrings. Shared in-memory Kubernetes client construction, closed provider
   clients and HTTP responses, bounded cleanup requests, and removed secret
   variable shadowing. Corrected the integration policy test's outdated helper
   calls and restored only its policy during teardown. Cloud-init remains unchanged
   because even comment edits trigger VM replacement. Larger design decisions are
   listed below.
   Verified on 2026-10-11 with 112 isolated tests, 182 subtests, Ruff, shell syntax
   checks, and a comparison of 43 mocked Pulumi registrations. Resource names,
   parents, providers, dependencies, secret outputs, and replacement options match.
   Updated policy files and their rollout checksum are intentional; provider code
   and source metadata were excluded from the comparison. No live stack was tested.
5. ~~**Unpinned worker dependencies.**~~ The MLflow integration job uses a
   dedicated uv project with pinned MLflow and boto3 versions, a committed
   transitive dependency lockfile, Python 3.14.8, and uv 0.13.0. Setup and execution
   use `--locked`. General SkyPilot jobs retain their own dependency setup.
   Verified on 2026-10-11 by running the YAML setup and payload locally against
   SQLite-backed MLflow: parameters, metrics, and the artifact matched; an outdated
   lockfile was rejected without changing it. SkyPilot parsed the updated task.
   No live RunPod job or deployed MLflow server was tested.
6. **Application deployment structure.** `infrastructure/apps.py` combines
   namespaces, network policies, operators, databases, and both applications in
   one module that registers resources on import. Proposed follow-up: separate
   deployment modules with explicit inputs while retaining resource names,
   parents, providers, and dependencies. Decide the structure before moving code
   or introducing Pulumi components.
7. **Live backup test lifecycle.** The backup and recovery fixtures clean up only
   after reaching `yield`; setup errors can leave the sentinel table, Backup CR,
   or recovery Cluster behind. They also use unchecked SQL exec and a fixed
   `postgres-1` primary. Proposed follow-up: checked SQL on the discovered primary,
   cleanup on setup failure, and a verified wait for recovery pod deletion.
