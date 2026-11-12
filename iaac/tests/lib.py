"""Helpers for the cluster tests: run the game's own answers against a real Kubernetes.

The tests never type their own commands when the game already has one: they take the correct
option of a step from questions.json (answer()), so a typo or a stale flag in the database fails
here. Only the cluster of iaac/cluster.sh is reachable: it has its own kubeconfig.
"""
import atexit
from datetime import datetime, timezone
import os
import re
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IAAC = ROOT / "iaac"
# IAAC_TARGET=kind (default): the kind cluster of iaac/cluster.sh, nodes are Podman containers.
# IAAC_TARGET=vm: the libvirt lab of iaac/vm (Ubuntu VMs cp1 + node01, or cp1..cp3 + lb + node01
# with HA=1), reached over SSH. The node names are then the ones the questions use.
TARGET = os.environ.get("IAAC_TARGET", "kind")
VM = TARGET == "vm"
SSH_CONFIG = IAAC / "vm" / ".ssh" / "config"
CLUSTER = os.environ.get("CLUSTER", "cka-test")
if VM:
    KUBECONFIG = IAAC / ".kubeconfig-vm"
    CONTROL_PLANE, WORKER = "cp1", "node01"
else:
    KUBECONFIG = IAAC / ".kubeconfig"
    CONTROL_PLANE, WORKER = f"{CLUSTER}-control-plane", f"{CLUSTER}-worker"
PODMAN = os.environ.get("CONTAINER_CLI", "docker" if subprocess.call(
    "command -v docker >/dev/null", shell=True) == 0 else "podman")

sys.path.insert(0, str(ROOT))
import cka_trainer as t  # noqa: E402

DOMAINS, QUESTIONS = t.load_db(t.DEFAULT_DB)
Q = {q["id"]: q for q in QUESTIONS}

ENV = dict(os.environ,
           PATH=f"{IAAC / '.bin'}:{os.environ['PATH']}",
           KUBECONFIG=str(KUBECONFIG),
           HELM_CONFIG_HOME=str(IAAC / ".helm/config"),
           HELM_CACHE_HOME=str(IAAC / ".helm/cache"),
           HELM_DATA_HOME=str(IAAC / ".helm/data"))
ENV.pop("KUBECTL_CONTEXT", None)


class Result:
    def __init__(self, rc, out):
        self.rc, self.out = rc, out

    def __bool__(self):
        return self.rc == 0

    def __str__(self):
        return f"rc={self.rc}\n{self.out}"


def sh(cmd, timeout=120, stdin=None, env=None, cwd=None):
    """Run a shell command; stdout and stderr merged."""
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                       input=stdin, env=env or ENV, cwd=cwd)
    return Result(p.returncode, (p.stdout + p.stderr).strip())


def node(cmd, which=CONTROL_PLANE, timeout=120, stdin=None):
    """Run a command as root on a node: a kind node container (a systemd machine, like a real node)
    or, with IAAC_TARGET=vm, a VM over SSH (sudo)."""
    if VM:
        remote = "sudo sh -c " + shlex.quote(cmd)
        return sh(f"ssh -F {SSH_CONFIG} {which} {shlex.quote(remote)}", timeout=timeout, stdin=stdin)
    return sh(f"{PODMAN} exec -i --privileged {which} sh -c {shlex.quote(cmd)}", timeout=timeout, stdin=stdin)


def control_planes():
    """Names of the control-plane nodes, cp1 first: one on kind and in the exam layout, three in
    the VM lab's HA layout (make vm-up HA=1)."""
    out = sh("kubectl get nodes -l node-role.kubernetes.io/control-plane -o name").out
    return sorted(n.split("/", 1)[1] for n in out.split() if n.startswith("node/"))


def copy_to_node(src, which, dst, timeout=300):
    """Copy a local file or directory to a node (root-owned; executables stay executable)."""
    src = Path(src)
    if not VM:
        return sh(f"{PODMAN} cp {src} {which}:{dst}", timeout=timeout)
    if src.is_dir():
        remote = f"mkdir -p {shlex.quote(dst)} && tar -C {shlex.quote(dst)} -xf -"
        return sh(f"tar -C {shlex.quote(str(src))} -cf - . | ssh -F {SSH_CONFIG} {which} "
                  f"{shlex.quote('sudo sh -c ' + shlex.quote(remote))}", timeout=timeout)
    mode = "0755" if os.access(src, os.X_OK) else "0644"
    remote = (f"mkdir -p {shlex.quote(str(Path(dst).parent))} && cat > {shlex.quote(dst)} "
              f"&& chmod {mode} {shlex.quote(dst)}")
    return sh(f"ssh -F {SSH_CONFIG} {which} {shlex.quote('sudo sh -c ' + shlex.quote(remote))} "
              f"< {shlex.quote(str(src))}", timeout=timeout)


