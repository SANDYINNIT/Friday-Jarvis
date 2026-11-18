# Telegram pilot -> FRIDAY branch map: incomplete until live inspection

No n8n tools are exposed in this session. This is a category-level mapping from the operator's supplied inventory, **not** a claim that the 111 deployed nodes were reviewed. Do not replace a node-level/security review with this table.

| Pilot branch | Telegram dependency to inspect | Intended FRIDAY counterpart | Current evidence/disposition |
| --- | --- | --- | --- |
| Interactive chat/tools | Chat-ID ownership, incoming-message text, Telegram reply destination, model/tool wiring | Exact signed request ID/client/account/session; local request review; authenticated result | Local counterpart tested. Actual pilot tool graph and model credentials UNINSPECTED. |
| Gmail watcher | Owner chat mapping, notices and sender/recipient filters | Namespaced durable outbox notice or exact pending decision | Local pull/store/receipt tested. Remote watcher/filter/credential parity PENDING. |
| Scheduled start summary | Timezone, schedule activation and owner delivery | Explicit Settings opt-in; queued event with ID, order/expiry | No schedule copied/activated; remote implementation PENDING. |
| Scheduled end summary | Timezone, schedule state and owner delivery | Separate queued event, never implied completion | PENDING actual branch inspection. |
| Gmail writes/replies | Reply-to and exact-message confirmation checks | One-use exact action/arguments token, original account/session/expiry | Client/fixture guard tested. Gmail action/schema and remote atomic consumption PENDING. |
| Calendar/schedule tools | Telegram yes/no, reply-to correlation, credential scope | Exact typed pending action; independent Calendar account/permission | No real Calendar writes. Remote port PENDING. |
| Docs creation/editing | Owner IDs, target document, command/approval context | Exact Docs request/proposal, reviewed authorization and result | No Docs account/token copied. Remote port PENDING. |
| Memory tables | Telegram-keyed rows and possible cross-chat scope | New client/account-namespaced workflow storage | No table copied or migrated; schema/keys/retention PENDING. Not character/canonical memory. |
| Notices | sendMessage and silent/interactive distinctions | Inert bounded local event, optional notifications; storage receipt separate | Implemented/tested locally. No automatic model/external action on notice. |
| Pending confirmations | Chat-ID + reply-to + one-use/expiry state | Exact signed event/token/action digest; no bare boolean or oldest-request match | Local persistence/expiry/duplicates tested; authoritative remote state PENDING. |
| Credentials | Telegram/Gmail/Calendar/Docs/model credential bindings | Separate explicit selections; no secret export | No credentials read/copied/changed; reuse requires actual review. |
| Data tables | Project sharing and all Telegram-derived keys | New namespaced stores with transactional duplicate/consent claims | PENDING actual table metadata and concurrency guarantees. |

The separate review draft is not the completed counterpart. No deployed workflow was modified or activated in this session. No live read/write parity is claimed.
