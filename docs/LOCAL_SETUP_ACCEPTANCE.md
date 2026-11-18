# Local provider setup: acceptance and limits

This records the unpublished Windows/Linux distribution follow-up. It does not replace original packaging evidence or approve publication. All implementation, dependency installs and fixture state belonged to this distribution. The working installation, its personal configuration and processes were untouched.

## Shipped flow and contracts

- Windows: install CPython 3.11 x64 and documented OS prerequisites; run install-windows.ps1, run-windows.ps1 -Setup, then -Check. Only explicit -EnableDevices enters the existing native app. It may initialize devices and local STT/VAD models; this round did not launch it.
- Linux: run install-linux.sh, run-linux.sh setup, run-linux.sh check, then run-linux.sh chat/ask. This is terminal-only. Local import-text/search need no selected Brain model. No Linux HUD, tray, audio, catalog/Town, desktop tools or network listener.
- Private routes.json is revisioned/atomic configuration feeding the existing BrainRouter and Windows screen-understanding path, not another dispatch router. Configuration IDs are not character UUIDs.
- Add saves an unassigned exact model. Brain and Vision independently select primary then fallbacks; only chosen, enabled candidates are considered. Groq supports Brain only in these transports. Declared capability is not a model probe.
- Hidden interactive credential entry refuses echo fallback. Credential writes preserve other pools and compare original content hash. Running pools cache keys, so rotation requires restarting that user's own instance.
- Saving credentials never grants speech roles, enrolls residents or calls a model. Explicit setup refuses independent legacy image-pointing/default-caption paths rather than bypassing route choices.
- Passive check never connects. The separately opted-in test makes one bounded metadata GET with no redirects, retries or generation. Model-list presence is not health, quota, image support or answer success.
- Existing catalog selectors/gates or an installed default catalog cause reconciliation refusal. Setup does not migrate an enrolled installation or waive its serving choices.
- Existing owner, generation, Stop, quota and catalog guards remain authoritative. Vision checks before dispatch and acceptance under one existing total timeout budget. Local cancellation cannot prove remote cancellation.
- Public projections/errors omit keys/endpoints. Windows protects only its own user profile with user/SYSTEM/Administrators ACLs and refuses redirected/permissive profiles. Linux checks private ownership/modes.
- Scoped intentional-stop preserves hide versus exit. Automatic sign-in is unavailable because the old registered launcher does not select this environment/configuration.

## Fresh verification

Separate runs below: earlier overlapping reruns are not additional coverage.

| Command/check | Result | Evidence scope |
| --- | --- | --- |
| python -I -B tests/setup/test_user_routes.py | **55 passed**, 2.089s | Actual configuration/store/router paths and fake providers: exact order, persistence, redaction, capability independence, exhaustion, failed/stale writes, catalog boundary, ownership/Stop and hidden-fallback refusal. |
| python -I -B tests/setup/test_windows_profile.py | **1 passed**, 6.286s | Actual Windows PowerShell wrapper, disposable HOME/LOCALAPPDATA, init/add/chain/check/reopen, protected ACL, missing-profile read and outside-profile refusal. No app/devices. |
| python -I -B tests/linux/test_linux_entry.py | **20 passed**, 0.708s | Terminal/archive/knowledge contracts on Windows-hosted CPython 3.11, not native Linux acceptance. |
| python -I -B tests/linux/test_sdk_offline.py | **1 passed**, 9.318s | Real pinned SDK mock-stream through LinuxSession, fake metadata adapter, external connections forbidden. |
| Selected existing API pool/routing/catalog pytest regressions | **59 passed**, 3 deselected, no errors/failures/skips | Actual modules/isolated stores; fake PyAudio format adapter fails if device constructed, external connections prohibited. |
| software/test_light_lifecycle.py | **8 passed**, no errors/failures/skips | Fake HUD/devices and owned ephemeral loopback ASGI sockets, no production port contacted. |
| packaging/check_native_entry.py | **exit 0** | Actual main.py --help with model/device-import and network sentinels. Parser/dependency compatibility only. |
| Windows installer, complete hash lock, pip check | **passed** | Isolated native CPython 3.11 environment, no global packages modified. |
| Actual pkg_resources import | **passed** | Confirms the bundled interpreter's required setuptools API, beyond dependency metadata checks. |

Native versions: Typer 0.12.5, Click 8.1.7, Uvicorn 0.34.3, MCP 1.26.0, pywebview 5.2, patched Open Interpreter 0.4.3, setuptools 80.9.0. Bootstrap: pip 25.3, wheel 0.45.1, uv 0.13.8. The final lock hash and machine-readable results are in the privately preserved setup-verification report.

The 59-test selection covered software/test_api_pools.py, software/test_routing_view.py and software/test_catalog_startup.py, excluding exactly:

