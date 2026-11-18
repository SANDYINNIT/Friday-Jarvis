# Optional n8n setup (local client implemented; remote port pending)

**Do not enable this against the existing review draft expecting a completed service.** The live pilot/draft could not be read because the current session has zero n8n tools. No deployed workflow was edited, activated or verified. The contract template is intentionally marked incomplete, not an importable completed n8n export.

## Windows
After the normal installer and model setup, open Settings -> n8n / Workflows. Enter your deployment origin and a new dedicated shared credential in the private fields. Select capabilities explicitly; leave enabled and notices off until the separate counterpart's ownership, credentials and storage are verified. Blank private fields preserve existing values.

Terminal alternative through the protected launcher:

    .\run-windows.ps1 -N8N -ConfigArgs @('wizard')
    .\run-windows.ps1 -N8N -ConfigArgs @('show')
    .\run-windows.ps1 -N8N -ConfigArgs @('test','--yes-connect')

The last command intentionally contacts status only, never sends an email or edits Calendar. A saved credential or observed status response is not account permission, model health or an executed action. Native Settings changes apply to the next operation; saving creates no connection. Changing revision fences older in-flight results.

## Linux terminal
Use the existing isolated Linux installer. This adds a private optional terminal integration, not native HUD/tray/voice/Town or a public listener:

    sh ./run-linux.sh n8n wizard
    sh ./run-linux.sh n8n show
    sh ./run-linux.sh n8n test --yes-connect
    sh ./run-linux.sh n8n console

Only the explicit test/console operations contact your configured deployment. The console supports status, pull, inbox, review EVENT_ID, approve EVENT_ID, reject EVENT_ID and quit. A review is required before a separately confirmed approval. It is one runtime/session; exiting does not silently transfer old decisions to a replacement. There is no autonomous Linux polling thread or ordinary-chat redirect.

Configuration lives beside your existing private routes.json (same Windows protected profile/XDG Linux config profile), unless FRIDAY_N8N_CONFIG selects another approved private file. Windows wrapper checks that override stays inside its protected profile. No keys in command arguments, repository, logs or shell history. Wizard secret/address prompts require an interactive non-echoing terminal.

Use disable or remove-key through the same n8n command for explicit confirmation. Credential removal disables the local integration; it does not deactivate the n8n workflow, revoke a remote credential, undo work or delete histories.

## Requests, notices and decisions
The Windows native model may propose friday_workflow_request only when n8n is explicitly enabled and a capability selected. It is described as requiring an explicit user selection, not a general chat router. Review the exact proposal in Settings before sending. The existing admitted-owner/generation/Stop/deadline/tool allowance is rechecked at dispatch; unknown outcomes never automatically retry.
Consequential requests propose a remote action. They do not approve it. A separate authenticated event presents the exact action for one-use review, tied to its account/session/digest/expiry. Plain yes/no, model output, approved=true, assistant action consent and Workspace artifact review cannot supply that authorization.
Notices pull from n8n; no Cloud callback to localhost is assumed. Receipt means durably stored, not read, approved or completed. Native runtime polls every30s only with explicit notices opt-in; opening/refreshing Settings/Workflows is passive. Remote content is inert and does not create tasks, memories, model turns or external messages.
Old legacy n8n.json is preserved. The new editor refuses unsupported versions instead of migrating or replacing them; deliberate adoption requires a reviewed backup/configuration decision outside this setup.

## Remote prerequisites and limits
Read software/docs/n8n/CONTRACT.md and PILOT_MAPPING_PENDING.md. Before real use: inspect the actual pilot and draft, validate deployed node versions, separately map client credential to host identity, implement namespaced durable outbox/request/decision state with atomic one-use claims, select your own Gmail/Calendar/Docs accounts/scopes, and run inactive mocked branch parity. Do not copy Telegram chat IDs, personal table rows, secrets or historical data.
This repository includes a schema, placeholder configuration, explicitly incomplete contract template and offline peer. The peer is test-only and cannot be deployed as an automation service. No real email/calendar/docs action, live n8n contract or clean Linux install is accepted here.
Multi-account access remains a separate project. Existing publication/history and art-license blockers remain; do not push the retained checkout.
