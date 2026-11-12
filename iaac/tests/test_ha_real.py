"""HA questions on a REAL HA control plane: the VM lab's HA layout (make vm-up HA=1: cp1, cp2, cp3 with
stacked etcd behind the HAProxy VM 'lb', plus node01). Skipped anywhere else (kind, exam layout).

    make vm-test ARGS=test_ha_real                 the read-only checks
    make vm-test-destructive ARGS=test_ha_real     also: one etcd member down (TRB-036), cp3 re-joined (CLU-027)

The questions name the load balancer lb.k8s.lab and the control planes 192.168.10.11..13; the lab's are
'lb' and 192.168.124.10..12: the tests substitute them.
"""
import re
import time
import unittest

from lib import DESTRUCTIVE, VM, ClusterTest, answer, control_planes, node, sh, wait_for

PKI = "/etc/kubernetes/pki/etcd"
LAB_IP = {"cp1": "192.168.124.10", "cp2": "192.168.124.11", "cp3": "192.168.124.12"}


def on_cp(cmd, which="cp1", timeout=120):
    """A game command typed as root on a control plane, in the etcd PKI directory (the questions'
    relative certificate names), sudo dropped."""
    cmd = re.sub(r"(^|&&\s*)sudo ", r"\1", cmd)
    return node(f"cd {PKI} && {cmd}", which=which, timeout=timeout)


def lab(cmd):
    """The question's names -> the lab's."""
    cmd = cmd.replace("lb.k8s.lab", "lb")
    for name, ip in (("cp1", "192.168.10.11"), ("cp2", "192.168.10.12"), ("cp3", "192.168.10.13")):
        cmd = cmd.replace(ip, LAB_IP[name])
    return cmd


def wait_regex(cmd, pattern, timeout, every=3):
    """Poll a command until its output matches `pattern`; returns the last Result."""
    end, res = time.time() + timeout, None
    while time.time() < end:
        res = sh(cmd)
        if re.search(pattern, res.out):
            return res
        time.sleep(every)
    return res


def ready_through_lb(times, timeout):
    """True once /readyz succeeds `times` times in a row through the load balancer (it spreads the
    requests over every API server)."""
    end, ok = time.time() + timeout, 0
    while time.time() < end:
        ok = ok + 1 if sh("kubectl get --raw /readyz", timeout=15) else 0
        if ok >= times:
            return True
        time.sleep(2)
    return False