def on_node(cmd):
    """A game command typed on a node: the helpers already run as root, so drop a leading sudo."""
    return re.sub(r"(^|\|\s*|&&\s*|;\s*)sudo ", r"\1", cmd)


def answer(qid, sid):
    """The correct command of a command step, exactly as the game stores it."""
    return t.correct_option(Q[qid]["steps"][sid])["cmd"]


def wrong(qid, sid, index):
    """The command of a wrong option (a trap) of a command step, by its index in the options list."""
    opt = Q[qid]["steps"][sid]["options"][index]
    assert not opt.get("correct"), (qid, sid, index)
    return opt["cmd"]


def final_file(qid, sid):
    """The file a player ends up with after typing the correct snippet of an edit step."""
    step = Q[qid]["steps"][sid]
    return t.apply_snippet(step, t.lines_of(t.correct_option(step)["snippet"]))


def prefill(qid, sid):
    """The file a player sees first in an edit step (before typing anything)."""
    return t.as_text(Q[qid]["steps"][sid]["prefill"])


def wait_for(cmd, expect, timeout=90, every=2):
    """Poll a command until its output contains `expect`; returns the last Result."""
    end, res = time.time() + timeout, None
    while time.time() < end:
        res = sh(cmd)
        if expect in res.out:
            return res
        time.sleep(every)
    return res


def used_namespaces():
    """Every namespace the questions really use: -n NS in commands, 'namespace: NS' lines in
    manifests, namespace 'NS' in scenarios. (Not free prose: it is full of English words.)"""
    found = set()
    for q in QUESTIONS:
        found |= set(re.findall(r"namespace '([a-z][a-z0-9-]*)'", q["scenario"]))
        for step in q["steps"].values():
            texts = [step.get("prefill"), step.get("context")]
            for o in step["options"]:
                texts += [o.get("cmd"), o.get("snippet")] + o.get("aliases", [])
            for text in texts:
                text = t.as_text(text) if text else ""
                found |= set(re.findall(r"(?<![\w-])-n ([a-z][a-z0-9-]*)(?![\w-])", text))
                found |= set(re.findall(r"--namespace[= ]([a-z][a-z0-9-]*)(?![\w-])", text))
                found |= set(re.findall(r"^\s*namespace: ([a-z][a-z0-9-]*)\s*$", text, re.M))
    return found - {"default", "kube-system", "kube-public", "kube-node-lease", "cka-exam"}


_created = []


def ensure_namespaces():
    have = set(sh("kubectl get ns -o name").out.replace("namespace/", "").split())
    for ns in sorted(used_namespaces() - have):
        if sh(f"kubectl create namespace {ns}"):
            _created.append(ns)


@atexit.register
def _cleanup():
    if _created and not os.environ.get("KEEP"):
        sh("kubectl delete namespace " + " ".join(_created) + " --wait=true --timeout=150s", timeout=170)


ATTACH_BANNER = "If you don't see a command prompt"


def settle_node(name=WORKER, timeout=180):
    """Before a drain: wait until `name` holds no Pod that drain would refuse for another reason
    than the one under test: bare Pods (no owner, e.g. local-path helper Pods) and Pods still
    terminating (a previous test deleted them with --wait=false). Mirror (static) Pods are fine."""
    import json
    end = time.time() + timeout
    while time.time() < end:
        res = sh(f"kubectl get pods -A --field-selector spec.nodeName={name} -o json")
        items = json.loads(res.out).get("items", []) if res else []
        busy = [p["metadata"]["name"] for p in items
                if p["metadata"].get("deletionTimestamp")
                or (not p["metadata"].get("ownerReferences")
                    and "kubernetes.io/config.mirror" not in p["metadata"].get("annotations", {}))]
        if res and not busy:
            return
        time.sleep(2)
    raise AssertionError(f"{name} still runs bare or terminating Pods: {busy}")


