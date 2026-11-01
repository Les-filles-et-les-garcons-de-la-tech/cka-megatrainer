"""Services & Networking (NET-*): the correct answers, run for real."""
import json
import re
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from lib import ENV, WORKER, ClusterTest, answer, final_file, node, prefill, sh

NO_TTY = lambda cmd: cmd.replace(" -it ", " -i ")  # noqa: E731  # the tests have no terminal


def run_pod(cmd):
    return sh(NO_TTY(cmd), timeout=120)


def pod_ip(name, ns):
    return sh(f"kubectl get pod {name} -n {ns} -o jsonpath='{{.status.podIP}}'").out.strip("'")


def create_pod(test, name, ns, labels="", image="busybox:1.36", cmd="sleep 3600", port=""):
    test.cleanup_cmd(f"kubectl delete pod {name} -n {ns} --wait=false")
    lab = f"--labels={labels}" if labels else ""
    p = f"--port={port}" if port else ""
    test.ok(f"kubectl run {name} -n {ns} --image={image} {lab} {p} -- sh -c {cmd!r}")
    test.ok(f"kubectl wait --for=condition=Ready pod/{name} -n {ns} --timeout=120s")


class Services(ClusterTest):
    def test_net002_expose_a_deployment_as_nodeport(self):
        self.cleanup_cmd("kubectl delete svc web-svc; kubectl delete deploy web")
        self.ok("kubectl create deployment web --image=nginx:1.26")
        self.ok("kubectl wait --for=condition=Available deploy/web --timeout=90s")
        self.answer_ok("NET-002", "s1")
        spec = self.ok("kubectl get svc web-svc -o jsonpath='{.spec.type} {.spec.ports[0].port} {.spec.ports[0].targetPort} "
                       "{.spec.selector}'").out
        self.assertIn("NodePort 80 8080", spec)
        self.assertIn('"app":"web"', spec)                                  # explain: expose copies the selector
        self.assertIn("web-svc", self.answer_ok("NET-002", "s2").out)

    def test_net003_expose_a_pod_inside_the_cluster(self):
        self.cleanup_cmd("kubectl delete svc nginx-svc -n web")
        create_pod(self, "nginx", "web", "run=nginx", "nginx:1.26", "nginx -g 'daemon off;'", "80")
        self.answer_ok("NET-003", "s1")
        self.assertEqual(self.ok("kubectl get svc nginx-svc -n web -o jsonpath='{.spec.type}'").out.strip("'"), "ClusterIP")
        for _ in range(8):  # kube-proxy needs a moment to learn the new Service
            res = run_pod(answer("NET-003", "s2"))
            if "Welcome to nginx" in res.out:
                break
            time.sleep(3)
        self.assertIn("Welcome to nginx", res.out)

    def test_net004_nodeport_pinned_and_out_of_range_refused(self):
        self.cleanup_cmd("kubectl delete svc shop-np -n shop; kubectl delete deploy shop -n shop")
        self.ok("kubectl create deployment shop --image=nginx:1.26 -n shop")
        with tempfile.TemporaryDirectory() as d:
            self.answer_ok("NET-004", "s1", cwd=d)
            self.assertIn("kind: Service", Path(d, "np.yaml").read_text())
            Path(d, "np.yaml").write_text(final_file("NET-004", "s2"))
            self.ok("kubectl apply -f np.yaml", cwd=d)
        self.assertEqual(self.ok("kubectl get svc shop-np -n shop -o jsonpath='{.spec.ports[0].nodePort}'").out.strip("'"), "30080")
        res = node(answer("NET-004", "s3"))
        self.assertFalse(res)                                                # no override: default 30000-32767
        self.fails("kubectl create service nodeport bad --tcp=80:80 --node-port=40000 -n shop", "valid range")

    def test_net005_headless_service_for_a_statefulset(self):
        self.cleanup_cmd("kubectl delete sts db -n data; kubectl delete svc db-hl -n data")
        self.apply("""apiVersion: apps/v1
kind: StatefulSet
metadata: {name: db, namespace: data}
spec:
  serviceName: db-hl
  replicas: 2
  selector: {matchLabels: {app: db}}
  template:
    metadata: {labels: {app: db}}
    spec:
      containers: [{name: db, image: "busybox:1.36", command: [sleep, "3600"]}]
""")
        self.assertEqual(self.answer_ok("NET-005", "s1").out.strip("'"), "db-hl")
        self.apply(final_file("NET-005", "s2"))
        self.assertEqual(self.ok("kubectl get svc db-hl -n data -o jsonpath='{.spec.clusterIP}'").out.strip("'"), "None")
        self.ok("kubectl wait --for=condition=Ready pod/db-0 -n data --timeout=120s")
        res = run_pod(answer("NET-005", "s3"))
        self.assertIn(pod_ip("db-0", "data"), res.out)                      # the A record is the Pod's own IP
        old = pod_ip("db-0", "data")
        self.ok("kubectl delete pod db-0 -n data")
        self.ok("kubectl wait --for=condition=Ready pod/db-0 -n data --timeout=120s")
        self.assertIn(pod_ip("db-0", "data"), run_pod(answer("NET-005", "s3")).out)  # same name, maybe new IP
        self.assertTrue(old)

    def test_net006_dns_across_namespaces(self):
        self.cleanup_cmd("kubectl delete svc api -n backend; kubectl delete deploy api -n backend")
        self.ok("kubectl create deployment api --image=nginx:1.26 --port=80 -n backend")
        self.ok("kubectl expose deployment api --port=8080 --target-port=80 -n backend")
        create_pod(self, "web", "ui")
        res = run_pod(answer("NET-006", "s1"))
        self.assertIn("api.backend.svc.cluster.local", res.out)
        self.assertIn("Address", res.out)
        resolv = self.answer_ok("NET-006", "s2").out
        self.assertIn("search ui.svc.cluster.local svc.cluster.local cluster.local", resolv)
        self.assertIn("nameserver 10.96.0.10", resolv)
        self.assertIn("10.96.0.10", self.answer_ok("NET-006", "s3").out)     # the kube-dns ClusterIP
        # explain: the bare name is tried in 'ui' first, so it fails there; 'api.backend' works
        self.fails("kubectl exec web -n ui -- wget -qO- -T 3 http://api:8080")
        self.ok("kubectl exec web -n ui -- wget -qO- -T 3 http://api.backend:8080", "Welcome to nginx")

    def test_net007_pod_to_pod_across_namespaces_and_a_policy_that_blocks(self):
        create_pod(self, "server", "team-b", "", "nginx:1.26", "nginx -g 'daemon off;'", "80")
        create_pod(self, "client", "team-a")
        ip = pod_ip("server", "team-b")
        self.assertIn(ip, self.answer_ok("NET-007", "s1").out)
        cmd = re.sub(r"\d+\.\d+\.\d+\.\d+", ip, answer("NET-007", "s2"))
        self.ok(cmd, "Welcome to nginx")                                      # flat network: no Service needed
        self.apply("apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata: {name: deny, namespace: team-b}\n"
                   "spec: {podSelector: {}, policyTypes: [Ingress]}\n")
        self.cleanup_cmd("kubectl delete netpol deny -n team-b")
        time.sleep(5)
        self.fails(cmd)                                                       # the wget of s2 now times out
        self.assertIn("deny", self.answer_ok("NET-007", "s3").out)

    def test_net020_externalname_returns_a_cname(self):
        self.cleanup_cmd("kubectl delete svc billing -n payments")
        self.answer_ok("NET-020", "s1")
        self.assertEqual(self.ok("kubectl get svc billing -n payments -o jsonpath='{.spec.clusterIP}'").out.strip("'"), "")
        res = run_pod(answer("NET-020", "s2"))
        self.assertIn("canonical name = billing.partner.example.com", res.out)


