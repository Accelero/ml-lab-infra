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
3. **Concurrent SkyPilot configuration writes.**
   `resources/skypilot_config.py` reads, modifies, and overwrites the entire
   persisted YAML row. Another writer can change it between the read and write,
   causing unrelated configuration changes to be lost. Add transactional locking
   or optimistic concurrency control.
4. **Code cleanup.** Finish reviewing comments, docstrings, and structure;
   remove obvious commentary and unnecessary verbosity. This is cleanup work,
   not a confirmed additional security or correctness finding.
5. **Unpinned worker dependencies.** `tests/jobs/mlflow_train.yaml` installs
   `mlflow` and `boto3` without version pins. Make worker environments
   reproducible; updating the repository's `uv.lock` does not pin these installs.
