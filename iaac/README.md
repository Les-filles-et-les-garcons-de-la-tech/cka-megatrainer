# iaac: checking the answers on a real Kubernetes

The game teaches commands and YAML. This folder runs them against a real **Kubernetes v1.37**
cluster (kind), so that **the tests, not our memory, are the source of truth** for every question:
if the game says a command is correct, a test has typed that exact command and the cluster
accepted it; if an explanation claims "this is rejected" or "this times out", a test has
provoked it.

```
iaac/
├── cluster.sh        create / check / delete the test cluster (kind, v1.37.0)
├── addons.sh         Gateway API CRDs + metrics-server (what a bare kind cluster lacks)
├── kind/cluster.yaml one control-plane node + one worker
├── run-tests.sh      run the tests against that cluster
├── PENDING.txt       questions whose cluster test is still to be written (should stay empty)
└── tests/
    ├── lib.py                  helpers (answer(), final_file(), prefill(), node(), wait_for()…)
    ├── test_manifests.py       every correct edit-step file is accepted by the v1.37 API server
    ├── test_cluster.py         CLU-*  cluster architecture, installation, configuration
    ├── test_workloads.py       WRK-*  workloads and scheduling
    ├── test_storage.py         STO-*  storage
    ├── test_networking.py      NET-*  services and networking
    ├── test_troubleshooting.py TRB-*  troubleshooting
    ├── test_destructive.py     TRB-/CLU-  break and repair the control plane (opt-in)
    └── test_coverage.py        every question has a test (needs no cluster)
```

## Quick start

You need **podman or docker** (kind runs the nodes as containers), **python3**, **curl** and network
access. With podman on a Mac, the podman machine needs about 6 GB of memory.

```bash
make iaac-up              # about 2 minutes the first time (downloads the node image)
make iaac-test            # run everything that is safe: about 15 minutes
make iaac-test ARGS=test_storage                       # one file
make iaac-test ARGS=test_storage.Reclaim               # one class
make iaac-test ARGS=test_storage.Reclaim.test_sto006_retain_keeps_the_volume_when_the_claim_goes
make iaac-test-destructive                              # also break and repair the control plane
make iaac-down            # delete the cluster
make iaac-coverage        # which tests exercise which question (no cluster needed)
```

`make test` also runs the coverage check, which needs no cluster.

### What it will and will not touch

- **Your kubeconfig is never modified.** The cluster has its own file, `iaac/.kubeconfig`, which
  holds only this cluster. Your `~/.kube/config`, your current context and your other clusters are
  not read or changed.
