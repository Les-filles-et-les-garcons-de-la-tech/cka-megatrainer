"""Cluster tests for WRK-016..018 (PDB, DaemonSet, HPA v2) and STO-011..012 (StatefulSet claims, access modes).

Cluster: kind v1.37, 1 control plane (tainted) + 1 worker, StorageClass 'standard' = rancher
local-path (WaitForFirstConsumer), metrics-server installed. Scenario names such as node01 are
replaced by WORKER; classes that do not exist on kind are replaced (see each test).
"""
import json
import tempfile
import time
from pathlib import Path

from lib import CONTROL_PLANE, WORKER, ClusterTest, answer, final_file, prefill, settle_node, sh, t, wait_for, Q


def traps(qid, sid):
    """(option, file) for every wrong snippet of an edit step, as the player would leave the file."""
    step = Q[qid]["steps"][sid]
    return [(o, t.apply_snippet(step, t.lines_of(o["snippet"]))) for o in step["options"] if not o.get("correct")]


def rejected(text):
    """True when the API server refuses the manifest (nothing is stored: --dry-run=server)."""
    return not sh("kubectl apply --dry-run=server -f -", stdin=text)


def jsonpath(kind, name, ns, path):
    return sh(f"kubectl get {kind} {name} -n {ns} -o jsonpath='{{{path}}}'").out.strip("'")


