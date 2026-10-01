# mokvia repository instructions

This is a clean distribution of the local mokvia application. Read docs/SETUP-WINDOWS.md and docs/COPILOT.md before setup. The canonical terminology and entity contracts are CONTEXT.md and docs/data-schema.md.

- Run on approved Windows Docker Linux containers with Compose project `mokvia`, service `app`, port `127.0.0.1:24873`, named volume `mokvia_data` mounted at `/data`.
- Node.js installation on the Windows host is not required. No external hosting, home PC connection, Tailscale, Google Calendar sync, automatic Git commit/push, or required paid service.
- Never change or bypass company PowerShell execution policy, signing requirements, browser policy, firewall policy, or authentication controls. Use browser notification fallback when native helper is unavailable.
- Keep company records, secrets, backups and notification state outside this source tree. Prefer ZIP distribution without `.git`. Never push company data or attach logs to public issues.
- Copilot is manually instructed; do not introduce autonomous AI, AI API keys or scheduled cloud agents.
- Do not read company data or transmit it to AI unless explicitly authorized within company policy. Repository source setup does not grant permission to read business Task contents.
- Entity mutations go through validated preview/apply API with latest-file conflict checks and readback. Never bypass pending recovery by editing canonical Markdown directly.
- Notifications show state only; do not mutate entities from the Windows notification helper. No notification during sleep/offline; resume coalesces current state.
- Preserve existing GTD terminology and UI. The local calendar uses Japan time and distinguishes scheduled Tasks from recorded work sessions.
- Verify start/stop, duplicate start, loopback-only binding, persistence, backup/restore, notification fallback and time calculations. Record Windows-only checks as pending until run on Windows.

- Use the public source at <https://github.com/kiyugithkvjl1024/mokvia>. Start.ps1 provisions the external named volume `mokvia_data` only after preflight. If legacy `gtd-local_gtd_data`-style volumes or Windows `GtdLocal` state exist, stop and identify them; do not delete, import, or bypass the guard. This is a fresh company installation, not a transfer of personal data.
