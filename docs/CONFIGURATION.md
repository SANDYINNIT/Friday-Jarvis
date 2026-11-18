# Exact local model setup

Use the supported launcher from the repository root. `setup` is interactive; `configure` commands manage the same private `routes.json` and credential authority consumed by the existing BrainRouter and native screen-understanding path. No second dispatch router is created.

| Action | Linux | Windows PowerShell (inside a permitted script session) |
| --- | --- | --- |
| First-run wizard | `sh ./run-linux.sh setup` | `.\run-windows.ps1 -Setup` |
| Saved choices/reasons | `sh ./run-linux.sh configure show` | `.\run-windows.ps1 -Configure show` |
| Passive validation | `sh ./run-linux.sh check` | `.\run-windows.ps1 -Check` |
| Key entry | `sh ./run-linux.sh configure key groq_brain` | `.\run-windows.ps1 -Configure -ConfigArgs @('key','groq_brain')` |

If Windows execution policy requires it, invoke the script using the transient `powershell -NoProfile -ExecutionPolicy Bypass -File ...` form in README. Never paste a key into arguments, chat, logs or the repository. Noninteractive/echoing secret prompts are refused. The selected pool's saved key list is replaced only after confirmation by file replacement; unrelated pools and route choices remain unchanged.

## Provider setup