def run_pod(cmd, expect=None, timeout=240):
    """Run a game answer of the form `kubectl run NAME --rm -i[t] ... -- COMMAND` (no TTY here).

    kubectl loses the container's output when the container exits before kubectl attaches (seen
    when images are cached and the command is quick, e.g. nslookup). The exact command still ran
    (its exit code is kept), so when `expect` is missing from an output that holds only kubectl's
    attach banner, the same Pod is replayed detached and its logs are read instead."""
    res = sh(cmd.replace(" -it ", " -i "), timeout=timeout)
    if expect is None or expect in res.out or ATTACH_BANNER not in res.out:
        return res
    name = re.search(r"kubectl run (\S+)", cmd).group(1)
    ns = re.search(r"(?:-n|--namespace)[= ](\S+)", cmd)
    ns = f"-n {ns.group(1)}" if ns else ""
    detached = re.sub(r" --rm\b| -it?\b", "", cmd)
    sh(f"kubectl delete pod {name} {ns} --ignore-not-found --wait=true")
    started = sh(detached, timeout=timeout)
    end = time.time() + timeout
    while time.time() < end and sh(f"kubectl get pod {name} {ns} -o jsonpath='{{.status.phase}}'"
                                   ).out.strip("'") not in ("Succeeded", "Failed"):
        time.sleep(2)
    logs = sh(f"kubectl logs {name} {ns}")
    sh(f"kubectl delete pod {name} {ns} --ignore-not-found --wait=false")
    return Result(res.rc, f"{res.out}\n[replayed detached: {started.out}]\n{logs.out}")


class ClusterTest(unittest.TestCase):
    """Skipped when the cluster is not there (run `iaac/cluster.sh up`)."""

    @classmethod
    def setUpClass(cls):
        if not KUBECONFIG.exists():
            raise unittest.SkipTest("no cluster: run iaac/cluster.sh up")
        if not sh("kubectl get --raw /readyz"):
            raise unittest.SkipTest("cluster not reachable")
        ensure_namespaces()

    # -- assertions that print what the cluster said
    def ok(self, cmd, expect=None, **kw):
        res = sh(cmd, **kw)
        self.assertTrue(res, f"$ {cmd}\n{res}")
        if expect is not None:
            self.assertIn(expect, res.out, f"$ {cmd}\n{res}")
        return res

    def fails(self, cmd, expect=None, **kw):
        res = sh(cmd, **kw)
        self.assertFalse(res, f"$ {cmd}\nexpected a failure, got:\n{res}")
        if expect is not None:
            self.assertIn(expect, res.out, f"$ {cmd}\n{res}")
        return res

    def answer_ok(self, qid, sid, expect=None, **kw):
        """Run the correct answer of a step as the player would type it."""
        return self.ok(answer(qid, sid), expect, **kw)

    def apply(self, yaml, *flags):
        """kubectl apply; whatever it creates is deleted when the test ends (no leaks between tests)."""
        res = self.ok(f"kubectl apply {' '.join(flags)} -f -", stdin=yaml)

        def undo():
            try:
                sh("kubectl delete --ignore-not-found --wait=false -f -", stdin=yaml, timeout=60)
            except subprocess.TimeoutExpired:
                pass
        self.addCleanup(undo)
        return res

    def cleanup_cmd(self, cmd):
        """Run `cmd` when the test ends, pass or fail. Errors and hangs are ignored: a stuck
        cleanup must never fail a test or block the run."""
        def run():
            try:
                sh(cmd, timeout=90)
            except subprocess.TimeoutExpired:
                pass
        self.addCleanup(run)


# ---------------------------------------------------------------------------------------------
# Control-plane edits (from CKS Megatrainer): restart detection that also waits for the controllers
# ---------------------------------------------------------------------------------------------

DESTRUCTIVE = os.environ.get("DESTRUCTIVE") == "1"
APISERVER = "/etc/kubernetes/manifests/kube-apiserver.yaml"
APISERVER_ORIG = "/root/kube-apiserver.yaml.iaac-orig"  # outside the manifests dir: never a 2nd Pod
FAKE_IMAGE = re.compile(r"^( *)(- )?image: (?:registry\.(?:local|example\.com)\S*)[ \t]*$", re.M)


