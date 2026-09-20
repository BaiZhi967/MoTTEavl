#!/bin/bash
# Deterministic solution that outlives any test deadline: the oracle agent runs
# this and never finishes before the platform cancels the job.
set -eu
printf 'ok\n' > /app/answer.txt
sleep 600
