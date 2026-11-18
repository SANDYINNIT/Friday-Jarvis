# Fresh staging verification — 2026-11-16

All application/store tests use synthetic state and fake providers in temporary directories. No FRIDAY runtime, production store, microphone, speaker, screen or real integration was used. Live source was not modified. No clean Linux execution environment is installed on this Windows host.

## Final focused runs (keep separate)

| Check | Fresh result | Scope |
| --- | --- | --- |
| `python -I -B tests/linux/test_linux_entry.py` | **20 passed**, 0 failures (0.670s) | Setup/reopen, passive check, private-file validation, explicit credential precedence, gates, real archive/KB storage, partial/failed/cancelled turns, generation isolation, inert terminal text, fake completion/tool refusal. Windows-hosted CPython 3.12.14. |
| `python -I -B tests/linux/test_sdk_offline.py` | **1 passed**, 0 failures (8.420s) | Actual pinned LiteLLM 1.100.0 import and mock stream, then actual SDK chunk objects through Linux Session/router/archive. Fake Ollama metadata adapter; external connections blocked. Internal Windows event-loop socketpair allowed. |
| Existing `software/test_api_pools.py` | **30 passed**, 0 failures; 1 existing SyntaxWarning | Isolated temporary Python3.12 environment, pytest8.1.1, selected fake credentials, no external connections, no cacheprovider. Directly affected pool-selection regression. |
| Existing `software/test_memory.py -k secret_shapes` | **2 passed**, 0 failures; 1 existing SyntaxWarning | Explicit synthetic token replacements preserve original redaction/storage assertions. Other memory tests were not run. |
| Existing wheel integrity cases | **4 passed**, 0 failures (0.028s) | RECORD, duplicate entries, stale bytecode and machine-path checks on the actual wheel. |
| Generalized redaction fixture | **1 direct assertion group passed** | Actual redactor masks the generic profile path; no provider/device/runtime. |
| Linux dependency locks | **58 pinned packages**; Python3.11/3.12 Linux resolutions have identical versions and SHA256 entries | x86_64 Linux wheel resolution, not an installed Linux smoke test. |
| Temporary Windows dependency closure | **59 packages**, `uv pip check` clean before adding test-only pytest | Separate Windows lock adds platform colorama; Linux lock is not rewritten for Windows. No global/live-venv installs. |
| Explicit tokenizer setup | **2 assets hash verified** | Official public cl100k/o200k tables in temporary venv; no model weights or provider calls. |
| Shell | Both scripts pass Git Bash `bash -n`; actual Windows execution of each refuses with exit2 before install/launch | No `.venv-linux` or install lock created by refusal checks. This is not POSIX Linux execution. |
| PowerShell | `start_friday.ps1` parser reports **0 errors** | Parse only; no native launch. VBS is inspected only, not executed. |
| Passive source/package checks | **490 Python files**, **38 JavaScript files**, 0 syntax errors; 41 local HTML references resolve; wheel integrity/RECORD pass | Python AST checked against3.11 grammar, JavaScript via installed Node `--check`, JSON, local HTML references, wheel CRC and RECORD hashes. |
| Secrets/privacy | See `public-scan.json` and PUBLICATION_REVIEW.md | Working files/wheel and all retained Git blobs; values not exported. Historical privacy blockers remain. |
| Live source integrity | See `source-unchanged.json` | Hash comparison against the original565-file source baseline, not a claim about unrelated actively changing runtime data. |

These rows are separate checks, not a full-suite total. Earlier overlapping runs are not added to them.

## Initial failures and corrective reruns

- Attempting to use the Linux hash lock on the Windows verification host failed because Windows requires an extra platform package (colorama). Resolved with a **separate temporary Windows verification lock**, leaving Linux pins unchanged.
- Initial socket.connect guards blocked Python's own Windows asyncio socketpair setup: a captured selection had **17 tests /7 failures**. The guard now permits only stdlib socketpair calls, still forbidding provider/integration connections.
- A subsequent fixture run had **6 teardown errors** from nondeterministically closed SQLite readers. Readers now use `contextlib.closing`; later17/18-case passes overlapped the final20-case run.
- The first real-SDK empty-cache import attempted a tokenizer download and was blocked. Installer-only bounded hash-checked tokenizer setup was added; runtime verification does not download missing tables.
- One SDK mock attempt also counted internal Windows socketpair connections as external. Corrected the fixture boundary, not application permissions.
- Extending the SDK test to the default Ollama route exposed **four blocked model-metadata connection attempts** despite mock_response. Final fixture supplies a fake Ollama metadata adapter as well; assertions and the network guard remain intact. No live service was contacted.
- One passive inspection script used Windows' default text encoding and failed decoding HTML; switched the reproducible checker to explicit UTF-8.
- The first completed asset check found `/bridge-bootstrap.js` missing from shipped assets. It was a harness-only synthetic bridge reference. Removed it from shipped HTML and inject it only in the existing offline harness. Final local HTML references all resolve; browser campaign not repeated.
- A corresponding-source check found the wheel's scripts/wtf.py omitted from the extraction. Added the exact source member; all wheel source now matches.
- Existing invalid-escape SyntaxWarnings in conftest/profile template are recorded, not converted into a clean broad-suite claim.

## Reproduce on the supported Linux target

```sh
sh ./install-linux.sh
sh ./run-linux.sh check
sh ./run-linux.sh setup
KEY=$(python3 packaging/linux_env.py key)
".venv-linux/$KEY/bin/python" -I -B tests/linux/test_linux_entry.py
".venv-linux/$KEY/bin/python" -I -B tests/linux/test_sdk_offline.py
python3 -I -B packaging/check_distribution.py
python3 -I -B packaging/scan_public.py
```

Use the same explicit `PYTHON` choice/key helper if installing with Python3.12 instead of python3. `check_distribution.py` runs JavaScript checks only when Node is installed and reports that availability; Node is not required for Linux text chat. Existing pytest/browser suites need separate test dependencies and platform setup, not the minimal runtime lock.

Actual clean Linux installation/launch remains pending. No claims of native audio, full HUD, catalog activation, model health, Unix permission enforcement or public ingress acceptance follow from these fixtures.
