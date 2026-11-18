# FRIDAY · Field Notes

Local assistant source with a Windows native app and a limited Linux terminal. **Source preview:** platform and live-service acceptance is limited. Read [publication status](PUBLICATION_REVIEW.md), [platform limits](docs/PLATFORMS.md), and [fresh setup evidence](docs/LOCAL_SETUP_ACCEPTANCE.md).

All commands below run from this checkout's root. No keys, models, personal databases or accounts are bundled. A cloud request may cost money. Saving a key does not select a model, assign a role, create a Town resident or send a model request.

## Windows 10/11 x64

Install **CPython 3.11 x64** from [python.org](https://www.python.org/downloads/windows/). The native WebView needs Microsoft's [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) and a suitable .NET Framework/desktop runtime; native libraries may also need the [Visual C++ x64 runtime](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist). These OS components are not installed automatically. Tesseract is optional for OCR; no model weights are downloaded by our installer.

Open PowerShell in the checkout, without administrator privileges:

```powershell
py -3.11 --version
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\run-windows.ps1 -Setup
powershell -NoProfile -ExecutionPolicy Bypass -File .\run-windows.ps1 -Check
```

The execution-policy flag affects that child process only. The installer creates its own version/hash-specific `.venv-windows` environment, installs a complete hash lock, and runs `pip check`. It does not modify system Python, registry/sign-in settings or another FRIDAY installation. The pinned uv helper is an installer transport, not an assistant capability.

The setup wizard asks for a provider, **exact model ID**, public deployment label, capabilities and explicit primary choice. Keys are requested separately through a hidden prompt. Modal's private endpoint is also entered interactively. User configuration lives under `%LOCALAPPDATA%/FRIDAY`; the launcher protects only that directory and verifies its files. An existing permissive or redirected profile causes refusal, not a broad permission repair.

To launch the shipped native application after setup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run-windows.ps1 -EnableDevices
```

**Read before that command:** the existing native app initializes speech/device paths and may download its local STT/VAD models on first use/startup. `-EnableDevices` explicitly acknowledges that behavior; this is not a device-free mode. Local speech input uses the existing native path. **Voice output is not assigned by this Brain/Vision setup**, and saved Brain keys are not reused for cloud STT/TTS. Use text until speech-provider setup receives its own reviewed contract. Neither native display/device quality nor a fresh Windows OS account has been accepted by this source slice. The dependency installation and guarded command parser were tested separately.

If port 10101 is occupied, this launcher refuses to raise or stop an unidentified instance. Restore an already-running app through its own tray menu. Close/hide keeps the app running; use its own **Exit FRIDAY** for a true exit. The distribution's intentional-stop flag is scoped to its profile. Automatic sign-in registration is deliberately unavailable; legacy launchers are not the supported public setup path.

## Linux x86_64 terminal

Supported Python: **3.11 or 3.12**, with SQLite FTS5. Debian 12/Ubuntu 24.04 prerequisites:

```sh
sudo apt update
sudo apt install python3 python3-venv python3-pip git ca-certificates
python3 --version
sh ./install-linux.sh
sh ./run-linux.sh setup
sh ./run-linux.sh check
sh ./run-linux.sh chat
```

For another installed supported Python, prefix install and subsequent commands with `PYTHON=python3.12`. The installer creates `.venv-linux`; no system Python changes or `sudo pip`. Linux has **no HUD, tray, microphone, speech output, Town catalog, desktop control, tools or network listener** in this mode. It provides streamed text, private conversation archives and local text knowledge retrieval. A clean Linux installation/launch remains unverified on this Windows host; no WSL distro/container runtime was available.

Linux private configuration is `${XDG_CONFIG_HOME:-$HOME/.config}/friday`, with user-owned 0700 directory/0600 key file. Data lives under `${XDG_DATA_HOME:-$HOME/.local/share}/friday-linux`. Both launchers preserve existing valid configuration when rerun. Missing/malformed selected configuration fails; it does not fall back to somebody else's file or a default provider chain.

## Choose providers and models deliberately

Local-only Ollama requires no provider account. Install [Ollama](https://ollama.com/download) separately, start it, and deliberately pull a model you chose, for example:

```sh
ollama pull qwen3:8b
```

The installer does not start Ollama or download that model. In setup select `ollama`, enter `qwen3:8b`, and explicitly assign Brain primary. A Vision model must actually support image input; do not assume a text model does.

Supported Brain transports: Ollama, Modal, Groq, OpenRouter and Gemini. Vision: Ollama, Modal, OpenRouter and Gemini on Windows. Groq Vision is not implemented by this runtime. Model format/transport support does not prove model availability, image capability, permissions, quota or tool calling.

Only bindings explicitly put in each capability's ordered chain can be selected. Disabled entries are skipped. If every chosen route fails, FRIDAY reports exhaustion; it does not silently pick an unchosen local/cloud provider. Retry before visible text is supported; partial streamed output is not replayed on another model.

See [configuration commands](docs/CONFIGURATION.md) for add/edit/remove, independent Brain/Vision primary/fallback order, hidden key entry and the optional metadata-only connection test. Nothing is pre-enrolled in Town. Configuration entry IDs are not catalog character UUIDs.

## Local terminal use

```sh
sh ./run-linux.sh ask "Explain this idea briefly."
sh ./run-linux.sh import-text --title "My guide" --file ./guide.md
sh ./run-linux.sh search "relevant subject"
```

Imports are explicit UTF-8 .txt/.md files up to 256 KiB, never automatic scans. Retrieval uses source/version/line references; source text is untrusted reference material, not verified facts or executable instructions. Update a known source by supplying both `--source-id` and `--expected-version`. Conversations retain partial/failed/cancelled outcomes. Ctrl+C exits/cancels locally; an already dispatched transport may still finish remotely. There are no executable model tools in Linux mode.

## Update

Before updating, stop your own FRIDAY instance and check for local changes. This checkout uses a fresh release history. Existing users of the former history must clone afresh instead of merging that history. In a clean release checkout:

```sh
git status --short
git pull --ff-only
sh ./install-linux.sh
sh ./run-linux.sh check
```

On Windows rerun `install-windows.ps1`, then `run-windows.ps1 -Check`. A changed lock creates a separate environment; an unsuccessful install leaves no success marker. Updates do not reinstall residents, migrate personal stores, activate gates or resume Town. Unsupported/stale user configuration requires review rather than silent replacement.

## Uninstall

First exit only your own app/terminal. This installer created no OS startup registration. From the verified checkout root, inspect your current directory, then remove only generated environments:

```sh
pwd
rm -rf -- ./.venv-linux
```

Windows PowerShell equivalent, after verifying `Get-Location` is this checkout:

```powershell
Get-Location
Remove-Item -LiteralPath .\.venv-windows -Recurse
```

You can then remove the checkout when you no longer need it. Private keys/data are **retained**. Decide separately whether to delete your own exact FRIDAY config/data directories; do not delete the surrounding profile, another installation, or histories without review. No registry cleanup is necessary for this installer.

## Troubleshooting and limits

- **No chosen/eligible Brain:** run configuration `show`/`check`; assign a primary/fallback chain, enable the relevant role, and save its own credential if required. Credential presence is not health.
- **Model unavailable/404/permission error:** confirm the exact model with your provider. No guessed replacement or extra provider is selected. A model-list test is not a successful answer test.
- **TLS/index errors during installation:** certificate verification remains enabled. Windows uses OS trust through documented pip/uv support. Repair your approved trust/proxy configuration; never use `--trusted-host` or disable TLS. Interrupted installs can be rerun.
- **Secret prompt unavailable:** use an interactive terminal. The CLI refuses echo fallback and does not accept a key as a command argument.
- **Catalog detected/conflicting selectors:** stop this setup path. An installed catalog needs explicit parity/enrollment review; no identities/world are rewritten. Legacy routing does not enforce an installed catalog's serving choices.
- **Native DLL/WebView/device/model failure:** check OS prerequisites and the selected native environment. Do not infer audio/UI acceptance from installer or mock tests. No public ingress or multi-account networking is enabled.

Licenses and retained attribution: [AGPL project license](LICENSE), [third-party notices](THIRD_PARTY_NOTICES.md). Reserved-rights Town artwork and its dependent visual page are excluded. Older private history is preserved outside this checkout. GitHub retained-object cleanup remains a separate owner action. See [publication status](PUBLICATION_REVIEW.md).

## Optional n8n integration

Private Settings/terminal configuration and the guarded local counterpart are implemented. No deployed workflow or remote account is bundled or activated. This package has offline counterpart tests, while live branch parity and remote action acceptance remain unverified. See [setup and limits](docs/N8N_SETUP.md). Saving/configuring does not activate workflows or redirect ordinary chat. No remote accounts or secrets are included.
