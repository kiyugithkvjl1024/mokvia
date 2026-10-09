# mokvia distribution

This is application source, not a task-data repository. Company data stays outside this checkout in the local Docker volume; backups stay in a company-approved local directory. Never stage, commit, push, attach, or copy real task data, notification state, backups, credentials, logs, or conversations into this repository.

Use the local Web API preview/apply contract for task changes, re-read every affected entity, preserve user choices, and never retry an unknown apply. No cloud runtime, background calendar sync, Git backup, AI API, or remote home access. Optional manual Outlook acquisition is company-local and read-only; see docs/OUTLOOK-CALENDAR.md. Do not bypass Windows execution policy or company restrictions.

Read docs/SETUP-WINDOWS.md and docs/COPILOT.md before setup. Local Calendar uses Asia/Tokyo. Runtime owns deterministic availability release and break completion; Copilot is manually invoked.

Validate source changes with python3 -m unittest discover -s tests. Optional developer JS checks use node tests/local_calendar_runtime.js and node tests/local_notifications_runtime.js; Node is not required on the company PC.

Public changes contain only generic code, documents, and synthetic tests. The copyright holder approved the current individually permitted use terms in LICENSE. Public source visibility is not permission for unrestricted execution, modification, or redistribution. Preserve prior MIT grants and third-party notices. New third-party assets or code still require appropriate rights and notices.

Outlook manual import is opt-in and company-local. Graph uses only separately approved read-only Microsoft 365 access; classic Outlook uses local COM. Neither writes to Outlook. No OAuth/token setup is performed by this distribution. See docs/OUTLOOK-CALENDAR.md for deferred company acceptance.
