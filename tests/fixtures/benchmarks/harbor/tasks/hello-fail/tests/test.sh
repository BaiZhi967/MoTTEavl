#!/bin/bash
# Same deterministic verifier as hello-pass: reward 1 only for the expected
# answer. The paired failing solution never writes it, so reward must be 0 and
# the Run must not report a quality pass.
set -u
mkdir -p /logs/verifier
cd /app 2>/dev/null || true
if [ -f /app/answer.txt ] && [ "$(tr -d '[:space:]' < /app/answer.txt)" = "ok" ]; then
  echo 1 > /logs/verifier/reward.txt
  echo "PASS"
else
  echo 0 > /logs/verifier/reward.txt
  echo "FAIL"
fi
