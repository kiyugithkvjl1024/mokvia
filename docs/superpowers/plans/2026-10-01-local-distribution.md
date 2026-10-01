# Local GTD Implementation Plan

> For agentic workers: use native execution with isolated file ownership.

**Goal:** Ship an independent clean Windows Docker GTD distribution with local calendar and notifications.
**Architecture:** Retain the existing standard-library Python Store/API and vanilla JS UI. Add a local runtime and Windows display helper; use a Linux data volume.
**Tech Stack:** Python standard library, Docker Compose, Windows PowerShell, vanilla JavaScript.
**Spec:** ../specs/2026-10-01-local-distribution.md

## Global Constraints
No production-repository writes. No real entities/history/credentials. No external sync or AI runtime. Host publish is 127.0.0.1:24873 only. Calendar timezone is Asia/Tokyo. Windows scripts never bypass company execution policy.

## Review Focus
Cross-midnight/year calendar ranges; stale previews; suspend/resume notification bursts; concurrent/multiple startup; backup restoration without secrets or data loss.

## Tasks
- [x] Extract allowlisted source and sanitize all identities; exclude unverified images and historical documents.
- [x] Test local runtime initialization, host boundary, scheduling and notification deduplication before implementation. Add Docker packaging and external data-root initialization.
- [x] Test day/week/month calendar projection and boundaries before implementation. Add calendar UI using existing task preview/apply.
- [x] Add Windows Start/Stop/Backup/Restore/notification helper, browser fallback, Copilot and acceptance docs; test policies and ownership.
- [x] Run focused and full included tests, Docker persistence/backup checks, rendered browser checks, publication audit.
- [ ] Resolve license rights before publication. Prepare exact commit/CI/publication evidence; publish only the clean new repository after the gate.
