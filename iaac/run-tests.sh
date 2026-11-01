#!/bin/sh
# Run the cluster tests against the cluster of iaac/cluster.sh (create it first: iaac/cluster.sh up).
#   iaac/run-tests.sh                      all tests
#   iaac/run-tests.sh test_storage         one file
#   iaac/run-tests.sh test_storage.PVBinding.test_x   one test
# KEEP=1 keeps the namespaces the tests created, for a look with iaac/.bin/kubectl.
cd "$(dirname "$0")/tests" || exit 1
export KUBECONFIG="$PWD/../.kubeconfig" PATH="$PWD/../.bin:$PATH"
if [ $# -eq 0 ]; then
  exec python3 -m unittest discover -s . -p 'test_*.py' -v
fi
exec python3 -m unittest -v "$@"
