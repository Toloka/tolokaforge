# Matrix helpers

Scripts that support the head-to-head TF-vs-Harbor matrix comparison
(`~/Documents/toloka/new_tolokaforge/docs/evaluation-tracking.md`).

## `prune-docker.sh`

Reclaims Docker disk between matrix rows. A long matrix run (16 rows ×
12 cells) can accumulate 20+ GB of harness-image layers and per-trial
containers — enough that Harbor's per-trial `apt-get install` starts
failing with `You don't have enough free space in
/var/cache/apt/archives/`. Call this between rows in the matrix loop.

Safe to run at any time (Docker refuses to touch layers held by a
running container), but the reclaim only pays off between rows.
