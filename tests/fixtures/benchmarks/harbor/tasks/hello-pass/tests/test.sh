#!/bin/bash
# Deterministic calibration verifier: reward 1 only when the agent produced
# /app/answer.txt with the expected token, otherwise 0. No hidden state, no
# network, no gold answer leakage into the agent's instruction.
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