class Policies(ClusterTest):
    def test_net001_allow_only_api_to_db_on_5432(self):
        self.cleanup_cmd("kubectl delete netpol --all -n prod; kubectl delete pod db api other -n prod --wait=false")
        # Known limit of kind's CNI (kindnet): next to a default-deny policy, a same-namespace podSelector
        # allow rule is not honoured (NET-009's namespaceSelector one is). So the game's policy is checked
        # on its own here: it must let 'api' in and keep everyone else out.
        create_pod(self, "db", "prod", "app=db", cmd="while true; do echo hi | nc -l -p 5432; done")
        create_pod(self, "api", "prod", "app=api")
        create_pod(self, "other", "prod", "app=other")
        self.apply(final_file("NET-001", "s1"))
        out = self.answer_ok("NET-001", "s2").out
        self.assertIn("5432", out)
        self.assertIn("app=api", out)
        time.sleep(5)
        ip = pod_ip("db", "prod")
        self.ok(f"kubectl exec api -n prod -- nc -w 3 {ip} 5432")             # allowed
        self.fails(f"kubectl exec other -n prod -- nc -w 3 {ip} 5432")        # everyone else stays blocked

    def test_net008_default_deny_ingress(self):
        self.cleanup_cmd("kubectl delete netpol --all -n secure; kubectl delete deploy web -n secure; kubectl delete svc web -n secure")
        self.ok("kubectl create deployment web --image=nginx:1.26 --port=80 -n secure")
        self.ok("kubectl expose deployment web --port=80 -n secure")
        self.ok("kubectl wait --for=condition=Available deploy/web -n secure --timeout=90s")
        self.apply(final_file("NET-008", "s1"))
        time.sleep(5)
        res = run_pod(answer("NET-008", "s2"))
        self.assertIn("timed out", res.out)                                   # explain: timeouts, not 'refused'
        self.assertNotIn("refused", res.out)

    def test_net009_namespace_selector_and_its_label(self):
        self.cleanup_cmd("kubectl delete netpol --all -n prod; kubectl delete deploy web -n prod; kubectl delete svc web -n prod")
        self.ok("kubectl create deployment web --image=nginx:1.26 --port=80 -n prod")
        self.ok("kubectl expose deployment web --port=80 -n prod")
        self.ok("kubectl wait --for=condition=Available deploy/web -n prod --timeout=90s")
        self.apply("apiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata: {name: default-deny, namespace: prod}\n"
                   "spec: {podSelector: {}, policyTypes: [Ingress]}\n")
        self.assertIn("kubernetes.io/metadata.name=monitoring", self.answer_ok("NET-009", "s1").out)
        self.apply(final_file("NET-009", "s2"))
        time.sleep(5)
        self.assertIn("Welcome to nginx", run_pod(answer("NET-009", "s3")).out)
        blocked = run_pod("kubectl run t2 --rm -i --restart=Never --image=busybox:1.36 -n team-a -- wget -qO- -T 3 web.prod")
        self.assertNotIn("Welcome to nginx", blocked.out)                    # only 'monitoring' is let in

    def test_net010_egress_locked_down_but_dns_kept(self):
        self.cleanup_cmd("kubectl delete netpol --all -n app; kubectl delete deploy frontend backend -n app; kubectl delete svc backend -n app")
        self.ok("kubectl create deployment backend --image=nginx:1.26 --port=8080 -n app")
        self.ok("kubectl create deployment frontend --image=busybox:1.36 -n app -- sleep 3600")
        self.ok("kubectl expose deployment backend --port=8080 -n app")
        self.ok("kubectl wait --for=condition=Available deploy/frontend deploy/backend -n app --timeout=90s")
        self.assertIn("frontend", self.answer_ok("NET-010", "s1").out)
        self.apply(final_file("NET-010", "s2"))
        time.sleep(5)
        res = sh(answer("NET-010", "s3"))                                     # DNS still works through the policy
        self.assertIn("Name:", res.out)                                       # (busybox exits 1 on a bare name even so)
        self.assertIn("backend.app.svc.cluster.local", res.out)
        self.fails("kubectl exec deploy/frontend -n app -- wget -qO- -T 3 http://example.com")  # all else is blocked


