"""Workloads & Scheduling (WRK-*): the correct answers, run for real."""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from lib import WORKER, ClusterTest, answer, final_file, node, sh, wait_for


def pods(selector, ns):
    return sh(f"kubectl get pods -n {ns} -l {selector} -o name").out.split()


def live_pods(selector, ns):
    """Pods that count against maxSurge: those being deleted (Terminating) do not."""
    items = json.loads(sh(f"kubectl get pods -n {ns} -l {selector} -o json").out or "{}").get("items", [])
    return [p for p in items if not p["metadata"].get("deletionTimestamp")]


class Deployments(ClusterTest):
    def test_wrk001_limits_from_a_dry_run_manifest(self):
        self.cleanup_cmd("kubectl delete deploy web")
        with tempfile.TemporaryDirectory() as d:
            self.answer_ok("WRK-001", "s1", cwd=d)
            self.assertIn("kind: Deployment", Path(d, "deploy.yaml").read_text())
            self.assertEqual(sh("kubectl get deploy web -o name").rc, 1)  # --dry-run created nothing
            Path(d, "deploy.yaml").write_text(final_file("WRK-001", "s2"))
            self.answer_ok("WRK-001", "s3", cwd=d)
        got = self.ok("kubectl get deploy web -o jsonpath='{.spec.template.spec.containers[0].resources.limits}'").out
        self.assertIn("500m", got)
        self.assertIn("128Mi", got)

    def test_wrk003_rollout_then_roll_back_to_a_chosen_revision(self):
        self.apply("""apiVersion: apps/v1
kind: Deployment
metadata: {name: api, namespace: shop}
spec:
  replicas: 2
  selector: {matchLabels: {app: api}}
  template:
    metadata: {labels: {app: api}}
    spec:
      containers: [{name: server, image: "nginx:1.25"}]
""")
        self.ok("kubectl set image deployment/api server=nginx:1.26 -n shop")      # revision 2 = validated
        self.ok("kubectl rollout status deployment/api -n shop --timeout=120s")
        self.answer_ok("WRK-003", "s1")                                            # revision 3
        self.assertIn("successfully rolled out", self.answer_ok("WRK-003", "s2", timeout=150).out)
        self.assertIn("3", self.answer_ok("WRK-003", "s3").out)
        self.answer_ok("WRK-003", "s4")
        self.assertIn("nginx:1.26", self.ok("kubectl get deploy api -n shop -o wide").out)
        rows = [ln.split()[0] for ln in self.ok("kubectl rollout history deployment/api -n shop").out.splitlines()
                if ln[:1].isdigit()]
        self.assertEqual(rows, ["1", "3", "4"])  # explain: revision 2 comes back as the newest one

    def test_wrk004_zero_downtime_strategy(self):
        self.apply("""apiVersion: apps/v1
kind: Deployment
metadata: {name: checkout, namespace: shop}
spec:
  replicas: 4
  selector: {matchLabels: {app: checkout}}
  template:
    metadata: {labels: {app: checkout}}
    spec:
      containers: [{name: web, image: "nginx:1.25"}]
""")
        self.ok("kubectl rollout status deployment/checkout -n shop --timeout=120s")
        with tempfile.TemporaryDirectory() as d:
            self.answer_ok("WRK-004", "s1", cwd=d)
            Path(d, "checkout.yaml").write_text(final_file("WRK-004", "s2"))
            self.answer_ok("WRK-004", "s3", cwd=d)
        strategy = self.ok("kubectl get deploy checkout -n shop -o jsonpath='{.spec.strategy.rollingUpdate}'").out
        self.assertIn('"maxSurge":1', strategy)
        self.assertIn('"maxUnavailable":0', strategy)
        self.ok("kubectl set image deploy/checkout '*=nginx:1.26' -n shop")       # the colleague's update
        lowest, highest, end = 99, 0, time.time() + 60
        while time.time() < end:
            ready = sh("kubectl get deploy checkout -n shop -o jsonpath='{.status.availableReplicas}'").out or "0"
            total = len(live_pods("app=checkout", "shop"))
            lowest, highest = min(lowest, int(ready.strip("'") or 0)), max(highest, total)
            if "successfully" in sh("kubectl rollout status deploy/checkout -n shop --timeout=1s").out:
                break
        self.assertGreaterEqual(lowest, 4, "fewer than 4 Pods were available during the update")
        self.assertLessEqual(highest, 5, "more than 5 Pods existed during the update")


