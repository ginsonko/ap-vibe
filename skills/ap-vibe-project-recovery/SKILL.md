---
name: ap-vibe-project-recovery
description: Recover the AP-Vibe project's reviewed local milestone, unreviewed delta, redlines, and next action after context compaction, a daemon restart, or a task handoff. This skill is read-only with respect to cognition and external Vibe knowledge.
---

# AP-Vibe Project Recovery

Use the bundled client instead of opening AP-Vibe SQLite, Codex JSONL, or Vibe data directly:

```powershell
python scripts/restore.py restore
```

The installed client uses `references/installation.json` to locate its own
configuration and actual port. `AP_VIBE_CONFIG_PATH` can explicitly select an
installation for this process. A missing custom configuration is reported;
it does not silently query another workbench.

The command first checks the loopback AP-Vibe daemon. When the daemon is unavailable, it may return the last online-verified portable snapshot with `source_mode=portable_snapshot` and `stale=true`. If neither source is usable, it returns a handled, empty receipt with `context_available=false`; continue the original task and report that no AP-Vibe context was available.

Read the receipt without treating quality fields as a gate. `context_available` says whether any bounded context was returned; `recovery_valid=true` and `authority=local_reviewed_recovery` mean a verified reviewed milestone is present. A receipt with `degraded=true`, `recovery_valid=false`, `authority=temporary_delta_only` or `authority=unavailable` is still usable for read-only orientation: retain its evidence boundary, keep unknowns unknown, and do not present it as reviewed knowledge. Missing revision/hash fields are expected in that state. `brief.latest_delta` is always unreviewed context and never replaces a reviewed milestone. If `stale=true`, say that the data may lag the daemon.

This skill grants no permission to write Vibe knowledge, install skills globally, send messages, call a model, or access APV3's protected database. A failed receipt includes an actionable remedy; do not guess a project, port, revision, or missing fact.
