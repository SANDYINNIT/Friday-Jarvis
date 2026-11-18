# FRIDAY webhook bridge v1

## Implementation status
Local Windows runtime, optional Field Notes Settings, private terminal setup, typed authenticated transport and durable receipts are implemented. A test-only peer exercises the protocol offline.
The live Telegram pilot and separate webhook draft have NOT been inspected or edited in this session because no n8n tools are exposed. Their inactive states and node counts are user-supplied evidence, not current verification. This document/template is **not** a completed n8n workflow/export and must not be advertised as one. Live branch parity, credentials, remote stores and atomic remote decision consumption remain pending.

## Directions
| Operation | Direction and endpoint | Meaning |
| --- | --- | --- |
| Selected request | FRIDAY -> n8n POST /webhook/friday/v1/request | Explicit confirmed local request. Consequential capabilities use mode=propose, never execute permission. |
| Contract test | FRIDAY -> n8n POST /webhook/friday/v1/status | Authenticated contract observation only; not Gmail/Calendar/provider health. |
| Notice retrieval | FRIDAY -> n8n POST /webhook/friday/v1/pull | At most 20 events, only with saved notifications opt-in. Runtime cadence 30 seconds; manual pull is separate. |
| Delivery receipt | FRIDAY -> n8n POST /webhook/friday/v1/ack | Durable local storage receipt, not display, user approval or workflow completion. |
| Exact decision | FRIDAY -> same ack endpoint | One-use event token + original session + action digest + approve/reject choice, following explicit local review. No bare approved boolean. |
| Inbound callback | None added | Never assume n8n Cloud can reach localhost. Existing legacy ingress is unchanged, not activated/exposed. |

These fixed paths are the proposed v1 counterpart contract; they have not been verified against the deployed six-node draft.

## Authentication and JSON
- TLS verification stays enabled. HTTPS origins required; HTTP allowed only for explicit loopback development.
- X-Friday-Token carries the shared client credential. X-Friday-Signature is lowercase hexadecimal HMAC-SHA256 of **exact UTF-8 HTTP body bytes** with that credential. n8n must use its raw body, not reserialize JSON before verification. Responses must likewise sign their exact emitted JSON bytes. Reject redirects; no credentials in URLs, CLI arguments, exports or diagnostics.
- Remote configuration must bind the credential to exactly the registered client_id and account_id=host. Never authorize an incoming account/client claim merely because it exists in JSON. Future accounts require separate credentials/namespaces; account creation/network deployment is not implemented here.
- Envelope fields: version=friday.n8n.v1; origin=friday for outgoing operations/n8n for result/event; kind; stable opaque id; client_id; account_id; session_id; created_at; expires_at; payload object. Times are absolute UTC Unix seconds, not ambiguous local dates. Unknown/missing fields, duplicate JSON keys and non-finite numbers are refused.
- Outgoing operation lifetime 120 seconds. Incoming timestamps allow 30 seconds future skew, at most 24 hours age/lifetime, and must be unexpired. Current signed transport responses must match original request ID, account, client and session.
- 64 KiB maximum request/response, 4,000-character request message, 20 events per pull, 16 KiB per event. Connection/read timeouts 3/15 seconds, 20-second streamed-response processing deadline (an in-progress read may reach its read timeout). No automatic dispatch retry.
- Response outcome is one of ready, accepted, completed, partial, failed, pending_decision, events, received. HTTP 200 alone is not success. workflow_completed=true is only a **remote report**, honored as reported completion only with outcome=completed; it is not independent Google-account verification.
- Network/storage failure after reservation/dispatch has unknown outcome. The same request ID never dispatches again locally. Server must independently deduplicate the complete owner namespace+ID, rejecting changed payloads and concurrent claim races.
- Remote errors and free-form results do not become instructions. Settings receives only typed receipt/outcome fields, not full returned payloads. Inert notice text is bounded/redacted. No notice automatically submits a model turn, email, task, memory, tool or workflow.

## Receipt, event and decision state
Private n8n-receipts.db is separate from personal memories/conversations. It is opened only by explicit network/storage operations; snapshot/inbox of an absent database creates nothing. SQLite transactions reserve requests as unknown before dispatch, then record the remote report. Never delete journals or restore over an open store.
Event records are keyed by authenticated client+host+event ID, verify exact digest and monotonic sequence, and commit a whole batch before ack. Out-of-order/conflicting/expired/wrong-session batches are not acknowledged. Duplicate identical events may receive another storage receipt but never create another local event/action. Ack failure retains the durable event; receipt retry is not an action retry.
Decisions have an exact consequential action, arguments, SHA256 action_digest and random one-use token from independently persisted remote pending state. The signed event binds account/client/session/expiry. The token never enters general UI snapshots. Explicit preview displays exact supported non-sensitive arguments; credential/path/URL-bearing or oversized previews refuse rather than concealing fields and asking approval. Email recipient addresses remain visible for exact review. Approval sends the original token/digest/session and must be consumed atomically remotely before the specific authorized write.
A consumed or uncertain decision is not automatically replayed. New configuration revision, removed capability, disable, Stop, expiry or session replacement refuses dispatch. Previously dispatched remote work may continue; local cancellation is not proof of remote cancellation.
Each runtime has a fresh session; UI reload keeps it, runtime restart does not. Old-session events/decisions require remote reissue with a NEW event identity after reviewed ownership; no silent adoption. Automatic outbox reconciliation/reissue is not implemented until the remote workflow is reviewed.
Records cap at 5,000; further operations fail closed instead of silently deleting history. Inbox previews are bounded to 200 newest records and explicitly mark possible truncation. Administrative retention/recovery tooling remains separate.

## Private setup
Windows live source: Settings -> n8n / Workflows. Public Windows installer users use the same section, or the protected launcher terminal setup described in the public installation guide.
Linux public distribution: terminal only; n8n setup/show/test and interactive event console do not grant HUD, voice, Town or automatic model tools. No automatic event background worker is added to the Linux terminal.

Saving does not test, connect, activate an n8n workflow or dispatch a request. Status test is explicit and can run while requests are disabled. Enablement and allowed capabilities are independent. Notification opt-in starts server-owned pulls at runtime; merely browsing Settings/Workflows never pulls. Credential rotation/removal is private and revision-checked. Legacy n8n.json is preserved and refused by the v2 editor rather than silently migrated.
FRIDAY_N8N_CONFIG selects the exact private file. Public setup otherwise uses its existing protected per-user profile; the live source otherwise uses its existing .friday/n8n.json. Never place it in the checkout.

## Remote implementation still required
Inspect both actual workflows and deployed n8n version/schema first. Preserve Telegram security behavior, bind credentials/stores explicitly, implement atomic request and decision claims, separate namespaced tables and exact source branch ports, then validate inactive workflow with mocks. Do not use workflow static data as a concurrency-safe transaction or copy Telegram-keyed historical rows.
No runtime/export can honestly be called live-ready until those steps pass. No email/Calendar mutation is authorized as a test.

## References
- https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/
- https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.respondtowebhook/
- https://docs.n8n.io/data/data-tables/
- https://requests.readthedocs.io/en/latest/api/
Installed Requests 2.34.2 post/Session.request signatures were inspected, including bytes data, stream, timeout and allow_redirects. No unfamiliar provider SDK or new dependency was added.
