# aid-zuul-config

Zuul's trusted config project for AIDepartment (AIDepartment#1341, #1344, #1346): the pipelines, the base job, and
the jobs with secrets and their secrets (AIDepartment#1351). AIDepartment's own jobs are not here: they are in AIDepartment's
`ci/zuul/`, which says how the two fit together (its `ci/zuul/README.md`).

| Path | What |
| --- | --- |
| [`zuul.d/pipelines.yaml`](zuul.d/pipelines.yaml) | `check`, `check-external` and `gate`, reported as check runs of the App `aid-zuul`, and `hold`, which takes a pull request out of `gate` and reports nothing. AIDepartment's `ci/zuul/gate.md` says how `gate` and `hold` do what Mergify does |
| [`zuul.d/jobs.yaml`](zuul.d/jobs.yaml) | `base`, every job's parent: one build node, a 2-hour timeout, and the checkout |
| [`playbooks/base/`](playbooks/base) | the base job's playbooks |
| [`zuul.d/project.yaml`](zuul.d/project.yaml) | this repository's own `check`, which validates a pull request here and runs nothing |
| [`zuul.d/release.yaml`](zuul.d/release.yaml) | `aid-release-credentials`, the release's credential check, and where it runs: `post` for AIDepartment |
| `zuul.d/secrets.yaml` | the secrets, each encrypted for this project's key by AIDepartment's `deploy/zuul/encrypt-secret.sh` |
| [`playbooks/secrets/guard.yaml`](playbooks/secrets/guard.yaml) | the pre-run of every job with a secret |
| [`playbooks/release/`](playbooks/release) | the release's credential check: a token of the App `aid-release`, made, checked and revoked |

## Trusted

This repository is the security boundary of AID's CI (Kristóf's decision on AIDepartment#1346): what is in it decides
what every job may do and see. Only a config project may define pipelines, a job with no parent and the providers of
build nodes; its playbooks run outside the sandbox that untrusted playbooks get, and see the secrets it defines. So
Zuul never runs a proposed change to it: a pull request here is checked for configuration errors, and takes effect
once it is merged, when Zuul loads `main` again.

AIDepartment is an untrusted project: a pull request there that adds or changes a job runs it on itself, but it can
define no pipeline and no base job, and Zuul refuses either with a configuration error.

## Changing it

- **Never merge here yourself, and never push to `main`.** `main` is protected: a change is a pull request, reviewed
  by the other bot, and merged by the orchestrator. Nothing merges into it automatically, Zuul included.
- **Test it first on AIDepartment's test bed** (its `ci/zuul/README.md`, "The test bed"): it runs Zuul with a copy of
  this repository's branch as the config project and AIDepartment's jobs as the untrusted project, and checks that the
  layout loads and that AIDepartment's pull requests still run speculatively. Link the run in the pull request.
- Zuul's `check` on the pull request reports a configuration error, if any, before the merge.
- After the merge, `curl <zuul-web>/api/tenant/aid/config-errors` is empty if it loaded.

## The build node

`zuul.d/providers.yaml` registers one PR-only k3s pod as `aid-node` (AIDepartment#1345). Its deployment, checked image,
network policies and quota are AIDepartment's `deploy/zuul/node/`. Zuul's static provider assigns one job at a time;
`base` bounds a job at 7200 seconds. The shared connection `aid-nodes` is `driver=static` in the deployed `zuul.conf`.

The Kubernetes driver in Zuul 14.3.0 creates namespaces and RBAC for each pod, which needs cluster-wide powers the
tenant does not have, and has no hook to create the namespace's quota and network policy before starting the pod.
For now the admin creates a bounded namespace and Kubernetes keeps its one pod alive. Jobs share its PR trust level
and scratch. Its projected ServiceAccount token gives only `dev` at the farm; no job on this node can write the
trusted cache. Main's post jobs need a separate node and a post-only secret (#1351), never a credential on `aid-node`.

`check-external` is blocked by the base job's trusted pre-run until a separate credential-free execution boundary
exists. It fails before checkout and before any untrusted pre/run/post/cleanup playbook, and tells Zuul not to retry.
The shared pod's raw projected token is readable by its job user; a conditional credential helper cannot secure that
pipeline. Do not remove the guard while external jobs could use `aid-node`. The standard Compose test bed seeds a
fake readable farm token and proves that all four malicious hooks run in `check` and none run in `check-external`.

The node's SSH host key is pinned in the provider. On rotation, generate the new SSH Secrets in AIDepartment, update
`host-key` here in the same rollout, and restart the pod and executor. Only the public key belongs here. The private
keys stay in AID's keychain and Kubernetes Secrets.

## Jobs with secrets

AIDepartment's `deploy/zuul/job-secrets.md` has the model: each secret's plaintext is in AID's keychain, and jobs get
it only as a secret here, encrypted for this project's key. A secret is defined only here, never in AIDepartment, and a
job that names one:

- is defined here, in a file of `zuul.d/` with only such jobs and where they run (the test bed leaves those files out);
- is `final: true` and `post-review: true`, with `allowed-projects`;
- runs with `nodeset: {nodes: []}`, or on a node of its own, never `aid-node`;
- starts with `pre-run: playbooks/secrets/guard.yaml`, and sets `secret_pipelines` and `secret_projects` in its `vars`.

To add or rotate a secret, encrypt it from the keychain with `deploy/zuul/encrypt-secret.sh --expect-key <the key in
job-secrets.md>`, and replace its stanza in `zuul.d/secrets.yaml` by a pull request. Zuul's `check` here decrypts it
as it loads the proposed configuration, so a stanza for another key is reported there. After the merge, the job's next
run uses it.