- Everything downloaded stays in `iaac/.bin/` (kind, and a `kubectl` of the cluster's own version,
  both checksum-verified) and `iaac/.helm/` (Helm's repositories). Both are git-ignored. Your
  system `kind`, `kubectl` and Helm configuration are not used or changed.
- The tests create namespaces named after the questions (`shop`, `prod`, `team-a`…) and delete them
  when they finish. `KEEP=1 make iaac-test` keeps them so you can look around with
  `iaac/.bin/kubectl --kubeconfig iaac/.kubeconfig …`.
- The cluster is disposable. If something is left in a strange state, `make iaac-down iaac-up`.

`K8S_VERSION` (default `v1.37.0`), `KIND_VERSION` and `CLUSTER` (default `cka-test`) can be
overridden in the environment.

## How the tests are written

A test does not retype the game's commands. It **takes the correct option of the step from
`questions.json`**:

```python
self.answer_ok("CLU-001", "s1")          # runs the stored correct command of step s1, expects exit 0
final_file("WRK-001", "s2")              # the file a player ends up with after the correct edit
prefill("STO-001", "s2")                 # the file the player starts from
node("crictl ps -a")                     # run inside the control-plane node (a systemd machine)
wait_for("kubectl get pod x", "Running") # poll until the output contains the text
```

So a typo, a stale flag or a renamed field in the database fails here. Beyond "the answer works",
a test also checks **the claims of the `explain` and `why` texts** where a cluster can show them:

```python
self.fails("kubectl patch sc local-wait -p '{\"reclaimPolicy\":\"Delete\"}'")   # "cannot be changed later"
```

Every scenario is built in a small fixture first (a Deployment, a broken Pod, a PV…), because the
game gives you the cluster already in that state.

### Adding a test for a new question

1. Add the question to `questions.json` (see `CONTRIBUTING.md`).
2. Open the test file of its domain and add a method named after the question id. Prefer one test
   per question, and keep the id in the name (`test_sto011_…`): the coverage test and
   `make iaac-coverage` find it from there.
3. Build the situation the scenario describes, run the stored answers with `answer_ok(...)`, and
   assert what the explanation promises.
4. Run just that test: `make iaac-test ARGS=test_storage.Classes.test_sto011_…`
5. If you cannot run a cluster, add `STO-011 <why not>` to `iaac/PENDING.txt` instead. The maintainer
   will write the test; `make test` stays green, and the list stays visible.

A minimal example:

```python
def test_sto004_hostpath_pv_is_available(self):
    self.cleanup_cmd("kubectl delete pv pv-log")                 # always clean up
    with tempfile.TemporaryDirectory() as d:
        Path(d, "pv.yaml").write_text(final_file("STO-004", "s1"))
        self.answer_ok("STO-004", "s2", cwd=d)                   # kubectl apply -f pv.yaml
    self.assertIn("Available", self.answer_ok("STO-004", "s3").out)
```

`apply()` deletes what it created when the test ends, and `cleanup_cmd()` runs a command after the
test whether it passed or not (hangs and errors in cleanup are ignored).

## When a test fails

Decide which of three things is wrong, in this order:

1. **The game is wrong**: the answer is rejected, or the explanation claims something the cluster
   contradicts. Fix `questions.json`. (This has happened: see "What this suite found" below.)
2. **The test is wrong**: a timing assumption (`wait_for` instead of reading once), a name that
   changed, a Pod count that includes Terminating Pods. Fix the test.
3. **The environment is different**: see the limits below. Fix the test so it checks what the
   environment can show, and say so in a comment.

## Known limits

- **The scenarios use made-up hosts and images.** `node01` becomes the worker (`cka-test-worker`),
  `controlplane` becomes `cka-test-control-plane`, `registry.local/…` images are replaced by real
  ones. The tests substitute them where needed.
- **kindnet and NetworkPolicy.** kind's CNI enforces policies, but next to a `default-deny` policy a
  *same-namespace* `podSelector` allow rule is not honoured (a `namespaceSelector` one is, NET-009).
  NET-001 therefore checks its allow policy on its own. Not verified on another CNI.
- **No real join or init.** A second kubeadm `init` or `join` cannot run on these nodes, so CLU-005
  and CLU-006 check the generated join command, the config (`kubeadm config validate`), the rule that
  `--config` cannot be mixed with flags, and the group in `admin.conf`.
- **Ingress controllers.** There is no Ingress or Gateway controller, so NET-011..016 check that the
  objects are accepted and shaped as the explanation says, not that traffic flows.
- **The version.** Everything runs on v1.37.0; a claim about another release is not verified.
- **Destructive tests** change the control plane for real (API server, scheduler, controller manager,
  kubelet, kube-proxy, certificates), so they are opt-in. They restore what they touch, even on
  failure. If a run is killed half-way, recreate the cluster.

## What this suite found

Running the answers for real corrected the game in these places (kept here so the reasoning is not
lost):

| Question | What was wrong | Fix |
|---|---|---|
| WRK-012 | `until nslookup db` never ends with busybox 1.36: it tries every search domain and exits 1 if one fails, even though the Service resolves | the loop uses the full name `db.shop.svc.cluster.local` |
| TRB-003 | `nslookup kubernetes.default` with busybox 1.36 answers NXDOMAIN on a healthy cluster | the check uses `busybox:1.28`, which resolves it |
| TRB-011 | `kubectl wait pod -l app=…` fails right after a rollout: the old Pods still match the label and never become Ready | wait for the Deployment's `Available` condition instead |
| STO-003 | "existing claims are not changed" is wrong: the new default is also given to claims that have none (retroactive, GA in 1.28); with two defaults the newest wins | explanations corrected |
| CLU-013 | containerd 2.3's default config is `version = 4`, not 3 | scenario and excerpt updated |
| TRB-017 | `kubectl top nodes` columns are `CPU(%)` and `MEMORY(%)` | explanation corrected |