def wait_value(kind, name, ns, path, want, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if jsonpath(kind, name, ns, path) == want:
            return True
        time.sleep(2)
    return False


def deployment(name, ns, replicas, image="nginx:1.25", resources=""):
    return f"""apiVersion: apps/v1
kind: Deployment
metadata: {{name: {name}, namespace: {ns}}}
spec:
  replicas: {replicas}
  selector: {{matchLabels: {{app: {name}}}}}
  template:
    metadata: {{labels: {{app: {name}}}}}
    spec:
      containers: [{{name: {name}, image: "{image}"{resources}}}]
"""


class NewWorkloads(ClusterTest):
    def test_wrk016_pdb_holds_a_drain(self):
        # cleanups run in reverse: the wait for kube-system / metrics-server runs LAST, after the uncordon
        self.cleanup_cmd("kubectl wait --for=condition=Ready pods --all -n kube-system --timeout=120s; "
                         "kubectl rollout status deploy/metrics-server -n kube-system --timeout=120s")
        self.cleanup_cmd(f"kubectl uncordon {WORKER}")
        self.cleanup_cmd("kubectl delete pdb api-pdb -n shop")
        self.apply(deployment("api", "shop", 3))
        self.ok("kubectl rollout status deployment/api -n shop --timeout=120s")
        # step 1 trap: both bounds at once are refused by kubectl
        self.fails("kubectl create pdb x --selector=app=api --min-available=2 --max-unavailable=1 -n shop",
                   "cannot be both specified")
        self.answer_ok("WRK-016", "s1")
        self.assertTrue(wait_value("pdb", "api-pdb", "shop", ".status.disruptionsAllowed", "1", 60))
        row = [ln for ln in self.answer_ok("WRK-016", "s2").out.splitlines() if ln.startswith("api-pdb")][0]
        self.assertEqual(row.split()[1:4], ["2", "N/A", "1"])                 # MIN, MAX, ALLOWED DISRUPTIONS
        # step 3: every api Pod is on the worker, and the tainted control plane takes no replacement,
        # so the drain evicts ONE api Pod (1 allowed) and then keeps being refused by the budget
        drain = answer("WRK-016", "s3").replace("node01", WORKER) + " --timeout=60s"
        settle_node()
        res = self.fails(drain, timeout=150)
        self.assertIn("disruption budget", res.out)
        running = sh("kubectl get pods -n shop -l app=api --field-selector=status.phase=Running -o name").out.split()
        self.assertEqual(len(running), 2, "the budget must keep 2 api Pods running")
        self.assertEqual(jsonpath("pdb", "api-pdb", "shop", ".status.disruptionsAllowed"), "0")

    def test_wrk017_daemonset_from_a_deployment_on_every_node(self):
        self.cleanup_cmd("kubectl delete ds log-agent -n logging --ignore-not-found")
        with tempfile.TemporaryDirectory() as d:
            self.answer_ok("WRK-017", "s1", cwd=d)
            generated = Path(d, "ds.yaml").read_text()
        for line in ("kind: Deployment", "  replicas: 1", "  strategy: {}", "        name: fluent-bit"):
            self.assertIn(line, generated)                                      # the prefill matches kubectl
        self.assertEqual(sh("kubectl get deploy log-agent -n logging -o name").rc, 1)  # dry run: nothing created
        # step 2 context: kind changed alone is refused for the leftover fields
        res = self.fails("kubectl apply -f -", stdin=prefill("WRK-017", "s2"))
        self.assertIn('unknown field "spec.replicas"', res.out)
        self.assertIn('unknown field "spec.strategy"', res.out)
        for o, text in traps("WRK-017", "s2"):
            self.assertTrue(rejected(text), o["snippet"])
        self.ok("kubectl apply -f -", stdin=final_file("WRK-017", "s2"))
        self.assertTrue(wait_value("ds", "log-agent", "logging", ".status.desiredNumberScheduled", "1"),
                        "without a toleration only the worker is covered")
        self.ok("kubectl apply -f -", stdin=final_file("WRK-017", "s3"))
        self.assertTrue(wait_value("ds", "log-agent", "logging", ".status.numberReady", "2", 180))
        out = self.answer_ok("WRK-017", "s4").out
        self.assertIn(CONTROL_PLANE, out)
        self.assertIn(WORKER, out)
        # step 3 traps: none of them reaches the control plane
        for o, text in traps("WRK-017", "s3"):
            self.ok("kubectl apply -f -", stdin=text)
            want = "0" if "nodeSelector" in text else "1"           # nodeSelector also drops the worker
            self.assertTrue(wait_value("ds", "log-agent", "logging", ".status.desiredNumberScheduled", want, 60),
                            o["snippet"])
            end = time.time() + 60                                  # the control-plane Pod is removed
            while time.time() < end and sh("kubectl get pods -n logging -l app=log-agent "
                                           f"--field-selector=spec.nodeName={CONTROL_PLANE} -o name").out:
                time.sleep(2)
            self.assertEqual(sh("kubectl get pods -n logging -l app=log-agent "
                                f"--field-selector=spec.nodeName={CONTROL_PLANE} -o name").out, "", o["snippet"])

    def test_wrk018_hpa_memory_and_slow_scale_down(self):
        self.apply(deployment("worker", "jobs", 2, resources=", resources: {requests: {memory: 256Mi}}"))
        self.ok("kubectl rollout status deployment/worker -n jobs --timeout=120s")
        self.assertIn("stabilizationWindowSeconds", self.answer_ok("WRK-018", "s1").out)
        self.fails("kubectl explain hpa.behavior.scaleDown")
        self.fails("kubectl explain hpa.spec.behavior.scaleDown --api-version=autoscaling/v1")
        for sid in ("s2", "s3"):
            for o, text in traps("WRK-018", sid):
                if any("scaleUp" in ln for ln in o["snippet"]):
                    continue                     # valid YAML, wrong direction: checked through the stored object
                self.assertTrue(rejected(text), (sid, o["snippet"]))
        self.apply(final_file("WRK-018", "s3"))
        policy = json.loads(jsonpath("hpa", "worker", "jobs", ".spec.behavior.scaleDown") or "{}")
        self.assertEqual(policy.get("stabilizationWindowSeconds"), 600)
        self.assertEqual(policy.get("policies"), [{"type": "Pods", "value": 1, "periodSeconds": 120}])
        # metrics-server feeds the memory target (a % of the 256Mi request)
        res = wait_for("kubectl get hpa worker -n jobs", "%/70%", 180)
        self.assertRegex(res.out, r"memory: \d+%/70%")
        out = self.answer_ok("WRK-018", "s4").out
        self.assertIn("AbleToScale", out)
        self.assertRegex(out, r"(?i)stabilization\s*window:?\s*600")    # describe prints the behavior
        self.assertIn("120", out)


class NewStorage(ClusterTest):
    def test_sto011_one_claim_per_replica_kept_and_reattached(self):
        claims = "data-db-0 data-db-1 data-db-2"
        self.cleanup_cmd(f"kubectl delete pvc {claims} -n data --ignore-not-found")    # runs last
        self.cleanup_cmd("kubectl delete sts db -n data --ignore-not-found --wait=true")
        for o, text in traps("STO-011", "s1"):
            if any("db-data" in ln for ln in o["snippet"]):
                continue           # accepted or refused depending on where the mount is validated; see below
            self.assertTrue(rejected(text), o["snippet"])
        sts = final_file("STO-011", "s1")
        self.ok("kubectl apply -f -", stdin=sts)
        self.assertTrue(wait_value("sts", "db", "data", ".status.readyReplicas", "3", 240))
        self.assertIn("Bound", self.answer_ok("STO-011", "s2").out)
        self.fails(answer("STO-011", "s2").replace("data-db-1", "db-1-data"), "NotFound")
        # step 3 and its traps
        for o in Q["STO-011"]["steps"]["s3"]["options"]:
            if not o.get("correct") and "Remove" in o["cmd"]:
                self.assertIn("Unsupported value", self.fails(o["cmd"]).out)
            elif not o.get("correct") and " pvc " in o["cmd"]:      # a PV field on a claim: warned, ignored
                res = self.ok(o["cmd"])
                self.assertIn('unknown field "spec.persistentVolumeReclaimPolicy"', res.out)
                self.assertIn("no change", res.out)
        self.answer_ok("STO-011", "s3")
        self.assertEqual(json.loads(jsonpath("sts", "db", "data", ".spec.persistentVolumeClaimRetentionPolicy")),
                         {"whenDeleted": "Retain", "whenScaled": "Delete"})
        self.ok("kubectl exec db-0 -n data -- sh -c 'echo kept > /data/marker'")
        # step 4 context: delete the StatefulSet, the claims stay; re-apply, db-0 gets data-db-0 back
        self.ok("kubectl delete sts db -n data --wait=true", timeout=150)
        end = time.time() + 120
        while time.time() < end and sh("kubectl get pods -n data -l app=db -o name").out:
            time.sleep(2)
        for c in claims.split():
            self.assertEqual(jsonpath("pvc", c, "data", ".status.phase"), "Bound", c)
        self.ok("kubectl apply -f -", stdin=sts)
        self.assertTrue(wait_value("sts", "db", "data", ".status.readyReplicas", "3", 240))
        self.assertIn("data-db-0", self.answer_ok("STO-011", "s4").out)
        self.assertEqual(self.ok("kubectl exec db-0 -n data -- cat /data/marker").out, "kept")
        # whenScaled: Delete removes only the claim of the condemned replica
        self.answer_ok("STO-011", "s3")
        self.answer_ok("STO-011", "s5")                                  # the game's scale-down
        end = time.time() + 120
        while time.time() < end and sh("kubectl get pvc data-db-2 -n data -o name").rc == 0:
            time.sleep(3)
        self.assertNotEqual(sh("kubectl get pvc data-db-2 -n data -o name").rc, 0, "data-db-2 must be deleted")
        self.assertEqual(jsonpath("pvc", "data-db-1", "data", ".status.phase"), "Bound")
        # the db-data trap: whatever the stage that refuses it, no replica ever starts
        trap = [text for o, text in traps("STO-011", "s1") if any("db-data" in ln for ln in o["snippet"])][0]
        trap = trap.replace("name: db\n", "name: db-trap\n", 1).replace("app: db", "app: db-trap")
        if sh("kubectl apply -f -", stdin=trap):
            self.cleanup_cmd("kubectl delete sts db-trap -n data --ignore-not-found")
            res = wait_for("kubectl describe sts db-trap -n data", "Not found", 60)
            self.assertIn("Not found", res.out)
            self.assertEqual(sh("kubectl get pods -n data -l app=db-trap -o name").out, "")

    def test_sto012_rwo_rwop_and_read_only_mount(self):
        ns = "ledger"
        writer = ("apiVersion: v1\nkind: Pod\nmetadata: {{name: {pod}, namespace: ledger}}\nspec:\n"
                  "  containers: [{{name: w, image: 'busybox:1.36', command: [sleep, '3600'], "
                  "volumeMounts: [{{name: v, mountPath: /ledger}}]}}]\n"
                  "  volumes: [{{name: v, persistentVolumeClaim: {{claimName: {claim}}}}}]\n")
        pods = "ledger-writer ledger-writer-2 report"
        self.cleanup_cmd("kubectl delete pv rwop-pv archive-pv --ignore-not-found")             # runs last
        self.cleanup_cmd(f"kubectl delete pvc ledger-data ledger-data-v2 archive -n {ns} --ignore-not-found")
        self.cleanup_cmd(f"kubectl delete pod {pods} -n {ns} --ignore-not-found --grace-period=1")
        # step 1: a ReadWriteOnce claim is shared by two Pods of the same node (kind: the only worker)
        self.ok("kubectl apply -f -", stdin="apiVersion: v1\nkind: PersistentVolumeClaim\n"
                "metadata: {name: ledger-data, namespace: ledger}\n"
                "spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 1Gi}}}\n")
        for pod in ("ledger-writer", "ledger-writer-2"):
            self.ok("kubectl apply -f -", stdin=writer.format(pod=pod, claim="ledger-data"))
        self.ok(f"kubectl wait --for=condition=Ready pod/ledger-writer pod/ledger-writer-2 -n {ns} --timeout=120s")
        used_by = self.answer_ok("STO-012", "s1").out
        self.assertIn("ledger-writer", used_by)
        self.assertIn("ledger-writer-2", used_by)
        self.ok(f"kubectl delete pod ledger-writer ledger-writer-2 -n {ns} --grace-period=1 --wait=true")
        # step 2: traps refused or useless; the RWOP claim is served by a static hostPath PV
        # (local-path is not CSI; the scheduler's RWOP check only looks at the claim)
        for o, text in traps("STO-012", "s2"):
            if "ReadOnlyMany" in o["snippet"][0]:
                continue                                                       # valid, just the wrong mode
            self.assertTrue(rejected(text), o["snippet"])
        self.ok("kubectl apply -f -", stdin="apiVersion: v1\nkind: PersistentVolume\nmetadata: {name: rwop-pv}\n"
                "spec:\n  capacity: {storage: 5Gi}\n  accessModes: [ReadWriteOncePod]\n  storageClassName: manual\n"
                "  hostPath: {path: /tmp/rwop-pv, type: DirectoryOrCreate}\n")
        self.ok("kubectl apply -f -", stdin=final_file("STO-012", "s2").replace("csi-standard", "manual"))
        self.ok("kubectl apply -f -", stdin=writer.format(pod="ledger-writer", claim="ledger-data-v2"))
        self.ok(f"kubectl wait --for=condition=Ready pod/ledger-writer -n {ns} --timeout=120s")
        self.ok("kubectl apply -f -", stdin=writer.format(pod="ledger-writer-2", claim="ledger-data-v2"))
        res = wait_for(answer("STO-012", "s3"), "ReadWriteOncePod", 60)
        self.assertIn("ReadWriteOncePod", res.out)                             # FailedScheduling names the mode
        self.assertEqual(jsonpath("pod", "ledger-writer-2", ns, ".status.phase"), "Pending")
        # step 4/5: a ReadOnlyMany claim is still writable until the mount says readOnly
        for o, text in traps("STO-012", "s4"):
            if any(":ro" in ln for ln in o["snippet"]):
                continue                                                       # accepted: a path named '/archive:ro'
            self.assertTrue(rejected(text), o["snippet"])
        self.ok("kubectl apply -f -", stdin="apiVersion: v1\nkind: PersistentVolume\nmetadata: {name: archive-pv}\n"
                "spec:\n  capacity: {storage: 1Gi}\n  accessModes: [ReadOnlyMany]\n  storageClassName: manual\n"
                "  hostPath: {path: /tmp/archive-pv, type: DirectoryOrCreate}\n")
        self.ok("kubectl apply -f -", stdin="apiVersion: v1\nkind: PersistentVolumeClaim\n"
                "metadata: {name: archive, namespace: ledger}\nspec: {storageClassName: manual, "
                "accessModes: [ReadOnlyMany], resources: {requests: {storage: 1Gi}}}\n")
        self.ok("kubectl apply -f -", stdin=prefill("STO-012", "s4"))         # no readOnly yet
        self.ok(f"kubectl wait --for=condition=Ready pod/report -n {ns} --timeout=120s")
        self.answer_ok("STO-012", "s5")                                       # ROX did NOT protect the volume
        self.ok(f"kubectl delete pod report -n {ns} --grace-period=1 --wait=true")
        self.ok("kubectl apply -f -", stdin=final_file("STO-012", "s4"))
        self.ok(f"kubectl wait --for=condition=Ready pod/report -n {ns} --timeout=120s")
        res = self.fails(answer("STO-012", "s5"))
        self.assertIn("Read-only file system", res.out)
