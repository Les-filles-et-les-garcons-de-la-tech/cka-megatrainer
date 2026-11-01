"""Helpers for the cluster tests: run the game's own answers against a real Kubernetes.

The tests never type their own commands when the game already has one: they take the correct
option of a step from questions.json (answer()), so a typo or a stale flag in the database fails
here. Only the cluster of iaac/cluster.sh is reachable: it has its own kubeconfig.
"""
import atexit
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
KUBECONFIG = IAAC / ".kubeconfig"
CLUSTER = os.environ.get("CLUSTER", "cka-test")
CONTROL_PLANE = f"{CLUSTER}-control-plane"
WORKER = f"{CLUSTER}-worker"
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
    """Run a command inside a kind node container (they are systemd machines, like real nodes)."""
    return sh(f"{PODMAN} exec -i --privileged {which} sh -c {shlex.quote(cmd)}", timeout=timeout, stdin=stdin)


def answer(qid, sid):
    """The correct command of a command step, exactly as the game stores it."""
    return t.correct_option(Q[qid]["steps"][sid])["cmd"]


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
