#!/bin/sh
# Run the cluster tests against the cluster of iaac/cluster.sh (create it first: iaac/cluster.sh up).
#   iaac/run-tests.sh                      all tests
#   iaac/run-tests.sh test_storage         one file
#   iaac/run-tests.sh test_storage.PVBinding.test_x   one test
# KEEP=1 keeps the namespaces the tests created, for a look with iaac/.bin/kubectl.
# IAAC_TARGET=vm: the libvirt lab of iaac/vm instead (make vm-up): nodes cp1 (+ cp2, cp3) and node01 over SSH.
# Runs against the same cluster are serialized (flock on iaac/.run-<target>.lock): two suites at once
# would drain the same node and restart the same API server under each other.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")/tests" || exit 1
TARGET=${IAAC_TARGET:-kind}
if [ "$TARGET" = vm ]; then
  export KUBECONFIG="$PWD/../.kubeconfig-vm"
else
  export KUBECONFIG="$PWD/../.kubeconfig"
fi
export PATH="$PWD/../.bin:$PATH"
if [ -z "${IAAC_LOCKED:-}" ] && command -v flock >/dev/null; then
  export IAAC_LOCKED=1
  exec flock -E 75 -w 3600 "$PWD/../.run-$TARGET.lock" "$SELF" "$@"
fi
if [ $# -eq 0 ]; then
  exec python3 -m unittest discover -s . -p 'test_*.py' -v
fi
exec python3 -m unittest -v "$@"
