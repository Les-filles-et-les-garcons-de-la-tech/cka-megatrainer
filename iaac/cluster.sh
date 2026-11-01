#!/bin/sh
# Create, check or delete the test cluster. Everything stays in iaac/: the cluster has its own
# kubeconfig (iaac/.kubeconfig), so your ~/.kube/config and your other clusters are never touched.
#
#   iaac/cluster.sh up       create the cluster (Kubernetes $K8S_VERSION) and install the add-ons
#   iaac/cluster.sh status   show nodes and version
#   iaac/cluster.sh down     delete the cluster
#
# K8S_VERSION  kind node image tag (default v1.37.0, the version the game targets)
# CLUSTER      cluster name (default cka-test)
cd "$(dirname "$0")" || exit 1
K8S_VERSION=${K8S_VERSION:-v1.37.0}
CLUSTER=${CLUSTER:-cka-test}
# A recent kind is needed: kubeadm 1.37 rejects the v1beta3 config older kinds generate. We fetch a
# pinned one into iaac/.bin (checksum verified) instead of touching the kind installed on the system.
KIND_VERSION=${KIND_VERSION:-v0.33.0}
KIND=$PWD/.bin/kind-$KIND_VERSION
fetch_kind() {
  [ -x "$KIND" ] && return
  os=$(uname -s | tr '[:upper:]' '[:lower:]'); arch=$(uname -m)
  case "$arch" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; esac
  url=https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-$os-$arch
  mkdir -p .bin && curl -fsSL -o "$KIND.tmp" "$url" || { echo "cannot download $url" >&2; exit 1; }
  want=$(curl -fsSL "$url.sha256sum" | cut -d' ' -f1)
  got=$(shasum -a 256 "$KIND.tmp" | cut -d' ' -f1)
  [ "$want" = "$got" ] || { echo "kind checksum mismatch" >&2; rm -f "$KIND.tmp"; exit 1; }
  chmod +x "$KIND.tmp" && mv "$KIND.tmp" "$KIND"
}
# A kubectl of the cluster's own version, next to kind: the game teaches the commands of that release
# (a v1.33 client would warn about version skew and miss newer flags). Used by iaac/run-tests.sh.
KUBECTL=$PWD/.bin/kubectl-$K8S_VERSION
fetch_kubectl() {
  [ -x "$KUBECTL" ] && return
  os=$(uname -s | tr '[:upper:]' '[:lower:]'); arch=$(uname -m)
  case "$arch" in x86_64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; esac
  url=https://dl.k8s.io/release/$K8S_VERSION/bin/$os/$arch/kubectl
  mkdir -p .bin && curl -fsSL -o "$KUBECTL.tmp" "$url" || { echo "cannot download $url" >&2; exit 1; }
  want=$(curl -fsSL "$url.sha256")
  got=$(shasum -a 256 "$KUBECTL.tmp" | cut -d' ' -f1)
  [ "$want" = "$got" ] || { echo "kubectl checksum mismatch" >&2; rm -f "$KUBECTL.tmp"; exit 1; }
  chmod +x "$KUBECTL.tmp" && mv "$KUBECTL.tmp" "$KUBECTL"
}
export KIND_EXPERIMENTAL_PROVIDER=${KIND_EXPERIMENTAL_PROVIDER:-$(command -v docker >/dev/null && echo docker || echo podman)}
export KUBECONFIG="$PWD/.kubeconfig"

case "${1:-}" in
  up)
    fetch_kind; fetch_kubectl
    ln -sf "$KUBECTL" .bin/kubectl
    if "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
      echo "cluster '$CLUSTER' already exists"
    else
      "$KIND" create cluster --name "$CLUSTER" --image "kindest/node:$K8S_VERSION" \
        --config kind/cluster.yaml --kubeconfig "$KUBECONFIG" --wait 180s || exit 1
    fi
    ./addons.sh ;;
  status)
    fetch_kubectl; ln -sf "$KUBECTL" .bin/kubectl
    .bin/kubectl --context "kind-$CLUSTER" version 2>&1 | grep -i version
    .bin/kubectl --context "kind-$CLUSTER" get nodes -o wide ;;
  down)
    fetch_kind; "$KIND" delete cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG"; rm -f "$KUBECONFIG" ;;
  *) echo "usage: $0 up|status|down" >&2; exit 2 ;;
esac
