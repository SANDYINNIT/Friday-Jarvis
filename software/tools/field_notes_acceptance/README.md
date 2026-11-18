# Field Notes isolated acceptance harness

`run.cjs` drives shipped UI assets against `bridge_fixture.py` and fake transports/stores. Its fake `/bridge-bootstrap.js` is injected by the harness only; it is not a production UI dependency. No saved screenshots, personal stores or previous machine-run results are included.

This retained harness currently expects an existing Windows Python environment and a particular Playwright browser layout under software/.venv. Those dependencies/browsers are **not** bundled or installed by the Linux text-mode installer. Running this harness on a fresh Windows/Linux machine requires a separate reviewed test setup; no browser acceptance was performed in this staging slice. Do not point it at personal stores or treat fixture activity as live runtime evidence.

Fresh packaging/Linux-entry evidence and exact commands are in ../../../packaging/VERIFICATION.md. The Linux installer supports no browser UI, native tray, microphone, speaker or desktop-control acceptance.