class Scheduling(ClusterTest):
    def test_wrk002_taint_toleration_and_removal(self):
        self.cleanup_cmd(f"kubectl taint nodes {WORKER} dedicated=gpu:NoSchedule-")
        fix = lambda sid: answer("WRK-002", sid).replace("node01", WORKER)  # noqa: E731
        self.ok(fix("s1"), "tainted")
        self.assertIn("dedicated=gpu", self.ok(f"kubectl describe node {WORKER}").out)
        self.apply(final_file("WRK-002", "s2").replace("name: gpu-pod", "name: gpu-pod"))  # tolerating Pod is valid
        self.ok(fix("s3"), "untainted")
        self.assertNotIn("dedicated", self.ok(f"kubectl describe node {WORKER} | grep -i taints").out)

    def test_wrk009_label_and_node_selector(self):
        self.cleanup_cmd(f"kubectl delete pod cache; kubectl label nodes {WORKER} disktype-")
        self.ok(answer("WRK-009", "s1").replace("node02", WORKER))
        self.apply(final_file("WRK-009", "s2"))
        self.ok("kubectl wait --for=condition=Ready pod/cache --timeout=90s")
        self.assertIn(WORKER, self.answer_ok("WRK-009", "s3").out)
        # explain: no matching node = Pending with the scheduler's message
        sh("kubectl delete pod cache --wait=true")
        sh(f"kubectl label nodes {WORKER} disktype-")
        self.apply(final_file("WRK-009", "s2"))
        res = wait_for("kubectl describe pod cache", "didn't match Pod's node affinity/selector", 60)
        self.assertIn("didn't match Pod's node affinity/selector", res.out)

    def test_wrk010_required_node_affinity_waits_for_the_right_label(self):
        self.cleanup_cmd(f"kubectl delete pod analytics -n data; kubectl label nodes {WORKER} topology.kubernetes.io/zone-")
        self.ok(f"kubectl label nodes {WORKER} topology.kubernetes.io/zone=eu-west-1c --overwrite")
        self.assertIn("eu-west-1c", self.answer_ok("WRK-010", "s1").out)
        self.apply(final_file("WRK-010", "s2"))
        wait_for("kubectl get pod analytics -n data", "Pending", 30)
        self.assertIn("Pending", self.ok("kubectl get pod analytics -n data").out)  # hard requirement: not placed
        self.ok(answer("WRK-010", "s3").replace("node02", WORKER))
        res = wait_for("kubectl get pod analytics -n data -o wide", WORKER, 90)
        self.assertIn(WORKER, res.out)

    def test_wrk011_priority_class_and_deployment(self):
        self.cleanup_cmd("kubectl delete priorityclass high-priority")
        self.answer_ok("WRK-011", "s1")
        # explain: user classes top out at 1 billion
        self.fails("kubectl create priorityclass too-high --value=2000000000")
        self.apply(final_file("WRK-011", "s2"))
        res = wait_for(answer("WRK-011", "s3"), "1000000", 60)
        self.assertRegex(res.out, r"payments\S*\s+1000000")

    def test_wrk014_static_pod_on_a_node(self):
        manifest = ("apiVersion: v1\nkind: Pod\nmetadata: {name: static-web}\nspec:\n"
                    "  containers: [{name: web, image: nginx:1.26}]\n")
        self.cleanup_cmd(f"podman exec {WORKER} rm -f /etc/kubernetes/manifests/static-web.yaml")
        node("cat > /tmp/static-web.yaml", which=WORKER, stdin=manifest)
        self.assertIn("staticPodPath: /etc/kubernetes/manifests", node(answer("WRK-014", "s1"), which=WORKER).out)
        self.assertTrue(node(answer("WRK-014", "s2"), which=WORKER))
        res = wait_for("kubectl get pods", f"static-web-{WORKER}", 90)  # the mirror Pod
        self.assertIn(f"static-web-{WORKER}", res.out)
        # explain: deleting the mirror Pod with kubectl does not remove it: the file is the source of truth
        sh(f"kubectl delete pod static-web-{WORKER} --wait=false")
        time.sleep(8)
        self.assertIn(f"static-web-{WORKER}", sh("kubectl get pods").out)
        self.assertTrue(node(answer("WRK-014", "s3"), which=WORKER))
        for _ in range(30):
            if f"static-web-{WORKER}" not in sh("kubectl get pods").out:
                return
            time.sleep(2)
        self.fail("the static Pod did not go away after removing its file")


