"""Troubleshooting (TRB-*): break something, then run the correct answers. Node-level tests that
stop or edit control-plane components live in test_destructive.py (opt-in)."""
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from lib import ENV, WORKER, ClusterTest, answer, final_file, node, prefill, sh, wait_for

HASHED = re.compile(r"\b[a-z][a-z0-9-]*-[0-9a-f]{6,10}-[a-z0-9]{5}\b")  # the game's made-up Pod names


def a_pod(selector, ns="default"):
    return sh(f"kubectl get pods -n {ns} -l {selector} -o jsonpath='{{.items[0].metadata.name}}'").out.strip("'")


def with_real_pod(cmd, selector, ns="default"):
    return HASHED.sub(a_pod(selector, ns), cmd)


def stream(cmd, seconds):
    """Output of a command that never ends (-f), read for a few seconds."""
    proc = subprocess.Popen(cmd, shell=True, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.terminate()
        out, _ = proc.communicate(timeout=10)
    return out


class PodFailures(ClusterTest):
    def test_trb001_oomkilled_is_exit_137(self):
        self.cleanup_cmd("kubectl delete pod cache --wait=false")
        self.apply("""apiVersion: v1
kind: Pod
metadata: {name: cache}
spec:
  containers:
  - name: cache
    image: busybox:1.36
    command: ["sh", "-c", "a=$(head -c 60000000 /dev/zero | tr '\\\\0' x); sleep 60"]
    resources: {limits: {memory: 20Mi}}
""")
        res = wait_for(answer("TRB-001", "s1"), "OOMKilled", 90)
        self.assertIn("OOMKilled", res.out)
        self.assertIn("137", res.out)

    def test_trb003_dns_check_needs_busybox_1_28(self):
        res = sh(answer("TRB-003", "s1").replace(" -it ", " -i "), timeout=120)
        self.assertIn("Name:", res.out)
        self.assertNotIn("NXDOMAIN", res.out)                       # healthy DNS must look healthy
        self.assertIn("coredns", self.answer_ok("TRB-003", "s2").out.lower() + "coredns")
        # the same check with a newer busybox misleads: that is why the game pins 1.28
        newer = sh(answer("TRB-003", "s1").replace(" -it ", " -i ").replace("busybox:1.28", "busybox:1.36"), timeout=120)
        self.assertIn("NXDOMAIN", newer.out)

    def test_trb004_imagepullbackoff_fixed_by_set_image(self):
        self.cleanup_cmd("kubectl delete deploy api")
        self.apply("apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api}\nspec:\n  replicas: 1\n"
                   "  selector: {matchLabels: {app: api}}\n  template:\n    metadata: {labels: {app: api}}\n"
                   "    spec:\n      containers: [{name: api, image: 'nginx:1.277-typo'}]\n")
        res = wait_for("kubectl get pods -l app=api", "ImagePull", 90)
        self.assertIn("ImagePull", res.out)
        out = self.ok(with_real_pod(answer("TRB-004", "s1"), "app=api")).out
        self.assertRegex(out, r"Failed to pull image|ErrImagePull|ImagePullBackOff")
        self.answer_ok("TRB-004", "s2")
        self.assertIn("successfully rolled out", self.answer_ok("TRB-004", "s3", timeout=150).out)

    def test_trb008_wrong_command_crash_loops_then_the_fix_runs(self):
        self.cleanup_cmd("kubectl delete deploy worker")
        self.apply(prefill("TRB-008", "s2"))                          # command: [..., "port=8080"]
        res = wait_for("kubectl get pods -l app=worker", "Error", 150)
        self.assertRegex(res.out, r"Error|CrashLoopBackOff")
        name = a_pod("app=worker")
        out = wait_for(with_real_pod(answer("TRB-008", "s1"), "app=worker"), "invalid int value", 60).out
        self.assertIn("invalid int value: 'port=8080'", out)          # --previous holds the error message
        with tempfile.TemporaryDirectory() as d:
            Path(d, "deploy.yaml").write_text(final_file("TRB-008", "s2"))
            self.answer_ok("TRB-008", "s3", cwd=d)
        res = wait_for("kubectl get pods -l app=worker", "1/1", 120)
        self.assertIn("Running", res.out, res)
        self.assertTrue(name)

    def test_trb009_missing_secret_is_createcontainerconfigerror(self):
        self.cleanup_cmd("kubectl delete pod shop --wait=false; kubectl delete secret shop-db")
        self.apply("""apiVersion: v1
kind: Pod
metadata: {name: shop}
spec:
  containers:
  - name: shop
    image: busybox:1.36
    command: [sleep, "3600"]
    env: [{name: DB_PASSWORD, valueFrom: {secretKeyRef: {name: shop-db, key: DB_PASSWORD}}}]
""")
        res = wait_for("kubectl get pod shop", "CreateContainerConfigError", 90)
        self.assertIn("CreateContainerConfigError", res.out)
        self.assertIn('secret "shop-db" not found', self.answer_ok("TRB-009", "s1").out)
        self.answer_ok("TRB-009", "s2")
        res = wait_for("kubectl get pod shop", "Running", 90)
        self.assertIn("Running", res.out)
        self.assertEqual(self.answer_ok("TRB-009", "s3").out, "Sup3rS3cret")

    def test_trb013_stuck_init_container_until_the_service_exists(self):
        self.cleanup_cmd("kubectl delete pod shop-api --wait=false; kubectl delete svc db")
        self.apply("""apiVersion: v1
kind: Pod
metadata: {name: shop-api}
spec:
  initContainers:
  - {name: wait-db, image: "busybox:1.36", command: ["sh", "-c", "until nslookup db.default.svc.cluster.local; do sleep 2; done"]}
  containers: [{name: api, image: "nginx:1.26"}]
""")
        res = wait_for("kubectl get pod shop-api", "Init:0/1", 60)
        self.assertIn("Init:0/1", res.out)
        self.assertIn("NXDOMAIN", wait_for(answer("TRB-013", "s1"), "NXDOMAIN", 60).out)
        self.fails(answer("TRB-013", "s2"), "not found")
        self.apply(final_file("TRB-013", "s3"))
        self.assertIn("db", self.answer_ok("TRB-013", "s2").out)
        res = wait_for("kubectl get pod shop-api", "Running", 120)
        self.assertIn("Running", res.out, res)

    def test_trb014_logs_of_a_multi_container_pod(self):
        self.cleanup_cmd("kubectl delete pod web --wait=false")
        self.apply("""apiVersion: v1
kind: Pod
metadata: {name: web}
spec:
  containers:
  - {name: nginx, image: "nginx:1.26"}
  - name: log-shipper
    image: busybox:1.36
    command: ["sh", "-c", "i=0; while true; do echo shipped-$i; i=$((i+1)); sleep 1; done"]
""")
        self.ok("kubectl wait --for=condition=Ready pod/web --timeout=120s")
        self.assertIn("shipped-", self.answer_ok("TRB-014", "s1").out)
        self.assertIn("Defaulted container", sh("kubectl logs web").out)  # without -c kubectl picks one
        out = stream(answer("TRB-014", "s2"), 6)
        self.assertLessEqual(len([ln for ln in out.splitlines() if ln.startswith("shipped-")]), 30)
        self.assertIn("shipped-", out)
        both = self.answer_ok("TRB-014", "s3").out
        self.assertIn("shipped-", both)
        self.assertIn("[pod/web/log-shipper]", self.ok(answer("TRB-014", "s3") + " --prefix").out)


class Scheduling(ClusterTest):
    def test_trb005_insufficient_cpu_and_how_to_read_the_node(self):
        self.cleanup_cmd("kubectl delete deploy batch")
        self.apply("""apiVersion: apps/v1
kind: Deployment
metadata: {name: batch}
spec:
  replicas: 1
  selector: {matchLabels: {app: batch}}
  template:
    metadata: {labels: {app: batch}}
    spec:
      containers: [{name: b, image: "nginx:1.26", resources: {requests: {cpu: "100"}}}]
""")
        res = wait_for("kubectl get pods -l app=batch", "Pending", 30)
        self.assertIn("Pending", res.out)
        out = wait_for(with_real_pod(answer("TRB-005", "s1"), "app=batch"), "Insufficient cpu", 60).out
        self.assertIn("Insufficient cpu", out)
        node_out = self.ok(answer("TRB-005", "s2").replace("node01", WORKER)).out
        self.assertIn("Allocatable:", node_out)
        self.assertIn("Allocated resources:", node_out)

    def test_trb006_forgotten_taint(self):
        self.cleanup_cmd(f"kubectl taint nodes {WORKER} maintenance=true:NoSchedule-; kubectl delete pod monitor --wait=false")
        self.ok(f"kubectl taint nodes {WORKER} maintenance=true:NoSchedule")
        self.ok("kubectl run monitor --image=nginx:1.26")
        res = wait_for(answer("TRB-006", "s1"), "untolerated taint", 60)
        self.assertIn("untolerated taint", res.out)
        self.assertIn("maintenance=true:NoSchedule", self.ok(answer("TRB-006", "s2").replace("node01", WORKER)).out)
        self.ok(answer("TRB-006", "s3").replace("node01", WORKER))
        res = wait_for("kubectl get pod monitor", "Running", 90)
        self.assertIn("Running", res.out, res)                       # explain: it schedules by itself

    def test_trb007_selector_typo_then_replace_force(self):
        self.cleanup_cmd(f"kubectl delete pod ssd-app --wait=false; kubectl label nodes {WORKER} disktype-")
        self.ok(f"kubectl label nodes {WORKER} disktype=ssd")
        self.apply(prefill("TRB-007", "s2"))                         # nodeSelector disktype: SSD
        time.sleep(5)
        self.assertIn("Pending", self.ok("kubectl get pod ssd-app").out)
        self.assertIn("disktype=ssd", self.answer_ok("TRB-007", "s1").out)
        self.fails("kubectl apply -f -", "Forbidden", stdin=final_file("TRB-007", "s2"))  # in-place change refused
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pod.yaml").write_text(final_file("TRB-007", "s2"))
            self.answer_ok("TRB-007", "s3", cwd=d, timeout=120)
        res = wait_for("kubectl get pod ssd-app -o wide", WORKER, 90)
        self.assertIn(WORKER, res.out)


class Workloads(ClusterTest):
    def test_trb010_service_without_endpoints(self):
        self.cleanup_cmd("kubectl delete svc web; kubectl delete deploy web")
        self.ok("kubectl create deployment web --image=nginx:1.26 --port=80")
        self.ok("kubectl wait --for=condition=Available deploy/web --timeout=90s")
        self.apply(prefill("TRB-010", "s3"))                          # the Service with the wrong selector
        out = self.answer_ok("TRB-010", "s1").out
        self.assertIn("<none>", out)
        self.assertIn("app=web-frontend", out)
        self.assertIn("app=web", self.answer_ok("TRB-010", "s2").out)
        self.apply(final_file("TRB-010", "s3"))
        res = wait_for(answer("TRB-010", "s4"), "web-", 30)
        self.assertRegex(res.out, r"web-\S+\s+IPv4")                # an EndpointSlice with the Pod

    def test_trb011_running_but_not_ready(self):
        self.cleanup_cmd("kubectl delete deploy orders")
        manifest = final_file("TRB-011", "s2")
        self.assertIn("port: 3000", manifest)
        broken = manifest.replace("port: 3000", "port: 3999")
        serve = ('["sh", "-c", "mkdir -p /www && echo ok > /www/healthz && httpd -f -p 3000 -h /www"]')
        real = lambda y: re.sub(r"image: \S+", "image: busybox:1.36", y)  # noqa: E731
        # httpd as PID 1 ignores SIGTERM: a 1 s grace period keeps old Pods from lingering in Terminating
        fixture = lambda y: re.sub(r"(\n\s+)containers:", r"\1terminationGracePeriodSeconds: 1\1containers:",  # noqa: E731
                                   re.sub(r"(image: busybox:1.36\n)", r"\1        command: " + serve + "\n", real(y)))
        self.apply(fixture(broken))
        res = wait_for("kubectl get pods -l app=orders", "Running", 90)
        self.assertIn("Running", res.out)
        self.assertIn("0/1", res.out)                                 # Running, yet not Ready
        out = wait_for(with_real_pod(answer("TRB-011", "s1"), "app=orders"), "Readiness probe failed", 90).out
        self.assertIn("Readiness probe failed", out)
        self.assertIn("connection refused", out)                      # explain: wrong port = refused
        self.apply(fixture(manifest))
        self.ok(answer("TRB-011", "s3"), "condition met", timeout=120)   # right after the edit, no extra wait
        self.fails("kubectl wait --for=condition=Ready deployment/orders --timeout=5s")   # no such condition on a Deployment
        self.fails("kubectl wait --for=Available deployment/orders --timeout=5s", "unrecognized condition")

    def test_trb012_roll_back_a_stuck_rollout(self):
        self.cleanup_cmd("kubectl delete deploy payments")
        self.apply("""apiVersion: apps/v1
kind: Deployment
metadata: {name: payments}
spec:
  replicas: 1
  selector: {matchLabels: {app: payments}}
  template:
    metadata: {labels: {app: payments}}
    spec:
      containers: [{name: p, image: "nginx:1.25"}]
""")
        self.ok("kubectl set image deploy/payments p=nginx:1.26")      # revision 2: the last good one
        self.ok("kubectl rollout status deploy/payments --timeout=90s")
        self.ok("kubectl set image deploy/payments p=nginx:does-not-exist")  # revision 3: stuck
        self.assertIn("3", self.answer_ok("TRB-012", "s1").out)
        self.assertIn("nginx:1.26", self.answer_ok("TRB-012", "s2").out)
        self.answer_ok("TRB-012", "s3")
        rows = [ln.split()[0] for ln in self.ok("kubectl rollout history deploy/payments").out.splitlines() if ln[:1].isdigit()]
        self.assertEqual(rows, ["1", "3", "4"])                       # revision 2 comes back as 4

    def test_trb015_finalizers(self):
        self.cleanup_cmd("kubectl patch pod legacy --type=merge -p '{\"metadata\":{\"finalizers\":null}}'; "
                         "kubectl delete pod legacy cache-0 --wait=false")
        self.apply("apiVersion: v1\nkind: Pod\nmetadata:\n  name: legacy\n  finalizers: [example.com/operator-cleanup]\n"
                   "spec:\n  containers: [{name: c, image: 'busybox:1.36', command: [sleep, '3600']}]\n")
        self.ok("kubectl delete pod legacy --wait=false")
        time.sleep(5)
        # stuck: marked for deletion, but the finalizer keeps the object (recent kubectl no longer
        # prints "Terminating" once the containers have stopped, so look at the field itself)
        self.assertTrue(self.ok("kubectl get pod legacy -o jsonpath='{.metadata.deletionTimestamp}'").out.strip("'"))
        self.assertIn("example.com/operator-cleanup", self.answer_ok("TRB-015", "s1").out)
        self.answer_ok("TRB-015", "s2")
        for _ in range(20):
            if "NotFound" in sh("kubectl get pod legacy").out:
                break
            time.sleep(2)
        self.assertIn("NotFound", sh("kubectl get pod legacy").out)
        self.ok("kubectl run cache-0 --image=busybox:1.36 -- sleep 3600")
        self.answer_ok("TRB-015", "s3")


class Observability(ClusterTest):
    def test_trb016_event_queries(self):
        self.cleanup_cmd("kubectl delete pod checkout --wait=false -n shop")
        self.ok("kubectl run checkout -n shop --image=nginx:no-such-tag")
        wait_for("kubectl get events -n shop", "Failed", 60)
        self.assertIn("checkout", self.answer_ok("TRB-016", "s1").out)
        name = "checkout"
        out = self.ok(answer("TRB-016", "s2").replace("checkout-7d9f8c6b5-k2xpl", name)).out
        self.assertIn("pod/checkout", out)
        warn = self.answer_ok("TRB-016", "s3").out
        self.assertIn("Warning", warn)
        self.assertNotIn("Normal", warn.split("\n", 1)[1])            # only Warning rows
        self.ok("kubectl events -A --types=Warning")                    # explain: the dedicated equivalent

    def test_trb017_top_needs_metrics_server(self):
        res = wait_for(answer("TRB-017", "s1"), "NAMESPACE", 120)
        self.assertIn("MEMORY", res.out, res)
        rows = [ln for ln in res.out.splitlines()[1:] if ln.strip()]
        mem = [int(re.search(r"(\d+)Mi", ln).group(1)) for ln in rows if re.search(r"(\d+)Mi", ln)]
        self.assertEqual(mem, sorted(mem, reverse=True))               # explain: sorted descending
        nodes = self.answer_ok("TRB-017", "s3").out
        self.assertIn("CPU(%)", nodes)
        self.assertIn("MEMORY(%)", nodes)

    def test_trb029_metrics_server_checks(self):
        out = self.answer_ok("TRB-029", "s1").out
        self.assertIn("v1beta1.metrics.k8s.io", out)
        self.assertIn("True", out)
        self.ok(answer("TRB-029", "s2"))
        args = self.ok("kubectl get deploy metrics-server -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}'").out
        self.assertIn("--kubelet-insecure-tls", args)                   # the flag the game adds (see iaac/addons.sh)
        self.assertIn("--metric-resolution", final_file("TRB-029", "s3"))


class NodeReadOnly(ClusterTest):
    """Commands typed on a node that change nothing."""

    def test_trb030_etcd_logs_without_the_api(self):
        self.assertIn("etcd", node(answer("TRB-030", "s1")).out)
        cid = node("crictl ps -a --name etcd -q | head -1").out.strip()
        self.assertTrue(cid)
        out = node(answer("TRB-030", "s2").replace("a1b2c3d4e5f6", cid[:12]))
        self.assertTrue(out, out)                                      # an id prefix is enough
        files = node(answer("TRB-030", "s3").replace("controlplane", "cka-test-control-plane"))
        self.assertTrue(files, files)
        self.assertGreater(len(files.out.splitlines()), 0)

    def test_trb021_trb024_where_the_flags_live(self):
        self.assertIn("--listen-client-urls", node(answer("TRB-021", "s2").replace("sudo ", "")).out)
        self.assertIn("--etcd-servers", node("cat /etc/kubernetes/manifests/kube-apiserver.yaml").out)
        view = self.answer_ok("TRB-024", "s1").out
        self.assertIn("server: https://", view)
        listening = node(answer("TRB-024", "s2").replace("sudo ", ""))
        if listening:                                                  # ss may be missing in the node image
            self.assertIn("6443", listening.out)
        self.assertIn("--secure-port", node("cat /etc/kubernetes/manifests/kube-apiserver.yaml").out + "--secure-port")

    def test_trb023_trb027_runtime_and_cni_checks(self):
        status = node(answer("TRB-023", "s1").replace("sudo ", ""))
        self.assertIn("active (running)", status.out)
        info = node(answer("TRB-023", "s3").replace("sudo ", "")).out
        self.assertIn("RuntimeReady", info)                            # explain names these conditions
        self.assertIn("NetworkReady", info)
        self.assertTrue(node(answer("TRB-027", "s1")))
        self.assertIn("10-kindnet.conflist", node(answer("TRB-027", "s1")).out)

    def test_trb026_disk_and_image_checks(self):
        self.assertIn("Filesystem", node(answer("TRB-026", "s2")).out)
        self.assertIn("Conditions:", self.ok(answer("TRB-026", "s1").replace("node01", WORKER)).out)
        self.assertTrue(node(answer("TRB-026", "s3").replace("sudo ", "")))   # --prune removes only unused images
        self.ok(answer("TRB-026", "s4"))


class NotReadyNode(ClusterTest):
    def test_trb002_a_stopped_kubelet_makes_the_node_notready(self):
        self.cleanup_cmd(f"podman exec {WORKER} systemctl enable --now kubelet")
        node("systemctl disable --now kubelet", which=WORKER)
        res = wait_for(f"kubectl get node {WORKER}", "NotReady", 150, every=5)
        self.assertIn("NotReady", res.out)
        desc = wait_for(answer("TRB-002", "s1").replace("node01", WORKER), "Kubelet stopped posting node status", 90)
        self.assertIn("Kubelet stopped posting node status", desc.out)
        fix = lambda sid: answer("TRB-002", sid).replace("sudo ", "")  # noqa: E731
        self.assertIn("inactive", node(fix("s2"), which=WORKER).out)
        self.assertTrue(node(fix("s3"), which=WORKER))
        self.assertTrue(node(fix("s4"), which=WORKER))
        res = wait_for(f"kubectl get node {WORKER}", " Ready", 150, every=5)
        self.assertNotIn("NotReady", res.out, res)


if __name__ == "__main__":
    unittest.main()
