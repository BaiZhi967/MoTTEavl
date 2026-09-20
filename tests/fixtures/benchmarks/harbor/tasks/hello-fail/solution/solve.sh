#!/bin/bash
# Deterministic failing solution: exits 0 without producing the expected
# artefact, so the process succeeds while the task fails (M3-G09).
set -eu
echo "deliberately not writing the expected answer"
