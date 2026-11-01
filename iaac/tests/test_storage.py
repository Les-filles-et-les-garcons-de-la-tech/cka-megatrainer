"""Storage (STO-*): the correct answers, run for real."""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from lib import ClusterTest, answer, final_file, prefill, sh, wait_for

PV = """apiVersion: v1
kind: PersistentVolume
metadata: {{name: {name}}}
spec:
  capacity: {{storage: {size}}}
  accessModes: [ReadWriteOnce]
  storageClassName: manual
  persistentVolumeReclaimPolicy: {policy}
  hostPath: {{path: /tmp/{name}}}
"""


def pv(name, size="1Gi", policy="Retain"):
    return PV.format(name=name, size=size, policy=policy)


def phase(kind, name, ns=""):
    flag = f"-n {ns}" if ns else ""
    return sh(f"kubectl get {kind} {name} {flag} -o jsonpath='{{.status.phase}}'").out.strip("'")


def wait_phase(kind, name, want, ns="", timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if phase(kind, name, ns) == want:
            return True
        time.sleep(2)
    return False


class Binding(ClusterTest):
    def test_sto001_pending_claim_fixed_by_matching_size_and_mode(self):
        self.cleanup_cmd("kubectl delete pvc data-pvc; kubectl delete pv data-pv")
        self.apply(pv("data-pv", "10Gi"))
        self.apply(prefill("STO-001", "s2"))                      # asks for more than the PV offers
        time.sleep(5)
        self.assertEqual(phase("pvc", "data-pvc"), "Pending")
        self.assertIn("Events", self.answer_ok("STO-001", "s1").out)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pvc.yaml").write_text(final_file("STO-001", "s2"))
            self.answer_ok("STO-001", "s3", cwd=d)
        self.assertTrue(wait_phase("pvc", "data-pvc", "Bound"))

    def test_sto004_hostpath_pv_is_available(self):
        self.cleanup_cmd("kubectl delete pv pv-log")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pv.yaml").write_text(final_file("STO-004", "s1"))
            self.answer_ok("STO-004", "s2", cwd=d)
        self.assertIn("Available", self.answer_ok("STO-004", "s3").out)

    def test_sto005_pod_mounts_a_claim(self):
        self.cleanup_cmd("kubectl delete pod web -n shop; kubectl delete pvc web-data -n shop")
        self.apply("apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: web-data, namespace: shop}\n"
                   "spec:\n  accessModes: [ReadWriteOnce]\n  resources: {requests: {storage: 1Gi}}\n")
        # explain: a Pending claim of a WaitForFirstConsumer class is normal, the Pod triggers the binding
        self.assertIn("Pending", self.answer_ok("STO-005", "s1").out)
        self.apply(final_file("STO-005", "s2"))
        self.assertTrue(wait_phase("pod", "web", "Running", "shop", 120))
        self.assertTrue(wait_phase("pvc", "web-data", "Bound", "shop"))
        self.assertIn("/usr/share/nginx/html", self.answer_ok("STO-005", "s3").out)

    def test_sto010_emptydir_shared_by_two_containers_and_lost_with_the_pod(self):
        self.cleanup_cmd("kubectl delete pod logger")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pod.yaml").write_text(final_file("STO-010", "s1"))
            self.apply(final_file("STO-010", "s1"))
            self.assertTrue(wait_phase("pod", "logger", "Running", "", 90))
            res = wait_for(answer("STO-010", "s2"), "20", 60)                  # a date line written by 'app'
            self.assertTrue(res and res.out, res)
            before = res.out.splitlines()[0]
            self.answer_ok("STO-010", "s3", cwd=d, timeout=150)                # delete + recreate
            self.assertTrue(wait_phase("pod", "logger", "Running", "", 90))
            after = wait_for(answer("STO-010", "s2"), "20", 60).out.splitlines()[0]
            self.assertNotEqual(before, after)                                 # the old content is gone


class Reclaim(ClusterTest):
    def bound_pair(self, name, claim, ns, policy, size="1Gi"):
        # delete every claim first: a Bound PV is protected by a finalizer and would hang
        self.cleanup_cmd(f"kubectl delete pvc --all -n {ns}; kubectl delete pv {name}")
        self.apply(pv(name, size, policy))
        self.apply(f"apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {{name: {claim}, namespace: {ns}}}\n"
                   f"spec:\n  storageClassName: manual\n  accessModes: [ReadWriteOnce]\n  volumeName: {name}\n"
                   f"  resources: {{requests: {{storage: {size}}}}}\n")
        self.assertTrue(wait_phase("pv", name, "Bound"))

    def test_sto006_retain_keeps_the_volume_when_the_claim_goes(self):
        self.bound_pair("pv-db", "db-data", "prod", "Delete")
        self.answer_ok("STO-006", "s1")                                   # mutable even while Bound
        self.assertEqual(self.answer_ok("STO-006", "s2").out.strip("'"), "Retain")
        self.answer_ok("STO-006", "s3")
        self.assertTrue(wait_phase("pv", "pv-db", "Released"))
        ref = self.ok("kubectl get pv pv-db -o jsonpath='{.spec.claimRef.name}'").out
        self.assertEqual(ref.strip("'"), "db-data")                       # explain: the old claimRef stays

    def test_sto007_released_volume_reused_by_a_new_claim(self):
        self.bound_pair("pv-archive", "old", "archive", "Retain", "5Gi")  # 5Gi, as in the scenario
        self.ok("kubectl delete pvc old -n archive")
        self.assertTrue(wait_phase("pv", "pv-archive", "Released"))
        # a matching claim cannot bind while the old claimRef is still there
        self.apply(final_file("STO-007", "s2"))
        time.sleep(8)
        self.assertEqual(phase("pvc", "archive-v2", "archive"), "Pending")
        self.ok("kubectl delete pvc archive-v2 -n archive")
        self.answer_ok("STO-007", "s1")
        self.assertTrue(wait_phase("pv", "pv-archive", "Available"))
        self.apply(final_file("STO-007", "s2"))
        res = wait_for(answer("STO-007", "s3"), "Bound", 60)
        self.assertIn("Bound", res.out, res)
        self.assertIn("pv-archive", res.out)


class Classes(ClusterTest):
    def test_sto002_wait_for_first_consumer_class_and_its_immutable_fields(self):
        self.cleanup_cmd("kubectl delete sc local-wait")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "sc.yaml").write_text(final_file("STO-002", "s1"))
            self.answer_ok("STO-002", "s2", cwd=d)
        row = [ln for ln in self.answer_ok("STO-002", "s3").out.splitlines() if "local-wait" in ln][0]
        for word in ("no-provisioner", "Retain", "WaitForFirstConsumer"):
            self.assertIn(word, row)
        # explain: allowVolumeExpansion can change later, the other fields cannot
        self.ok("kubectl patch sc local-wait -p '{\"allowVolumeExpansion\":true}'")
        self.fails("kubectl patch sc local-wait -p '{\"reclaimPolicy\":\"Delete\"}'")
        self.fails("kubectl patch sc local-wait -p '{\"volumeBindingMode\":\"Immediate\"}'")
        self.fails("kubectl patch sc local-wait -p '{\"provisioner\":\"example.com/x\"}'")

    def test_sto003_switch_the_default_class(self):
        sc = ("apiVersion: storage.k8s.io/v1\nkind: StorageClass\nmetadata:\n  name: {n}\n  annotations:\n"
              "    storageclass.kubernetes.io/is-default-class: \"{d}\"\nprovisioner: rancher.io/local-path\n"
              "volumeBindingMode: WaitForFirstConsumer\n")
        flip = "storageclass.kubernetes.io/is-default-class"
        self.cleanup_cmd("kubectl delete sc gp2 gp3; kubectl delete pvc retro two-defaults; "
                         f"kubectl annotate sc standard {flip}=true --overwrite")
        sh(f"kubectl annotate sc standard {flip}=false --overwrite")       # gp2 is now THE default
        self.apply(sc.format(n="gp2", d="true"))
        self.apply(sc.format(n="gp3", d="false"))
        self.assertIn("(default)", [ln for ln in self.answer_ok("STO-003", "s1").out.splitlines() if "gp2" in ln][0])
        self.answer_ok("STO-003", "s2")
        # a claim created while NO class is default...
        self.apply("apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: retro}\n"
                   "spec:\n  accessModes: [ReadWriteOnce]\n  resources: {requests: {storage: 1Gi}}\n")
        self.assertEqual(self.ok("kubectl get pvc retro -o jsonpath='{.spec.storageClassName}'").out.strip("'"), "")
        self.answer_ok("STO-003", "s3")
        row = [ln for ln in self.ok("kubectl get sc").out.splitlines() if "gp3" in ln][0]
        self.assertIn("(default)", row)
        # ...is given the new default afterwards (retroactive assignment, GA since 1.28)
        res = wait_for("kubectl get pvc retro -o jsonpath='{.spec.storageClassName}'", "gp3", 30)
        self.assertEqual(res.out.strip("'"), "gp3", "existing claims DO get the new default class")

    def test_sto003_two_defaults_the_newest_wins(self):
        sc = ("apiVersion: storage.k8s.io/v1\nkind: StorageClass\nmetadata:\n  name: {n}\n  annotations:\n"
              "    storageclass.kubernetes.io/is-default-class: \"true\"\nprovisioner: rancher.io/local-path\n"
              "volumeBindingMode: WaitForFirstConsumer\n")
        self.cleanup_cmd("kubectl delete sc gp2 gp3; kubectl delete pvc two-defaults")
        self.apply(sc.format(n="gp2"))
        time.sleep(2)
        self.apply(sc.format(n="gp3"))
        self.apply("apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: two-defaults}\n"
                   "spec:\n  accessModes: [ReadWriteOnce]\n  resources: {requests: {storage: 1Gi}}\n")
        got = self.ok("kubectl get pvc two-defaults -o jsonpath='{.spec.storageClassName}'").out.strip("'")
        self.assertEqual(got, "gp3")

    def test_sto008_claim_waits_for_its_pod_then_a_volume_appears(self):
        self.cleanup_cmd("kubectl delete pod cache-pod -n apps; kubectl delete pvc cache -n apps; "
                         "kubectl delete sc csi-hostpath")
        self.apply("apiVersion: storage.k8s.io/v1\nkind: StorageClass\nmetadata: {name: csi-hostpath}\n"
                   "provisioner: rancher.io/local-path\nvolumeBindingMode: WaitForFirstConsumer\n")
        self.apply("apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: {name: cache, namespace: apps}\n"
                   "spec:\n  storageClassName: csi-hostpath\n  accessModes: [ReadWriteOnce]\n"
                   "  resources: {requests: {storage: 1Gi}}\n")
        res = wait_for(answer("STO-008", "s1"), "waiting for first consumer", 30)
        self.assertIn("waiting for first consumer", res.out)
        self.assertEqual(self.ok("kubectl get pv --no-headers").out.count("apps/cache"), 0)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "cache-pod.yaml").write_text(
                "apiVersion: v1\nkind: Pod\nmetadata: {name: cache-pod, namespace: apps}\nspec:\n"
                "  containers: [{name: c, image: 'busybox:1.36', command: [sleep, '3600'], "
                "volumeMounts: [{name: v, mountPath: /data}]}]\n"
                "  volumes: [{name: v, persistentVolumeClaim: {claimName: cache}}]\n")
            self.answer_ok("STO-008", "s2", cwd=d)
        res = wait_for(answer("STO-008", "s3"), "apps/cache", 90)
        self.assertRegex(res.out, r"pvc-[0-9a-f-]+.*apps/cache")           # named pvc-<claim uid>

    def test_sto009_grow_a_claim(self):
        self.cleanup_cmd("kubectl delete pod grower -n finance; kubectl delete pvc reports -n finance; "
                         "kubectl patch sc standard -p '{\"allowVolumeExpansion\":false}'")
        # the exported file names a volume that exists only in the scenario: drop it
        self.apply(re.sub(r"\n\s*volumeName: \S+", "", prefill("STO-009", "s2")))
        self.apply("apiVersion: v1\nkind: Pod\nmetadata: {name: grower, namespace: finance}\nspec:\n"
                   "  containers: [{name: c, image: 'busybox:1.36', command: [sleep, '3600'], "
                   "volumeMounts: [{name: v, mountPath: /data}]}]\n"
                   "  volumes: [{name: v, persistentVolumeClaim: {claimName: reports}}]\n")
        self.assertTrue(wait_phase("pvc", "reports", "Bound", "finance", 120))
        # without allowVolumeExpansion the API server refuses to grow the claim
        self.fails("kubectl patch pvc reports -n finance -p "
                   "'{\"spec\":{\"resources\":{\"requests\":{\"storage\":\"5Gi\"}}}}'", "must support resize")
        self.answer_ok("STO-009", "s1")
        real = json.loads(self.ok("kubectl get pvc reports -n finance -o json").out)["spec"]["volumeName"]
        self.apply(final_file("STO-009", "s2").replace("pvc-6f1c2a4e-91d3-4b7e-a0c5-2d8e7f3b9a10", real))
        self.assertEqual(self.ok("kubectl get pvc reports -n finance -o jsonpath='{.spec.resources.requests.storage}'")
                         .out.strip("'"), "5Gi")
        self.answer_ok("STO-009", "s3")
        # a claim can grow, never shrink
        self.fails("kubectl patch pvc reports -n finance -p "
                   "'{\"spec\":{\"resources\":{\"requests\":{\"storage\":\"1Gi\"}}}}'", "less than")


if __name__ == "__main__":
    unittest.main()