- test_shared_cli_passes_prepared_identity_to_light
- test_cli_missing_configuration_exits_before_services_or_profile
- test_catalog_town_loading_skips_migration_and_visibility_preserves_pause

Those older full native/Light integration cases were **not passed by this selection**. New setup/parser sentinels verify the changed distribution entry contract, not a full native app. Existing conftest emitted one invalid-escape DeprecationWarning.

The exact pytest selector was: -k "not test_shared_cli_passes_prepared_identity_to_light and not test_cli_missing_configuration_exits_before_services_or_profile and not test_catalog_town_loading_skips_migration_and_visibility_preserves_pause". The runner supplied isolated state, fake device and connection sentinels. Do not run against a user's devices to work around those exclusions.

Final passive syntax/assets/wheel, full retained-history scan and source comparison are recorded separately in packaging/source-checks.json, packaging/publication-scan.json and the privately preserved source-comparison report. Source comparison covers 565 explicitly baselined code/assets/tests, without reading personal/runtime content. Current publication file changes are recorded in packaging/publication-changes.json.

## Initial failures and overlapping reruns

1. Native lock initially conflicted: MCP 1.26 needs Uvicorn >=0.31.1 versus old 0.30.6. Final 0.34.3 passed the 8 lifecycle tests.
2. Older bootstrap pip failed certificate verification. Added documented truststore opt-in below pip 24.2; TLS was never disabled.
3. Single-item PowerShell splatting split a truststore option into characters. Typed string arrays fixed invocation.
4. Runtime pip twice encountered InvalidChunkLength index responses, including cache-disabled retry. Pinned uv completed the same hash lock; no --no-deps or TLS bypass.
5. First extended setup run: 39 tests, one AST-fixture lookup error. Corrected lookup to actual module-level kernel function; safety assertion unchanged.
6. First existing-regression collection: one PyAudio import error. Added a format-only fake whose device constructor fails; network/device guards stayed active.
7. Actual installed help exposed Typer/Click make_metavar(ctx) mismatch despite pip check. Pinning Click 8.1.7 fixed the reproduced parser failure.
8. Actual import exposed setuptools 84 removal of pkg_resources, required by the interpreter. Pinning 80.9.0 fixed it.
9. PowerShell/.NET null-clearing left an empty uv boolean. Installer now unsets/restores through the Env provider.
10. Windows PowerShell 5 inherited PowerShell 7 module paths and failed Get-FileHash autoload. Scripts now import their own bundled core manifests. Fixture's separate ACL subprocess needed the same Security import; assertions unchanged.

Earlier 30/39/42/45/48/51/54 selections, repeated installs and earlier 59-test reruns overlap final groups; never add them. Failed installs never wrote a success marker. Owned verification environments/caches are removed after saving results.

## Limits and remaining boundaries

- No clean Linux environment: no installed WSL distro/container runtime. Shell/source/hosted tests are evidence; actual Linux installation and terminal launch remain unverified.
- Windows dependency install and real disposable profile/ACL passed. A fresh OS account, native HUD/WebView, microphone, speaker, capture and model-download behavior were not exercised. Native --help is not native app acceptance.
- No personal credentials, real metadata/provider calls, native devices, MCP servers or external integration used. No provider health/audio-quality claim.
- Existing catalog matcher supports one deployment/base per provider profile. Multiple exact models are supported; distinct same-provider deployments are refused pending broader binding support.
- Windows catalog enrollment, speech-provider setup, restricted legacy image-pointing helper and public sign-in remain separate work. Linux Vision preferences do not create a capture path.
- This release has fresh Git history and excludes the reserved-rights Town visual assets. The older remote history requires the separate sensitive-data removal procedure in docs/GITHUB_HISTORY_REMOVAL.md.
- Multi-account access remains deferred. Working FRIDAY process state was neither observed nor changed.

## Final packaging checks

Both Windows scripts parsed with zero errors. Both Linux scripts passed Bash -n without execution. Initial restricted-shell Bash namespace/Git access failures were environment restrictions; authorized syntax/read-only reruns passed. Tracked Git whitespace check passed, with line-ending normalization warnings only. Four owned native environments, installer caches and attributed temporary stores were removed; no personal profile or production process was touched. The source baseline remained 565 files with zero changes.

Final clean-tree checks: 496 Python files parsed for 3.11, 38 JavaScript files syntax-checked, 25 JSON files parsed, 41 local HTML asset references checked; zero syntax/missing-asset errors. Wheel: 150 entries, no CRC failure or RECORD mismatch; changed wheel member and RECORD match corresponding source, and the Windows lock contains its current hash. Scan: 644 files, zero excluded runtime/private files physically present; two reported synthetic rejection/redaction fixtures. All 293 retained Git objects (214 blobs and 11 commit/tag metadata objects) were inspected, with 27 findings; historical privacy and asset licensing blockers remain.