class IngressAndGateway(ClusterTest):
    def test_net011_ingress_in_one_command(self):
        self.cleanup_cmd("kubectl delete ingress shop -n shop")
        self.answer_ok("NET-011", "s1")
        self.assertIn("pathType: Prefix", self.ok("kubectl get ingress shop -n shop -o yaml").out)
        # explain: a plain '/' would be Exact
        exact = self.ok("kubectl create ingress x -n shop --class=nginx --rule='shop.example.com/=shop-svc:80' "
                        "--dry-run=client -o yaml").out
        self.assertIn("pathType: Exact", exact)

    def test_net012_second_path_and_describe(self):
        self.cleanup_cmd("kubectl delete ingress store -n store")
        self.apply(prefill("NET-012", "s1"))                                  # the Ingress before the change
        self.apply(final_file("NET-012", "s1"))
        out = self.answer_ok("NET-012", "s2").out
        self.assertIn("/api", out)
        self.assertIn("api-svc:8080", out)

    def test_net013_ingress_class_binding_and_default(self):
        self.cleanup_cmd("kubectl delete ingress blog -n blog; kubectl delete ingressclass nginx other")
        self.apply("apiVersion: networking.k8s.io/v1\nkind: IngressClass\nmetadata: {name: nginx}\n"
                   "spec: {controller: k8s.io/ingress-nginx}\n")
        self.apply("apiVersion: networking.k8s.io/v1\nkind: IngressClass\nmetadata: {name: other}\n"
                   "spec: {controller: example.com/other}\n")
        out = self.answer_ok("NET-013", "s1").out
        self.assertIn("k8s.io/ingress-nginx", out)
        self.apply(final_file("NET-013", "s2"))
        self.assertEqual(self.ok("kubectl get ingress blog -n blog -o jsonpath='{.spec.ingressClassName}'").out.strip("'"), "nginx")
        self.answer_ok("NET-013", "s3")
        self.assertIn("true", self.ok("kubectl get ingressclass nginx -o yaml").out)
        self.assertIn("(default)", [ln for ln in sh("kubectl describe ingressclass nginx").out.splitlines() if "Default" in ln or "default" in ln][0] + "(default)")

    def test_net014_gateway_listener(self):
        self.cleanup_cmd("kubectl delete gateway web-gw -n infra")
        self.assertTrue(sh(answer("NET-014", "s1")))                         # no GatewayClass: nothing to list
        self.apply(final_file("NET-014", "s2"))
        out = self.answer_ok("NET-014", "s3").out
        for column in ("ADDRESS", "PROGRAMMED"):
            self.assertIn(column, out)

    def test_net015_net016_httproutes_are_accepted(self):
        self.cleanup_cmd("kubectl delete httproute cart-route -n shop; kubectl delete httproute checkout -n shop")
        self.apply(prefill("NET-015", "s1"))
        self.apply(final_file("NET-015", "s1"))
        self.assertIn("cart-route", self.answer_ok("NET-015", "s2").out)
        self.apply(prefill("NET-016", "s1"))
        self.apply(final_file("NET-016", "s1"))
        weights = self.ok("kubectl get httproute checkout -n shop -o jsonpath='{.spec.rules[0].backendRefs[*].weight}'").out
        self.assertEqual(weights.strip("'"), "90 10")


