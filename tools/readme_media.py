#!/usr/bin/env python3
"""Regenerate the README screenshots (docs/media/*.svg) by playing scenes in tmux.

Needs tmux (on Fedora Silverblue: run it in the cka-dev toolbox, `make readme-media` does).
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "media"
W, H = 100, 38
sys.path.insert(0, str(Path(__file__).parent))
import ansi2svg  # noqa: E402


class Scene:
    def __init__(self, *args):
        self.sock = f"cka-media-{time.monotonic_ns()}"
        cmd = f"LANG=C.UTF-8 python3 cka_trainer.py --silent --allow-paste {' '.join(args)}"
        self.tmux("new-session", "-d", "-x", str(W), "-y", str(H), "-c", str(ROOT), cmd)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, *args], check=True,
                              capture_output=True, text=True).stdout

    def keys(self, *keys, wait=0.4):
        self.tmux("send-keys", *keys)
        time.sleep(wait)

    def type(self, text, wait=0.4):
        self.tmux("send-keys", "-l", text)
        time.sleep(wait)

    def answer(self, text):  # type, submit, leave the feedback screen
        self.type(text, 0.2)
        self.keys("Enter")
        self.keys("Enter")

    def capture(self):
        # -N keeps trailing spaces (full-width bars), -e the colours
        return ansi2svg.parse(self.tmux("capture-pane", "-p", "-e", "-N"), W)

    def shot(self, name):
        save(name, [self.capture()], 1)

    def record(self, name, seconds, fps):
        frames, t0 = [], time.monotonic()
        for i in range(int(seconds * fps)):
            frames.append(self.capture())
            time.sleep(max(0.0, t0 + (i + 1) / fps - time.monotonic()))
        save(name, frames, fps)

    def close(self):
        subprocess.run(["tmux", "-L", self.sock, "kill-server"], capture_output=True)


def save(name, frames, fps):
    path = OUT / f"{name}.svg"
    path.write_text(ansi2svg.build(frames, fps), encoding="utf-8")
    print(f"{path.relative_to(ROOT)}  {len(frames)} frame(s)  {path.stat().st_size // 1024} KB")


def main():
    if not shutil.which("tmux"):
        sys.exit("tmux not found (Silverblue: toolbox run -c cka-dev ...)")
    OUT.mkdir(parents=True, exist_ok=True)

    s = Scene()  # title screen: rotating helm, rainbow logo, starfield
    time.sleep(1.0)
    s.record("title", 3, 8)
    s.close()

    s = Scene("--id", "NET-002")  # a command step, half typed
    time.sleep(2.8)
    s.type("kubectl expose deployment web --name=web-svc --port=80 --tar", 4)
    s.shot("command-step")
    s.close()

    s = Scene("--id", "STO-001")  # an edit step with its snippet options
    time.sleep(2.8)
    s.answer("kubectl describe pvc data-pvc")
    s.keys("Up", "Up", "Up", "End", "BSpace", "BSpace", "BSpace", "BSpace", wait=0.1)
    s.type("Once", 0.1)  # - ReadWriteMany -> - ReadWriteOnce
    s.keys("Down", "Down", "Down", "End", "BSpace", "BSpace", "BSpace", "BSpace", wait=0.1)
    s.type("10G", 2)  # storage: 20Gi -> 10G... (caught mid-typing)
    s.shot("edit-step")
    s.close()

    s = Scene("--id", "CLU-002")  # a wrong answer: the trap is explained
    time.sleep(2.8)
    s.type("kubectl cordon node01", 0.2)
    s.keys("Enter", wait=0.6)
    s.shot("feedback")
    s.close()

    s = Scene("--id", "CLU-002")  # perfect run -> YOU WIN with fireworks
    time.sleep(2.8)
    for a in ("kubectl drain node01 --ignore-daemonsets --delete-emptydir-data",
              "sudo kubeadm upgrade node", "kubectl uncordon node01"):
        s.answer(a)
    s.keys("Enter", wait=2.5)
    s.record("you-win", 5, 8)
    s.close()


if __name__ == "__main__":
    main()