class HAReal(ClusterTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not VM or control_planes() != ["cp1", "cp2", "cp3"]:
            raise unittest.SkipTest("needs the VM lab's HA layout (make vm-up HA=1)")

    def test_clu028_three_members_all_healthy(self):
        nodes = self.answer_ok("CLU-028", "s1").out                     # s1: by the role label
        for cp in ("cp1", "cp2", "cp3"):
            self.assertIn(cp, nodes)
        self.assertNotIn("node01", nodes)
        members = on_cp(answer("CLU-028", "s2"))                       # s2: one member per control plane
        self.assertTrue(members, members)
        self.assertEqual(members.out.count(", started, "), 3, members.out)
        health = on_cp(answer("CLU-028", "s3"))                        # s3: every member, in one command
        self.assertTrue(health, health)
        self.assertEqual(len(re.findall(r"is healthy: successfully committed proposal", health.out)), 3, health.out)

    def test_clu026_endpoint_is_the_load_balancer(self):
        server = node(answer("CLU-026", "s5"), which="cp1").out          # s5 on cp1: root's kubeconfig
        self.assertIn("https://lb:6443", server)
        sans = node(answer("CLU-026", "s4"), which="cp1").out            # s4: the certificate covers 'lb'
        self.assertIn("DNS:lb", sans)
        for cp in ("cp2", "cp3"):                                       # joined control planes use it too
            self.assertIn("server: https://lb:6443", node("cat /etc/kubernetes/admin.conf", which=cp).out)
        secret = node(answer("CLU-026", "s3"), which="cp1")              # s3: absent once its 2 h TTL passed
        self.assertTrue("kubeadm-certs" in secret.out or "NotFound" in secret.out, secret)

    def test_clu030_peer_port_and_health_through_the_balancer(self):
        probe = node(lab(answer("CLU-030", "s1")), which="cp2")          # s1: cp1's etcd peer port from cp2
        self.assertTrue(probe, probe)
        self.assertRegex(probe.out, r"succeeded|open|Connected")
        cfg = node("cat /etc/haproxy/haproxy.cfg", which="lb").out       # s3: the lab's backend has that shape
        for cp, ip in LAB_IP.items():
            self.assertIn(f"server {cp} {ip}:6443 check", cfg)
        live = node(lab(answer("CLU-030", "s4")), which="cp1")           # s4: an API server answers via lb
        self.assertTrue(live, live)
        self.assertRegex(live.out, r"(?m)^ok|ok$")                       # curl's progress meter goes to stderr

    # ---------------------------------------------------------------- destructive: real failures
    def test_trb036_one_member_down_keeps_quorum_two_down_stop_writes(self):
        if not DESTRUCTIVE:
            self.skipTest("stops etcd members: run with DESTRUCTIVE=1")
        for cp in ("cp2", "cp3"):                                       # whatever happens: kubelets back on
            self.addCleanup(lambda cp=cp: node("systemctl enable --now kubelet", which=cp))
        self.cleanup_cmd("kubectl delete cm iaac-quorum-1 iaac-quorum-2 --ignore-not-found")
        # start from a healthy cluster (a previous run may still be recovering)
        self.assertTrue(ready_through_lb(6, 300), "the cluster is not healthy before the test")
        self.assertRegex(wait_regex("kubectl get nodes", r"cp3\s+Ready", 300).out, r"cp3\s+Ready")
        # cp3 loses its kubelet and etcd: the scenario of TRB-036
        node("systemctl disable --now kubelet; crictl stop $(crictl ps --name etcd -q)", which="cp3")
        time.sleep(10)
        health = on_cp(answer("TRB-036", "s1"))                         # s1: 2 healthy, 1 unhealthy
        self.assertFalse(health, "etcdctl must exit non-zero with an unhealthy member")
        self.assertEqual(len(re.findall(r"is healthy", health.out)), 2, health.out)
        self.assertIn("is unhealthy", health.out)
        status = on_cp(answer("TRB-036", "s2"))                         # s2: a leader is still there
        self.assertRegex(status.out, r"[|│]\s*true\s*[|│]")
        # quorum 2/3: writes work. Through the round-robin balancer a request may land on cp3's API
        # server, whose LOCAL etcd is down (stacked etcd): retry, one success proves the quorum
        wrote = any(sh("kubectl create configmap iaac-quorum-1 --from-literal=k=v --request-timeout=15s", timeout=40)
                    or sh("kubectl get cm iaac-quorum-1") for _ in range(6))
        self.assertTrue(wrote, "no write accepted with 2 of 3 etcd members")
        # explain: losing a SECOND member breaks quorum, writes stop
        node("systemctl stop kubelet; crictl stop $(crictl ps --name etcd -q)", which="cp2")
        time.sleep(10)
        lost = sh("kubectl create configmap iaac-quorum-2 --from-literal=k=v --request-timeout=20s", timeout=60)
        self.assertFalse(lost, lost)
        node("systemctl start kubelet", which="cp2")                    # back to 2/3
        # cp3's API server still runs on top of its stopped etcd: through the round-robin balancer some
        # requests fail until the fix below, so wait for a real success (rc 0), not for an 'ok' line
        self.assertTrue(ready_through_lb(1, 240), "no API server ready again with 2 of 3 members")
        # s3: the game's fix on cp3, then s4 / s5 from cp1
        self.assertTrue(node(answer("TRB-036", "s3").replace("sudo ", ""), which="cp3"))
        self.assertIn("enabled", node("systemctl is-enabled kubelet", which="cp3").out)
        res = wait_regex(answer("TRB-036", "s4"), r"(?s)(\bRunning\b.*){3}", 300)   # s4: an etcd Pod per plane
        self.assertEqual(len(re.findall(r"\bRunning\b", res.out)), 3, res.out)
        self.assertRegex(wait_regex(answer("TRB-036", "s5"), r"cp3\s+Ready", 300).out, r"cp3\s+Ready")  # s5
        self.assertTrue(ready_through_lb(6, 300), "every API server should be ready again")
        health = on_cp(answer("TRB-036", "s1"))
        self.assertTrue(health, health)

    def test_clu027_rejoin_cp3_with_a_new_certificate_key(self):
        if not DESTRUCTIVE:
            self.skipTest("resets and re-joins cp3: run with DESTRUCTIVE=1")
        # take cp3 out for good: kubeadm reset also removes its etcd member
        self.assertTrue(node("kubeadm reset -f", which="cp3", timeout=300))
        sh("kubectl delete node cp3")
        members = on_cp(answer("CLU-028", "s2"))
        self.assertEqual(members.out.count(", started, "), 2, members.out)
        key = node(answer("CLU-027", "s1"), which="cp1").out.strip().splitlines()[-1]   # s1: a NEW key
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        join = node(answer("CLU-027", "s2").replace("$KEY", key), which="cp1").out.strip()  # s2
        self.assertIn("--control-plane --certificate-key " + key, join)
        token = re.search(r"--token (\S+)", join).group(1)
        ca_hash = re.search(r"--discovery-token-ca-cert-hash (\S+)", join).group(1)
        cmd = (lab(answer("CLU-027", "s3")).replace("$TOKEN", token).replace("$HASH", ca_hash)
               .replace("$KEY", key))                                   # s3: the game's join, on cp3
        res = node(cmd + " --apiserver-advertise-address " + LAB_IP["cp3"], which="cp3", timeout=600)
        self.assertTrue(res, res)
        self.assertIn("This node has joined the cluster and a new control plane instance was created", res.out)
        self.assertTrue(node(answer("CLU-027", "s5") + " && kubectl get nodes", which="cp3"))   # s5 on cp3
        pods = wait_for(answer("CLU-027", "s4").replace("cp2", "cp3"), "etcd-cp3", 300).out    # s4
        for comp in ("kube-apiserver-cp3", "etcd-cp3", "kube-scheduler-cp3", "kube-controller-manager-cp3"):
            self.assertIn(comp, pods)
        node("mkdir -p /root/.kube && cp /etc/kubernetes/admin.conf /root/.kube/config", which="cp3")
        self.assertEqual(on_cp(answer("CLU-028", "s2")).out.count(", started, "), 3)


if __name__ == "__main__":
    unittest.main()
