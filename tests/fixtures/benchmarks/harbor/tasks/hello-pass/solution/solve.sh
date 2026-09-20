#!/bin/bash
# Deterministic solving solution: the oracle agent runs this and must reach
# reward 1. Nothing here reads the verifier's expectation.
set -eu
printf 'ok\n' > /app/answer.txt
