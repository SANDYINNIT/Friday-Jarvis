# Licensing and attribution

## Project source

The retained project license is GNU Affero General Public License version 3: [LICENSE](LICENSE), also retained at `software/LICENSE`. Original Open Interpreter01 attribution and source headers are preserved. The upstream project is [OpenInterpreter/01](https://github.com/OpenInterpreter/01). This staging task does not relicense upstream code or claim original authorship of it.

## Included patched interpreter

`software/tmp-oi/open_interpreter-0.4.3-py3-none-any.whl` is the project's existing patched Open Interpreter 0.4.3 distribution. Its actual bundled license is AGPL-3.0, regardless of a newer upstream version's license. The full included license is [LICENSES/Open-Interpreter-AGPL-3.0.txt](LICENSES/Open-Interpreter-AGPL-3.0.txt). Corresponding Python source, metadata, original headers and the FRIDAY execution patches are extracted beside it at `software/vendor/open-interpreter/`; no compiled native modules are in this wheel. Upstream: [OpenInterpreter/open-interpreter](https://github.com/OpenInterpreter/open-interpreter). Preserve these notices and corresponding-source access when distributing or operating a covered network service.

## Artwork and UI

The included green F SVG/tray identity and simple companion assets are project source assets distributed with the project license. Existing source headers and attribution are preserved. Legacy Town sprite sheets, tiles, their dependent visual page and the generator that reproduces them are excluded because their notice reserves redistribution rights. No license is granted for those excluded assets. No study-repository assets, commercial fonts, movie media, personal captures or model weights are bundled. Provider and product names identify integrations and imply no endorsement.

## Installed dependencies

Linux dependencies are installed from PyPI into a local virtual environment, not vendored into this Git tree. `requirements-linux.lock` pins the entire selected closure and hashes. Their own licenses remain in installed distribution metadata; consult them before redistribution of binaries/environments. Primary direct libraries: [LiteLLM](https://github.com/BerriAI/litellm) and [Requests](https://requests.readthedocs.io/). Optional Windows dependency names in the legacy Poetry file are references, not redistributed native binaries or a tested Linux install.

The installer fetches two OpenAI public tokenizer tables using the fixed SHA256 values from [tiktoken's official definitions](https://github.com/openai/tiktoken/blob/main/tiktoken_ext/openai_public.py). They remain inside the ignored virtual environment, not this checkout's distributed assets. [tiktoken is MIT licensed](https://github.com/openai/tiktoken/blob/main/LICENSE), copyright OpenAI/Shantanu Jain; its installed package includes that license. No training or embedding weights are downloaded.

See [publication status](PUBLICATION_REVIEW.md) for remote history cleanup and platform acceptance limits.

## Local setup follow-up

The included interpreter source/wheel adds a FRIDAY guard refusing legacy image-tool model calls when explicit user routes are selected. Its RECORD and the corresponding extracted source are updated together; AGPL notices remain. Windows installs native dependencies from a separate full hash lock and a pinned uv installer helper (upstream dual MIT/Apache-2.0), not binaries checked into this tree. Dependency licenses remain in their installed distributions. No model weights, proprietary OS runtimes or private config are redistributed.

## Excluded Town artwork
Files under software/source/server/ui/art/, their generator and the dependent Living Town visual page are excluded from this public working tree because redistribution rights were not established. The live installation is untouched. Historical Git objects are still not approved for distribution. No new rights are asserted over excluded material.
