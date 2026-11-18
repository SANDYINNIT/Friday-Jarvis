# Packaging research — 2026-11-16

Research was confined to dependency/platform questions for this distribution. No live source research ledger was edited.

- [Python 3.12 venv documentation](https://docs.python.org/3.12/library/venv.html): use an isolated environment at its final path; environments are not portable/relocatable. The installer therefore does not rename a built environment.
- [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/): all selected packages must be hash pinned. Installer uses the complete closure and binary wheels only.
- [LiteLLM 1.100.0 metadata](https://pypi.org/pypi/litellm/1.100.0/json): pinned direct SDK version matches the existing runtime's package metadata. Exact installed signatures/source were inspected in a separate temporary environment; completion's model/messages/stream/timeout/api_key/max_tokens parameters and api_base/mock_response kwargs are supported there.
- [LiteLLM mock requests](https://docs.litellm.ai/docs/completion/mock_requests): real SDK mock streaming is suitable for offline transport tests, but the installed Ollama model-info path additionally needs a fake metadata adapter. The final test blocks network connections and supplies both fake boundaries.
- [LiteLLM Ollama provider](https://docs.litellm.ai/docs/providers/ollama) and [official Ollama Linux instructions](https://ollama.com/download/linux): optional local service is a separate user choice; no service or model installation is performed automatically.
- [OpenAI tiktoken definitions](https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py) and [MIT license](https://github.com/openai/tiktoken/blob/main/LICENSE): exact cl100k/o200k URL cache keys and SHA256 values match the installed tokenizer definitions. An empty-cache SDK import attempted a download; the explicit installer now fetches/verifies those two tables before chat, rather than requiring hidden first-chat downloads.
- [fcntl in Python 3.11](https://docs.python.org/3.11/library/fcntl.html): Unix-only advisory locking. Windows tests do not establish native flock behavior.
- [Open Interpreter01 license](https://github.com/OpenInterpreter/01/blob/main/LICENSE): AGPL project attribution. The actual bundled 0.4.3 interpreter LICENSE and wheel source, not a newer upstream branch, determine its shipped license.

No unfamiliar native desktop API was added. Windows wrapper changes use existing relative script mechanisms; native execution is not claimed. The limited Linux entry reuses inspected local BrainRouter, ConversationArchive and KnowledgeStore signatures instead of importing the full server/profile/device runtime.

## Local-user setup follow-up

- Exact installed native versions checked: pywebview5.2, Typer0.12.5, MCP1.26.0, patched Open Interpreter0.4.3. Existing Python3.11 runtime metadata was read without importing devices. New native lock uses Uvicorn0.34.3 to satisfy MCP and Click8.1.7 because actual guarded --help reproduced the Click8.5 metavar incompatibility.
- [pip HTTPS certificates](https://pip.pypa.io/en/stable/topics/https-certificates/): pip before24.2 needs documented truststore opt-in to use OS roots. No insecure-host/TLS bypass used.
- [uv package installation](https://docs.astral.sh/uv/pip/packages/) and [system certificate support](https://docs.astral.sh/uv/concepts/authentication/certificates/): inspected installed uv0.13.8 flags, pinned that helper and used explicit venv selection, hash enforcement and system verification. It avoids this host's reproduced pip chunked-index transport failure.
- [pywebview documentation](https://pywebview.flowrl.com/guide/installation.html): Windows native renderer needs suitable Microsoft WebView/.NET components. Current docs include newer backends; no new pywebview API/backends were introduced. Existing5.2 native entry is reused, not inferred from current WinUI documentation.
- [Microsoft icacls](https://learn.microsoft.com/windows-server/administration/windows-commands/icacls): scoped inheritance/grant semantics. Current .NET WindowsIdentity.GetCurrent() reflection and installed icacls availability checked without changing real ACLs. Profile ACL application/native behavior remains untested on an actual fresh account.
