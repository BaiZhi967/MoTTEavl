#!/bin/bash
# Deliberately exceeds [verifier].timeout_sec in task.toml without writing a
# reward, so the platform must report verifier_error (never a guessed score).
set -u
mkdir -p /logs/verifier
echo "starting an intentionally slow verifier"
sleep 120
echo 1 > /logs/verifier/reward.txt