class Configuration(ClusterTest):
    def test_wrk005_configmap_as_environment(self):
        self.cleanup_cmd("kubectl delete pod web -n dev; kubectl delete cm app-config -n dev")
        self.answer_ok("WRK-005", "s1")
        self.apply(final_file("WRK-005", "s2"))
        self.ok("kubectl wait --for=condition=Ready pod/web -n dev --timeout=90s")
        self.assertEqual(self.answer_ok("WRK-005", "s3").out, "production")

    def test_wrk006_secret_mounted_and_readable_in_clear(self):
        self.cleanup_cmd("kubectl delete pod db-client -n prod; kubectl delete secret db-cred -n prod")
        self.answer_ok("WRK-006", "s1")
        self.apply(final_file("WRK-006", "s2"))
        self.ok("kubectl wait --for=condition=Ready pod/db-client -n prod --timeout=90s")
        self.assertEqual(self.ok("kubectl exec db-client -n prod -- cat /etc/db/username").out, "admin")
        self.assertEqual(self.answer_ok("WRK-006", "s3").out, "s3cret")

    def test_wrk007_autoscaler_needs_a_cpu_request(self):
        self.cleanup_cmd("kubectl delete hpa,deploy frontend -n web")
        self.ok("kubectl create deploy frontend --image=nginx:1.26 -n web")
        res = self.answer_ok("WRK-007", "s1")
        self.ok("kubectl get hpa frontend -n web")
        self.ok("kubectl delete hpa frontend -n web")
        self.ok("kubectl autoscale deployment frontend --min=2 --max=8 --cpu=60% -n web")  # the newer flag, too
        # explain: with no request there is nothing to divide by
        res = wait_for("kubectl get hpa frontend -n web", "<unknown>", 90)
        self.assertIn("<unknown>", res.out, res)
        self.answer_ok("WRK-007", "s2")
        self.answer_ok("WRK-007", "s3")
        for _ in range(75):  # "<unknown>/60%" also contains "/60%": wait until it is gone
            res = sh("kubectl get hpa frontend -n web")
            if "<unknown>" not in res.out:
                break
            time.sleep(2)
        self.assertNotIn("<unknown>", res.out, res)

    def test_wrk008_readiness_probe_failure_is_visible_in_describe(self):
        self.cleanup_cmd("kubectl delete deploy api -n shop")
        self.assertIn("httpGet", self.answer_ok("WRK-008", "s1").out)
        # the game's image is fictional: use a real one that does not listen on 8080
        self.apply(final_file("WRK-008", "s2").replace("registry.local/shop/api:2.1", "nginx:1.26"))
        res = wait_for("kubectl get pods -n shop -l app=api -o name", "pod/", 60)
        name = re.search(r"pod/(\S+)", res.out).group(1)
        res = wait_for(f"kubectl describe pod {name} -n shop", "Readiness probe failed", 90)
        self.assertIn("Readiness probe failed", res.out)
        cmd = re.sub(r"api-[a-z0-9]+-[a-z0-9]+", name, answer("WRK-008", "s3"))
        self.assertIn("0/1", sh(f"kubectl get pod {name} -n shop").out)
        self.ok(cmd)

    def test_wrk012_init_container_waits_for_a_service(self):
        self.cleanup_cmd("kubectl delete pod web -n shop; kubectl delete svc db -n shop")
        self.apply(final_file("WRK-012", "s1"))
        wait_for("kubectl get pod web -n shop", "Init:0/1", 60)
        self.assertIn("Init:0/1", self.ok("kubectl get pod web -n shop").out)
        res = wait_for(answer("WRK-012", "s2"), "NXDOMAIN", 90)
        self.assertIn("db.shop.svc.cluster.local", res.out)  # explain quotes this message
        self.ok("kubectl create service clusterip db --tcp=5432:5432 -n shop")
        res = wait_for("kubectl get pod web -n shop", "Running", 120)
        self.assertIn("Running", res.out, res)

    def test_wrk013_quota_and_limitrange(self):
        self.answer_ok("WRK-013", "s1")
        self.cleanup_cmd("kubectl delete quota,limitrange,deploy --all -n team-a")
        # explain: with a quota on requests, a Pod that declares none is rejected
        res = sh("kubectl run bare --image=nginx:1.26 -n team-a")
        self.assertFalse(res, "a Pod without requests must be rejected under the quota")
        self.assertIn("must specify", res.out)
        self.apply(final_file("WRK-013", "s2"))
        self.ok("kubectl run bare --image=nginx:1.26 -n team-a")                  # the default is injected now
        self.assertIn("100m", self.ok("kubectl get pod bare -n team-a -o jsonpath='{.spec.containers[0].resources.requests}'").out)
        self.ok("kubectl delete pod bare -n team-a")
        self.ok("kubectl create deploy web --image=nginx:1.26 --replicas=12 -n team-a")
        res = wait_for(answer("WRK-013", "s3"), "pods", 60)
        self.assertRegex(res.out, r"pods\s+\d+\s+10")
        res = wait_for("kubectl describe rs -n team-a", "exceeded quota", 60)
        self.assertIn("exceeded quota: team-a-quota", res.out)


class Jobs(ClusterTest):
    def test_wrk015_cronjob_from_a_dry_run_and_a_manual_job(self):
        self.cleanup_cmd("kubectl delete cronjob backup -n ops; kubectl delete job backup-manual -n ops")
        with tempfile.TemporaryDirectory() as d:
            self.answer_ok("WRK-015", "s1", cwd=d)
            Path(d, "cj.yaml").write_text(final_file("WRK-015", "s2"))
            self.answer_ok("WRK-015", "s3", cwd=d)
        spec = self.ok("kubectl get cronjob backup -n ops -o jsonpath='{.spec.schedule} {.spec.successfulJobsHistoryLimit} "
                       "{.spec.jobTemplate.spec.backoffLimit}'").out
        self.assertEqual(spec.strip("'"), "0 2 * * * 3 2")
        self.answer_ok("WRK-015", "s4")
        res = wait_for("kubectl get job backup-manual -n ops", "1/1", 90)
        self.assertIn("1/1", res.out, res)


if __name__ == "__main__":
    unittest.main()