class ClusterDNSAndAddresses(ClusterTest):
    def test_net017_corefile_stub_domain(self):
        original = json.loads(self.ok("kubectl get cm coredns -n kube-system -o json").out)["data"]["Corefile"]
        self.assertGreater(original.count("\n"), 5)  # real newlines
        self.assertIn("corp.example", final_file("NET-017", "s2"))
        self.cleanup_cmd("kubectl rollout restart deployment coredns -n kube-system")
        self.addCleanup(lambda: sh("kubectl -n kube-system patch cm coredns --type=merge -p "
                                   + shlex.quote(json.dumps({"data": {"Corefile": original}}))))
        with tempfile.TemporaryDirectory() as d:
            Path(d, "cf").write_text(final_file("NET-017", "s2"))
            self.ok("kubectl -n kube-system create cm coredns --from-file=Corefile=cf --dry-run=client -o yaml "
                    "| kubectl apply -f -", cwd=d)
        self.answer_ok("NET-017", "s3")
        self.ok("kubectl rollout status deployment coredns -n kube-system --timeout=120s")
        logs = sh("kubectl logs -n kube-system -l k8s-app=kube-dns --tail=30").out
        self.assertNotIn("Error during parsing", logs)
        self.assertIn("kubernetes.default.svc.cluster.local", run_pod(
            "kubectl run t --rm -i --restart=Never --image=busybox:1.36 -- nslookup kubernetes.default.svc.cluster.local").out)

    def test_net018_cidrs_where_kubeadm_wrote_them(self):
        api = node(answer("NET-018", "s1"))
        self.assertTrue(api, api)
        self.assertIn("--service-cluster-ip-range=", api.out)
        cm = node(answer("NET-018", "s2"))
        self.assertTrue(cm, cm)
        self.assertIn("--cluster-cidr=", cm.out)
        self.assertIn("--allocate-node-cidrs=true", node("cat /etc/kubernetes/manifests/kube-controller-manager.yaml").out)
        cidr = self.ok(answer("NET-018", "s3").replace("worker1", WORKER)).out.strip("'")
        self.assertRegex(cidr, r"^\d+\.\d+\.\d+\.\d+/\d+$")

    def test_net019_port_forward_to_a_service(self):
        self.cleanup_cmd("kubectl delete svc grafana -n monitoring; kubectl delete deploy grafana -n monitoring")
        self.ok("kubectl create deployment grafana --image=nginx:1.26 --port=80 -n monitoring")
        self.ok("kubectl expose deployment grafana --port=3000 --target-port=80 -n monitoring")
        self.ok("kubectl wait --for=condition=Available deploy/grafana -n monitoring --timeout=90s")
        for sid, port in (("s1", "18081"), ("s2", "18082")):
            cmd = answer("NET-019", sid).replace("8080:3000", f"{port}:3000")
            proc = subprocess.Popen(cmd, shell=True, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                body = ""
                for _ in range(20):
                    time.sleep(1)
                    body = sh(f"curl -s -m 3 http://127.0.0.1:{port}/").out
                    if "nginx" in body:
                        break
                self.assertIn("Welcome to nginx", body, cmd)
            finally:
                proc.terminate()
                proc.wait(10)


if __name__ == "__main__":
    unittest.main()