def runnable(yaml, node_name=WORKER):
    """The game's manifests may use made-up images (registry.local/...) and node names (node01):
    swap them for busybox sleeping, and the real worker."""
    def sub(m):
        indent = m.group(1) + ("  " if m.group(2) else "")
        return f'{m.group(1)}{m.group(2) or ""}image: busybox:1.36\n{indent}command: ["sleep", "3600"]'
    return FAKE_IMAGE.sub(sub, yaml).replace("nodeName: node01", f"nodeName: {node_name}")


def apiserver_ids(which=CONTROL_PLANE):
    return set(node("crictl ps --name kube-apiserver --state running -q", which).out.split())


def controllers_active(since, timeout=400):
    """True once kube-controller-manager and kube-scheduler both renewed their leader Lease after
    `since` (epoch seconds). Their Pods' Ready condition is no proof: right after an API server
    restart the mirror Pod still shows the old status, and each restart makes both lose their lease
    and exit (the kubelet may hold them in CrashLoopBackOff for minutes)."""
    end = time.time() + timeout
    while time.time() < end:
        fresh = 0
        for lease in ("kube-controller-manager", "kube-scheduler"):
            got = sh(f"kubectl get lease {lease} -n kube-system -o jsonpath='{{.spec.renewTime}}'").out.strip("'")
            try:
                stamp = datetime.strptime(got[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            fresh += stamp >= since - 1
        if fresh == 2:
            return True
        time.sleep(3)
    return False


def wait_apiserver_restart(old, timeout=300, which=CONTROL_PLANE):
    """True once a NEW kube-apiserver container runs on `which`, /readyz answers, and the
    controllers are back (see controllers_active)."""
    end = time.time() + timeout
    while time.time() < end:
        now = apiserver_ids(which)
        if now and not now & old and sh("kubectl get --raw /readyz", timeout=10):
            break
        time.sleep(3)
    else:
        return False
    return controllers_active(time.time())


def set_flags(manifest, flags):
    """Set kube-apiserver flags ('--name=value'): replace the flag if present, else append it."""
    lines = manifest.splitlines()
    for flag in flags:
        name = flag.split("=", 1)[0] + "="
        idx = [i for i, ln in enumerate(lines) if ln.strip().startswith("- " + name)]
        if idx:
            lines[idx[0]] = lines[idx[0]].split("- ")[0] + "- " + flag
        else:
            at = next(i for i, ln in enumerate(lines) if ln.strip() == "- kube-apiserver")
            lines.insert(at + 1, lines[at].split("- ")[0] + "- " + flag)
    return "\n".join(lines) + "\n"


def flags_of(text, names):
    """The '--name=value' flags of a game file (a kube-apiserver manifest or excerpt)."""
    found = re.findall(r"^\s*- (--[a-z-]+=\S+)\s*$", text, re.M)
    return [f for f in found if f.split("=", 1)[0] in names]


class ControlPlaneTest(ClusterTest):
    """Tests that edit the control plane for real. Opt-in (DESTRUCTIVE=1); the original
    kube-apiserver manifest is restored even when the test fails."""

    def setUp(self):
        if not DESTRUCTIVE:
            self.skipTest("changes the control plane: run with DESTRUCTIVE=1 (make iaac-test-destructive)")

    def edit_apiserver(self, change):
        """change(manifest text) -> new text. Waits for the new API server; restores at the end."""
        if not node(f"cp -n {APISERVER} {APISERVER_ORIG}"):
            self.fail("cannot back up the kube-apiserver manifest")
        if not getattr(self, "_restore_registered", False):
            self._restore_registered = True
            self.addCleanup(self.restore_apiserver)  # registered first: runs last
        old = apiserver_ids()
        new = change(node(f"cat {APISERVER}").out + "\n")
        self.assertTrue(node(f"cat > /root/kas.new && cp /root/kas.new {APISERVER} && rm /root/kas.new",
                             stdin=new))
        return wait_apiserver_restart(old)

    def restore_apiserver(self):
        if not node(f"test -f {APISERVER_ORIG}"):
            return
        if node(f"cmp -s {APISERVER_ORIG} {APISERVER}"):  # the answer put it back: no restart will come
            node(f"rm -f {APISERVER_ORIG}")
            return
        old = apiserver_ids()
        node(f"cp {APISERVER_ORIG} {APISERVER}")
        if wait_apiserver_restart(old):
            node(f"rm -f {APISERVER_ORIG}")
