#!/bin/bash
# Never reachable within the agent timeout: the trial must end in a Harbor
# agent timeout, and the verifier must still produce a reward.
set -u
mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt
echo "agent timed out before producing the answer"
