"""Cluster tests for CLU-026..CLU-030 (HA control plane).

The kind cluster has ONE control plane and one worker: no load balancer, no second etcd member.
These tests check what such a cluster CAN prove: the HA kubeadm config validates, the flags the
answers use exist (and the traps' flags do not), the certificate upload / control-plane join
command really work, etcdctl membership and health, the drain/uncordon sequence, the ports, and
the API health endpoint. They never run init/reset/join/upgrade apply for real on the control plane.

Not testable here (see each test): a real control-plane join (CLU-027 s3), quorum loss with
3 members (CLU-028 s3 explain), 'upgrade apply' then 'upgrade node' on several control planes
(CLU-029 s1/s2), ufw (CLU-030 s2: kind nodes have no ufw) and the HAProxy config (CLU-030 s3).
"""
import re
import shlex
import tempfile

from lib import CLUSTER, CONTROL_PLANE, PODMAN, WORKER, ClusterTest, answer, final_file, node, settle_node, sh, wait_for  # noqa: F401

ETCD_POD = f"etcd-{CONTROL_PLANE}"
PKI = "/etc/kubernetes/pki/etcd"
HEX64 = r"[0-9a-f]{64}"


def etcd(cmd):
    """Run a game etcdctl answer inside the etcd static Pod (it has etcdctl), with absolute cert paths."""
    argv = shlex.split(cmd)
    argv = [a.replace("ca.crt", f"{PKI}/ca.crt").replace("server.crt", f"{PKI}/server.crt")
             .replace("server.key", f"{PKI}/server.key") for a in argv]
    return sh(f"kubectl exec -n kube-system {ETCD_POD} -- " + " ".join(shlex.quote(a) for a in argv))


def api_server_url():
    """The real API server URL of the kind cluster (stands in for https://lb.k8s.lab:6443)."""
    return sh("kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'").out.strip("'")


def control_plane_ip():
    return sh(f"kubectl get node {CONTROL_PLANE} -o "
              "jsonpath='{.status.addresses[?(@.type==\"InternalIP\")].address}'").out.strip("'")