| Provider | Brain key field | Vision key field | Where to obtain your own key |
| --- | --- | --- | --- |
| Ollama | None | None | Separately installed loopback service/model. |
| Modal | `modal_brain` | `modal_vision` | Your own existing OpenAI-compatible Modal deployment/bearer proxy token; private HTTPS base entered through hidden input. FRIDAY does not deploy it. |
| Groq | `groq_brain` | Unsupported | [Groq console](https://console.groq.com/keys) |
| OpenRouter | `openrouter_brain` | `openrouter_vision` | [OpenRouter account](https://openrouter.ai/settings/keys) |
| Gemini | `gemini_brain` | `gemini_vision` | [Google AI Studio](https://aistudio.google.com/apikey) |

Cloud calls can cost money. Accounts, models, image capability and tool support must be checked with the provider. A manually declared capability says which supported transport to use, not that the model was probed or validated. No Brain/Vision key is reused to grant cloud listening/speaking access in explicit setup.

## Add, order, edit, disable and remove

First launch wizard can create a chosen primary without copying IDs. Advanced commands below use `PRIMARY_ID`/`FALLBACK_ID`/`ROUTE_ID` as **placeholders for exact IDs printed by add/show**, not literal input:

```sh
sh ./run-linux.sh configure add ollama qwen3:8b --roles brain --deployment ollama-local
sh ./run-linux.sh configure add ollama llava:7b --roles vision --deployment ollama-local
sh ./run-linux.sh configure chain brain PRIMARY_ID FALLBACK_ID
sh ./run-linux.sh configure chain vision VISION_PRIMARY_ID VISION_FALLBACK_ID
sh ./run-linux.sh configure disable ROUTE_ID --capability brain
sh ./run-linux.sh configure enable ROUTE_ID --capability brain
sh ./run-linux.sh configure edit ROUTE_ID --model YOUR_EXACT_MODEL_ID
sh ./run-linux.sh configure remove ROUTE_ID
```

Windows uses the same arguments through `-Configure -ConfigArgs @('chain','brain','PRIMARY_ID','FALLBACK_ID')`, etc. `add` saves an unassigned binding; it never puts it into a chain automatically. Chain order is primary first, then fallbacks. An empty chain removes that capability's assignments. `--roles brain,vision` is permitted only for transports supporting both; actual model capabilities remain unverified. Per-capability disable leaves the other capability intact. Global `disable ROUTE_ID` disables both. Removing a binding removes only its references from both chains and preserves other settings/credentials.

`edit --roles` refuses to leave incompatible chain references or disabled-role flags; unassign them explicitly before narrowing capabilities. `edit --prompt-base` asks for a private endpoint interactively. Binding configuration IDs remain stable across key rotation/reorder, but are **not** catalog UUIDs. Changing a model/deployment never authorizes assigning a historical character to it.

The present runtime/catalog matcher supports **one deployment/base per provider profile**. Multiple exact models on that endpoint are supported; two deployments under the same provider are explicitly refused rather than merged. Cloud providers other than Modal use their inspected standard API bases. Ollama is loopback-only; remote/network-account use is a later task.

## What check and test mean

`show` includes each declared capability, saved chain position, enabled state, missing-key/not-assigned/disabled/cached-cooldown reasons and unknown runtime health. `check` is passive: no provider probe, model generation, store enrollment, device initialization or persistent write. A usable saved Brain choice is required to launch. Runtime eligibility still depends on the existing quota/cooldown/ownership guards.

Optional explicit connection test:

```sh
sh ./run-linux.sh configure test ROUTE_ID --capability brain --yes-connect
```

This sends **one bounded model-list metadata GET**, with no redirects/retries or completion request. Ollama uses `/api/tags`; supported cloud bases use `/models`. Unsupported metadata, missing keys, network/auth errors and a model not appearing remain distinct from a successful answer. Listing a model does not prove image support, quota, answer quality or provider health. No real provider test was run for this slice. Provider-specific charges/policies may apply to any request.

## Persistence, overrides and privacy

- Selected `FRIDAY_USER_ROUTES` file is authoritative. Otherwise the public launcher selects the platform's private `routes.json`. Missing/malformed files fail; no legacy provider inventory is substituted.
- `FRIDAY_CREDENTIALS_FILE` selects a private JSON file; default is the same platform profile's `api_credentials.json`. On Windows, overrides must stay directly inside the same protected profile and pass its ACL checks; Linux custom files must satisfy private ownership/permissions. Existing provider key environment variables supplement file pools, but cannot add unchosen models/providers to chains. Clear unintended inherited key variables when reviewing setup.
- Selected user route order/model/base takes precedence over legacy `FRIDAY_*MODEL/CHAIN` defaults. Explicit cloud capability-off flags still restrict eligibility. Unsetting the public selector and bypassing the public launcher returns the old source-routing behavior; that is not an installed-catalog permission rollback.
- Credential pools are cached by a running app/terminal. After saving or rotating a key, intentionally exit and restart your own instance; a separate check reads saved values and cannot prove what a running process already loaded. Route choices apply at the next selection; already admitted work is not transferred or cancelled.
- The selected setup bypasses legacy dotenv discovery. Keys/endpoints are omitted from public projections and sanitized errors. Native Windows launcher requires a protected, nonredirected profile; Linux private directories/files must be user-owned0700/0600.
- Route writes compare a revision under an exclusive lock and replace the file atomically. Credential writes separately compare the original content hash. Failed/stale writes do not report success; inspect/reload before retrying an uncertain OS failure. Lock recovery is manual after establishing its writer has stopped; never delete an active lock blindly.

No user configuration action enrolls models into Town, edits serving permissions, starts Life/MCP, resolves consent/reviews or changes another account. Windows catalog-backed use requires its existing complete-manifest/operator workflow and reviewed reconciliation; this first-run profile refuses inherited installed-catalog gates/selectors. Different models/deployments must remain different characters on any later enrollment. Modal failure can select chosen Groq Brain while chosen Modal Vision remains independent. No identity/memory is transferred by failover.

## Known execution gaps

Linux can save Vision preferences and perform an explicit metadata test, but cannot capture images or execute Vision/Town/native features. Native screenshot understanding follows the chosen Vision chain. Legacy image-based pointer/describe helpers and auxiliary caption models are refused/skipped under explicit setup because they had independent default model paths. Local OCR and already authorized native controls retain their existing contracts. Do not treat these restrictions as new successful capabilities. Speech-provider configuration, automatic sign-in and catalog enrollment need separate reviewed work.

Configured Vision fallback has one total existing Vision timeout budget, with current-owner/generation/Stop checks immediately before dispatch and before accepting a result. A timed-out call may consume that budget; remote cancellation is never inferred from a local Stop.

Local Linux import/search commands need no chosen Brain model and do not initialize the SDK/tokenizer or call a provider. Use `configure init` if you want an empty local setup before adding model routes. A check can correctly report no usable Brain while those local-only commands remain available.

Optional n8n is configured separately through [N8N_SETUP.md](N8N_SETUP.md); it grants no Brain/Vision role, catalog identity or automatic action consent. The counterpart's remote deployment remains pending inspection.
