# Supported modes and acceptance limits

| Feature | Windows native entry | Linux terminal entry |
| --- | --- | --- |
| Installation | CPython 3.11 x64; separate hash-locked native dependencies and pinned installer helper. Isolated Windows environment installed and checked. | CPython 3.11/3.12 x86_64, SQLite FTS5; separate minimal lock. Clean Linux install not available for testing. |
| Application | Shipped `software/main.py` Light/HUD via public entry, existing window/tray lifecycle. Native window not launched in this slice. | Terminal only. No HUD, tray, voice, desktop or HTTP listener. |
| First-run private setup | Protected `%LOCALAPPDATA%/FRIDAY` profile; hidden key/base input. Disposable NTFS profile init/add/check and protected ACL verified; no real user profile modified. | XDG config profile with 0700/0600 ownership checks; Unix enforcement not natively accepted here. |
| Brain model order | Explicit saved primary/fallback candidates through existing BrainRouter. | Same authority; streamed text and no executable model tools. |
| Vision model order | Explicit candidates through native screen-understanding transport. Fake-provider execution tested, no capture/live model test. | Preferences/metadata test only; image execution unavailable. |
| Speech | Existing local input/device path only with explicit `-EnableDevices`; may initialize/download local speech models. Brain keys do not authorize cloud speech. Speaking setup unfinished. | Unavailable. |
| Town identities/Life | Existing source retained. New setup is unenrolled; adding keys/bindings never creates residents. Reviewed catalog path remains separate and conflicting selectors fail. | No catalog/Town attachment. |
| Knowledge and archives | Existing Field Notes source retained. | Explicit text/Markdown import, local FTS/versioned references, durable partial conversation archives. |
| Native image pointing/auxiliary caption models | Legacy independent model calls disabled under explicit setup until they can obey the same route contract. Local OCR/accessibility remains separate. | No desktop control. |
| Settings/sign-in | Profile-local configuration; legacy registry autostart unavailable to prevent registering the wrong environment. No registry writes performed. | Session/browser-only native preferences do not apply. |
| MCP, Telegram, n8n, public ingress, accounts | Not configured/activated by setup. No external startup messages/provider tests executed. | No attachment or remote account service. |

One provider endpoint/deployment per profile is currently supported; multiple exact models are allowed. Providers/models are not identities until a separately reviewed catalog enrollment establishes exact bindings. Keep existing histories and ownership untouched. Windows selected catalog integration requires complete parity with its existing manifest/world; the local-user setup will not reinterpret it.

Unverified: clean Windows OS-account prerequisites, actual native HUD/tray/voice/screen behavior, Linux installation/launch/permissions/flock, live provider reachability/answers and model image/tool capability. Existing product gaps, recent voice/window issues and publication blockers are not closed by package installation or fake tests. Multi-account network access remains a later task.

Optional n8n follow-up: Windows Settings and Linux explicit terminal setup/test/event console are available in source. Linux has no native GUI, model workflow tool or automatic pull worker. No live n8n/Gmail/Calendar/Docs acceptance or deployed counterpart parity is established. See N8N_SETUP.md.

Public redistribution follow-up: reserved-rights Town assets and the dependent Living Town visual page are now excluded. The public page explains the limitation and makes no Town reader/asset requests. Other identity/Roster source remains; automatic resident enrollment is still unavailable. The live installation keeps its original artwork and Town implementation.
