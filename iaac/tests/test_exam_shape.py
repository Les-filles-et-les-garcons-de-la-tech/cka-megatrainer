"""The steps added when hard questions took the exam's shape (prepare, change, wait, verify) and the
other new verification steps, where no older test already runs them.

Runs on kind and on the VM lab. CLU-014's plugin test edits the API server: DESTRUCTIVE=1.
"""
import json
import re
import tempfile
import time
from pathlib import Path

from lib import (CONTROL_PLANE, VM, WORKER, ClusterTest, ControlPlaneTest, answer, final_file, node,
                 set_flags, settle_node, sh, wait_for, wrong)

ETCD_POD = f"etcd-{CONTROL_PLANE}"
PKI = "/etc/kubernetes/pki/etcd"
ETCDCTL = (f"etcdctl --endpoints=https://127.0.0.1:2379 --cacert={PKI}/ca.crt --cert={PKI}/server.crt "
           f"--key={PKI}/server.key")


def in_etcd(cmd):
    """A command inside the etcd static Pod (the image has etcdctl and etcdutl, no shell)."""
    return sh(f"kubectl exec -n kube-system {ETCD_POD} -- {cmd}", timeout=120)


class ExamShape(ClusterTest):
    def test_clu006_clu029_trb036_node_listings(self):
        for qid in ("CLU-006", "CLU-029", "TRB-036"):                   # s5 of each: the cluster's machines
            self.assertIn(CONTROL_PLANE, self.answer_ok(qid, "s5").out, qid)
        self.assertFalse(node("systemctl status kubeadm"))              # CLU-006 trap: not a service

    def test_clu007_drain_uncordon_and_the_unschedulable_taint(self):
        settle_node()
        self.cleanup_cmd(f"kubectl uncordon {WORKER}")
        refused = self.fails(f"kubectl drain {WORKER} --timeout=30s")                # trap: DaemonSet Pods
        self.assertIn("DaemonSet-managed", refused.out)
        # s4 on the worker (kind / the lab run metrics-server, an emptyDir, there: a real control plane does not)
        self.ok(answer("CLU-007", "s4").replace("controlplane", WORKER) + " --delete-emptydir-data", "drained",
                timeout=150)
        self.assertIn("unknown flag: --uncordon", sh(wrong("CLU-007", "s5", 1).replace("controlplane", WORKER)).out)
        # trap: removing the taint by hand does not last while the node is cordoned
        sh(wrong("CLU-007", "s5", 3).replace("controlplane", WORKER))
        back = wait_for(f"kubectl get node {WORKER} -o jsonpath='{{.spec.taints}}'", "unschedulable", 60)
        self.assertIn("node.kubernetes.io/unschedulable", back.out)
        self.ok(answer("CLU-007", "s5").replace("controlplane", WORKER))             # s5
        self.assertNotIn("SchedulingDisabled", self.ok(f"kubectl get node {WORKER}").out)

    def test_clu009_snapshot_status_before_a_restore(self):
        snap = "/var/lib/etcd/iaac-status.db"                                      # hostPath: also on the node
        self.addCleanup(lambda: node(f"rm -f {snap}"))
        self.assertTrue(in_etcd(f"{ETCDCTL} snapshot save {snap}"))
        status = in_etcd(answer("CLU-009", "s0").replace("/opt/etcd-backup.db", snap))   # s0
        self.assertTrue(status, status)
        for col in ("HASH", "REVISION", "TOTAL KEYS"):
            self.assertIn(col, status.out)
        old = in_etcd(wrong("CLU-009", "s0", 1).replace("/opt/etcd-backup.db", snap))    # trap: etcdctl 3.6+
        self.assertIn("Available Commands:\n  save ", old.out)                           # only its help...
        self.assertNotIn("HASH", old.out)                                                # ...no status
        live = in_etcd(wrong("CLU-009", "s0", 2))                                         # trap: a directory
        self.assertFalse(live, live)

    def test_clu023_calico_crds_need_create_not_apply(self):
        if not VM:
            self.skipTest("Calico runs on the VM lab only")
        ver = re.search(r"^calico_version: (\S+)", (Path(__file__).parents[1] / "vm/group_vars/all.yml").read_text(), re.M).group(1)
        base = f"https://raw.githubusercontent.com/projectcalico/calico/{ver}/manifests"
        with tempfile.TemporaryDirectory() as d:
            for f in ("operator-crds.yaml", "tigera-operator.yaml"):
                self.ok(f"curl -fsSL -o {d}/{f} {base}/{f}", timeout=120)
            crds = Path(d, "operator-crds.yaml").read_text()
            self.assertGreater(len(crds), 1_000_000)                                        # the CRDs live here
            self.assertNotIn("kind: CustomResourceDefinition", Path(d, "tigera-operator.yaml").read_text())
            too_long = sh(wrong("CLU-023", "s2", 1) + " --dry-run=server", cwd=d, timeout=200)   # trap
            self.assertFalse(too_long)
            self.assertIn("metadata.annotations: Too long", too_long.out)
            self.ok(answer("CLU-023", "s2").replace("kubectl create", "kubectl apply --server-side --force-conflicts")
                    + " --dry-run=server", cwd=d, timeout=200)                               # alias: server-side
            self.ok("kubectl apply -f tigera-operator.yaml --dry-run=server", cwd=d)          # s6 alias: small file
            mism = sh(wrong("CLU-023", "s6", 1) + " --dry-run=server", cwd=d)                 # trap: -n kube-system
            self.assertIn('does not match the namespace "kube-system"', mism.out)

    def test_clu023_calico_lives_in_calico_system(self):
        if VM:                                                                         # the lab runs Calico
            self.assertIn("calico-node", self.answer_ok("CLU-023", "s5").out)
            self.assertNotIn("calico-node", sh(wrong("CLU-023", "s5", 1)).out)          # trap: not kube-system
        self.assertIn("No resources found", sh(wrong("CLU-023", "s5", 3)).out)          # trap: no 'calico' ns

    def test_clu026_kubeconfig_server_line(self):
        server = self.answer_ok("CLU-026", "s5").out.strip("'")
        self.assertRegex(server, r"^https://\S+:\d+$")                                # kind maps 6443 to a random host port
        self.assertRegex(sh(wrong("CLU-026", "s5", 1)).out, r"\d+\.\d+\.\d+\.\d+:6443|6443")  # trap: the members

    def test_clu027_admin_conf_versus_kubelet_conf(self):
        ok = node(answer("CLU-027", "s5") + " && kubectl get deployments -A")             # s5: admin identity
        self.assertTrue(ok, ok)
        self.assertIn("coredns", ok.out)
        nodeid = node(wrong("CLU-027", "s5", 1) + " && kubectl get deployments -A")      # trap: the node identity
        self.assertFalse(nodeid, nodeid)
        self.assertIn("forbidden", nodeid.out.lower())
        self.assertIn("system:node:", nodeid.out)

    def test_wrk018_metrics_first(self):
        self.cleanup_cmd("kubectl delete deploy worker -n jobs")
        self.ok("kubectl create deployment worker --image=nginx:1.27 -n jobs")
        self.ok("kubectl rollout status deployment worker -n jobs --timeout=120s")
        res = wait_for(answer("WRK-018", "s0"), "worker-", 120)                           # s0: metrics served
        self.assertRegex(res.out, r"worker-\S+\s+\d+m\s+\d+Mi")
        self.assertFalse(sh(wrong("WRK-018", "s0", 1)))                                   # trap: top deployment

    def test_sto007_create_the_claim_from_its_file(self):
        self.cleanup_cmd("kubectl delete pvc archive-v2 -n archive")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pvc.yaml").write_text(final_file("STO-007", "s2"))
            self.ok(wrong("STO-007", "s4", 3), cwd=d)                                     # trap: dry run, nothing
            self.assertFalse(sh("kubectl get pvc archive-v2 -n archive"))
            self.assertIn("does not match the namespace", self.fails(wrong("STO-007", "s4", 1), cwd=d).out)
            self.answer_ok("STO-007", "s4", cwd=d)                                         # s4
        self.assertIn("pv-archive", self.ok("kubectl get pvc archive-v2 -n archive -o jsonpath='{.spec.volumeName}'").out)

    def test_sto009_capacity_is_status_and_df_is_what_the_app_sees(self):
        # no expandable storage here (local-path cannot grow): check that the column read in s4 is
        # status.capacity, and that s5's df shows a mounted filesystem's size
        self.cleanup_cmd("kubectl delete pod reporter -n finance; kubectl delete pvc reports -n finance")
        self.apply("apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: reports, namespace: finance}\n"
                   "spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 1Gi}}}\n")
        self.apply("apiVersion: v1\nkind: Pod\nmetadata: {name: reporter, namespace: finance}\n"
                   "spec:\n  containers: [{name: app, image: busybox:1.36, command: [sleep, '3600'],\n"
                   "    volumeMounts: [{name: data, mountPath: /reports}]}]\n"
                   "  volumes: [{name: data, persistentVolumeClaim: {claimName: reports}}]\n")
        self.ok("kubectl wait --for=condition=Ready pod/reporter -n finance --timeout=180s", timeout=200)
        table = self.answer_ok("STO-009", "s4").out                                       # s4: CAPACITY column
        cap = sh("kubectl get pvc reports -n finance -o jsonpath='{.status.capacity.storage}'").out.strip("'")
        self.assertRegex(table, rf"reports\s+Bound\s+\S+\s+{re.escape(cap)}")
        self.assertFalse(sh(wrong("STO-009", "s4", 1)))                                    # trap: PV by claim name
        df = self.answer_ok("STO-009", "s5").out                                           # s5: the filesystem
        self.assertIn("/reports", df)
        self.assertRegex(df, r"Size|1K-blocks")

    def test_net016_canary_backend_apply_and_conditions(self):
        self.cleanup_cmd("kubectl delete httproute checkout -n shop; kubectl delete svc checkout-v2 -n shop")
        self.fails(answer("NET-016", "s0"))                                                 # s0: not there yet
        self.ok("kubectl create service clusterip checkout-v2 --tcp=8080:8080 -n shop")
        self.assertIn("8080/TCP", self.answer_ok("NET-016", "s0").out)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "checkout-route.yaml").write_text(final_file("NET-016", "s1"))
            self.answer_ok("NET-016", "s3", cwd=d)                                         # s3: apply
            self.assertIn("AlreadyExists", self.fails(wrong("NET-016", "s3", 1), cwd=d).out)
        weights = json.loads(self.ok("kubectl get httproute checkout -n shop -o jsonpath='{.spec.rules[0].backendRefs}'").out.strip("'"))
        self.assertEqual(sorted(b["weight"] for b in weights), [10, 90])
        self.assertIn("checkout", self.answer_ok("NET-016", "s4").out)                     # s4 (no controller here)

    def test_net017_rollout_status_waits_for_coredns(self):
        self.answer_ok("NET-017", "s4", timeout=200)
        self.assertIn("not found", sh(wrong("NET-017", "s4", 3)).out)                       # trap: no -n


class AdmissionPluginLive(ControlPlaneTest):
    def test_clu014_always_pull_images_rewrites_the_policy(self):
        line = final_file("CLU-014", "s2")
        flag = re.search(r"--enable-admission-plugins=\S+", line).group(0)
        self.assertTrue(self.edit_apiserver(lambda m: set_flags(m, [flag])), "the API server did not come back")
        self.cleanup_cmd("kubectl delete pod pull-test --wait=false")
        self.answer_ok("CLU-014", "s4")                                                     # s4: asks IfNotPresent
        stored = self.answer_ok("CLU-014", "s5").out                                        # s5: stored Always
        self.assertIn("Always", stored)
        self.assertNotIn("IfNotPresent", stored)
        self.assertEqual(sh(wrong("CLU-014", "s5", 1)).out.strip(), "")                     # trap: describe shows none
        self.assertFalse(sh(wrong("CLU-014", "s4", 2)))                                     # trap: no 'create pod'
