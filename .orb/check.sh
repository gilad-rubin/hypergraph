#!/bin/bash
# What proves a machine: CI's own stages (scripts/ci.sh, which CI's jobs call), so the orb's check and CI cannot drift.
# `orb base --for gilad-rubin/hypergraph` runs it on a machine from a new ready-made snapshot before the old one is
# deleted; a worker runs it before a PR. Arguments pass through (`.orb/check.sh test 3.12` runs one stage).
exec "$(dirname "$0")/../scripts/ci.sh" "$@"