class HAControlPlane(ClusterTest):
    """CLU-026..030: highly-available control plane (kubeadm), on a 1-control-plane kind cluster."""

    def upload_certs(self):
        """CLU-027 s1, run for real on the control plane: it only (re)creates the kubeadm-certs Secret
        (and a 2 h token owning it) and prints a new key. Harmless for the running control plane."""
        self.cleanup_cmd("kubectl -n kube-system delete secret kubeadm-certs --ignore-not-found")
        res = node(answer("CLU-027", "s1").replace("sudo ", ""), timeout=120)
        self.assertTrue(res, res)
        self.assertIn('Secret "kubeadm-certs"', res.out)      # CLU-026 s3 context, CLU-027 s2 context
        key = re.search(rf"^({HEX64})$", res.out, re.M)
        self.assertTrue(key, f"no certificate key printed:\n{res.out}")
        return key.group(1)

    def test_clu026_ha_config_upload_certs_and_san(self):
        # s1: the HA config validates (on the worker, like CLU-006's test)
        cfg = final_file("CLU-026", "s1")
        self.assertIn('controlPlaneEndpoint: "lb.k8s.lab:6443"', cfg)
        res = node("cat > /tmp/kubeadm-ha.yaml && kubeadm config validate --config /tmp/kubeadm-ha.yaml",
                   which=WORKER, stdin=cfg)
        self.assertTrue(res, res)
        # trap: indented under networking -> unknown field (the why says kubeadm reports it)
        trap = cfg.replace('controlPlaneEndpoint: "lb.k8s.lab:6443"', '  controlPlaneEndpoint: "lb.k8s.lab:6443"')
        res = node("cat > /tmp/kubeadm-ha-trap.yaml && kubeadm config validate --config /tmp/kubeadm-ha-trap.yaml",
                   which=WORKER, stdin=trap)
        self.assertIn("unknown field", res.out, res)
        # s2 trap: --config cannot be mixed with --control-plane-endpoint (dry run on the worker)
        mixed = node("kubeadm init --config /tmp/kubeadm-ha.yaml --control-plane-endpoint lb.k8s.lab:6443 --dry-run",
                     which=WORKER)
        self.assertFalse(mixed)
        self.assertIn("can not mix '--config' with arguments [control-plane-endpoint]", mixed.out)
        # s2 answer: --upload-certs IS allowed next to --config (dry run; the worker's preflight errors ignored)
        s2 = answer("CLU-026", "s2").replace("sudo ", "").replace("kubeadm-config.yaml", "/tmp/kubeadm-ha.yaml")
        dry = node(s2 + " --dry-run --ignore-preflight-errors=all", which=WORKER, timeout=240)
        self.assertNotIn("can not mix", dry.out)
        # s3: the Secret exists in kube-system after an upload; the traps do not find it
        self.upload_certs()
        self.ok(answer("CLU-026", "s3"), "kubeadm-certs")
        self.fails("kubectl get secret kubeadm-certs", "NotFound")
        self.fails("kubectl -n kube-system get configmap kubeadm-certs", "NotFound")
        # s4: the API server certificate carries the controlPlaneEndpoint host (kind sets one too)
        ccfg = sh("kubectl -n kube-system get cm kubeadm-config -o jsonpath='{.data.ClusterConfiguration}'").out
        endpoint = re.search(r"controlPlaneEndpoint:\s*\"?([^:\s\"]+):\d+", ccfg)
        self.assertTrue(endpoint, ccfg)
        san = node(answer("CLU-026", "s4").replace("sudo ", ""))
        self.assertTrue(san, san)
        self.assertIn(endpoint.group(1), san.out)
        # traps: the CA and the kubelet-client certificate do not carry it
        ca = node("openssl x509 -in /etc/kubernetes/pki/ca.crt -noout -ext subjectAltName").out
        self.assertNotIn(endpoint.group(1), ca)
        client = node("openssl x509 -in /etc/kubernetes/pki/apiserver-kubelet-client.crt -noout -ext subjectAltName").out
        self.assertNotIn(endpoint.group(1), client)

    def test_clu027_new_certificate_key_and_control_plane_join_command(self):
        # s1 trap: 'certs certificate-key' only prints a key, it uploads nothing
        self.assertRegex(node("kubeadm certs certificate-key").out, rf"^{HEX64}$")
        key = self.upload_certs()                                           # s1 answer
        # s2: the printed join is a control-plane join carrying the key
        s2 = answer("CLU-027", "s2").replace("sudo ", "").replace("$KEY", key)
        res = node(s2)
        self.assertTrue(res, res)
        self.assertRegex(res.out, r"kubeadm join \S+:\d+ --token \S+ \\?\s*--discovery-token-ca-cert-hash sha256:[0-9a-f]{64}")
        self.assertIn(f"--control-plane --certificate-key {key}", res.out)
        token = re.search(r"--token (\S+)", res.out).group(1)
        self.cleanup_cmd(f"{PODMAN} exec {CONTROL_PLANE} kubeadm token delete {token}")
        # s2 traps: the flags do not exist
        self.assertIn("unknown flag: --control-plane",
                      node("kubeadm token create --print-join-command --control-plane").out)
        self.assertIn("unknown flag: --print-join-command",
                      node("kubeadm init phase upload-certs --print-join-command").out)
        # s3: cannot really join a 2nd control plane here; check the flags of the answer exist
        join_help = node("kubeadm join --help").out
        for flag in ("--token", "--discovery-token-ca-cert-hash", "--control-plane", "--certificate-key"):
            self.assertIn(flag, join_help)
            self.assertIn(flag, answer("CLU-027", "s3"))
        # s4: the static control-plane Pods of a node, found by field selector in kube-system
        s4 = answer("CLU-027", "s4").replace("spec.nodeName=cp2", f"spec.nodeName={CONTROL_PLANE}")
        out = self.ok(s4).out
        for pod in ("kube-apiserver-", "etcd-", "kube-scheduler-", "kube-controller-manager-"):
            self.assertIn(pod + CONTROL_PLANE, out)
        trap = sh(f"kubectl -n kube-system get pods -l kubernetes.io/hostname={CONTROL_PLANE}")
        self.assertIn("No resources found", trap.out)

    def test_clu028_control_planes_etcd_members_and_kubeconfig(self):
        out = self.answer_ok("CLU-028", "s1").out                           # s1
        self.assertIn(CONTROL_PLANE, out)
        self.assertNotIn(WORKER, out)
        self.assertIn("No resources found", sh("kubectl get nodes -l node-role.kubernetes.io/master").out)
        self.assertIn("unknown flag: --role", sh("kubectl get nodes --role=control-plane").out)
        members = etcd(answer("CLU-028", "s2"))                             # s2: one member per control plane
        self.assertTrue(members, members)
        rows = [ln for ln in members.out.splitlines() if ", started, " in ln]
        self.assertEqual(len(rows), 1, members.out)
        self.assertIn(CONTROL_PLANE, rows[0])
        self.assertRegex(rows[0], r":2380, https://\S+:2379, false$")     # the context's format
        self.assertFalse(sh(f"kubectl exec -n kube-system {ETCD_POD} -- etcdctl --endpoints=https://127.0.0.1:2379 "
                            "--command-timeout=5s member list"))           # trap: no certificates
        health = etcd(answer("CLU-028", "s3"))                              # s3: --cluster checks every member
        self.assertTrue(health, health)
        self.assertIn("is healthy", health.out)
        # (3 members, quorum 2, one down: cannot be reproduced with a single etcd member)
        # s4: the edited kubeconfig is a valid kubeconfig whose server is the load balancer
        kc = final_file("CLU-028", "s4").replace("LS0tLS1CRUdJTi...", "Zm9v")  # placeholders -> valid base64
        with tempfile.NamedTemporaryFile("w", suffix=".kubeconfig") as f:
            f.write(kc)
            f.flush()
            server = sh(f"kubectl config view --kubeconfig {f.name} -o jsonpath='{{.clusters[0].cluster.server}}'")
        self.assertEqual(server.out.strip("'"), "https://lb.k8s.lab:6443", server)
        # trap (http://): the API server rejects plain HTTP on its TLS port
        plain = sh(f"curl -s {api_server_url().replace('https://', 'http://')}/livez")
        self.assertIn("Client sent an HTTP request to an HTTPS server", plain.out)

    def test_clu029_upgrade_commands_and_one_node_at_a_time(self):
        # s1/s2: 'apply' and 'node' cannot be run on several control planes here: check the commands
        self.assertEqual(answer("CLU-029", "s1"), "kubeadm upgrade apply v1.37.1")
        self.assertEqual(answer("CLU-029", "s2"), "kubeadm upgrade node")
        node_help = node("kubeadm upgrade node --help").out
        for phase in ("control-plane", "kubelet-config", "addon"):         # explain: node upgrades this node
            self.assertIn(phase, node_help)
        # s1 trap: kubeadm refuses a version newer than itself (dry run, answer 'n' if it ever asks).
        # Rootless Podman lacks the cpuset cgroup: ignore only that preflight check, as CLU-002 does.
        newer = node("kubeadm upgrade apply v1.38.0 --dry-run --ignore-preflight-errors=SystemVerification",
                     stdin="n\n", timeout=180)
        self.assertFalse(newer)
        self.assertIn("higher than the kubeadm minor release", newer.out)
        # s2 trap: 'upgrade node' takes no version (on the WORKER: there it only touches the kubelet config)
        self.assertFalse(node("kubeadm upgrade node v1.37.1", which=WORKER))
        # s2 trap: a deleted mirror Pod comes back from the unchanged manifest
        sched = f"kube-scheduler-{CONTROL_PLANE}"
        image = sh(f"kubectl -n kube-system get pod {sched} -o jsonpath='{{.spec.containers[0].image}}'").out
        self.ok(f"kubectl -n kube-system delete pod {sched}")
        back = wait_for(f"kubectl -n kube-system get pod {sched} -o jsonpath='{{.spec.containers[0].image}}'", image, 90)
        self.assertEqual(back.out, image)
        # s3/s4: drain then uncordon, on the worker (draining the only control plane is pointless here)
        self.cleanup_cmd(f"kubectl uncordon {WORKER}")
        refused = self.fails(f"kubectl drain {WORKER}", "--ignore-daemonsets")  # trap: DaemonSet Pods
        self.assertIn("cannot delete", refused.out)
        # kind runs metrics-server (an emptyDir) on the worker; a real cp2 hosts no such Pod
        settle_node()
        self.ok(answer("CLU-029", "s3").replace("cp2", WORKER) + " --delete-emptydir-data", "drained", timeout=150)
        self.assertIn("SchedulingDisabled", self.ok(f"kubectl get node {WORKER}").out)
        self.assertIn('unknown command "undrain"', sh(f"kubectl undrain {WORKER}").out)
        self.ok(answer("CLU-029", "s4").replace("cp2", WORKER))
        self.assertNotIn("SchedulingDisabled", self.ok(f"kubectl get node {WORKER}").out)

    def test_clu030_ports_and_health_through_the_api_endpoint(self):
        # tip/explain: the control-plane ports really are listening on a kubeadm control plane
        ss = node("ss -tlnp")
        if not ss:
            self.skipTest(f"no ss in this kind node: {ss.out}")
        for port in (6443, 2379, 2380, 10250, 10257, 10259):
            self.assertRegex(ss.out, rf":{port}\s", f"port {port} not listening")
        # s1: from the worker, TCP to the control plane's etcd peer port (needs nc in the node image)
        if node("command -v nc", which=WORKER):
            cmd = answer("CLU-030", "s1").replace("192.168.10.11", control_plane_ip())
            res = node(cmd, which=WORKER)
            self.assertTrue(res, res)
            self.assertRegex(res.out, r"succeeded|open|Connected")
        # s2 (ufw) and s3 (HAProxy) cannot run on kind nodes: no ufw, no HAProxy in front of one API server.
        self.assertIn("    server cp3 192.168.10.13:6443 check", final_file("CLU-030", "s3"))
        # s4: the health endpoint answers 'ok' to an anonymous HTTPS request
        url = api_server_url()
        live = sh(answer("CLU-030", "s4").replace("https://lb.k8s.lab:6443", url))
        self.assertTrue(live, live)
        # curl prints its progress meter on stderr when stdout is not a terminal: the body comes first
        self.assertTrue(live.out.startswith("ok"), live.out)
        plain = sh(f"curl -s {url.replace('https://', 'http://')}/livez")   # trap: plain HTTP
        self.assertIn("Client sent an HTTP request to an HTTPS server", plain.out)
