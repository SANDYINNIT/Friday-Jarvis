# Context

This document states the principles the FRIDAY project is built around. They are the reason FRIDAY looks the way she does — read this before changing direction.

## 1. Be minimal

Minimal for **developers** (the codebase should be approachable, with one obvious place for each thing) and minimal for **the user** (talk to FRIDAY the way you'd talk to a person — no menus, no setup rituals, no configuration tours).

## 2. Develop standards

Where knowledge already exists in the wider ecosystem and is compatible — Open Interpreter's tool model, Ollama/OpenAI API conventions, standard voice pipelines — FRIDAY builds on it instead of reinventing it. Standards are borrowed, not invented.

## 3. Resonate strongly with a niche

FRIDAY's niche is the **personal computer assistant**: one machine, one owner, real computer control (apps, windows, screen, files, the web), always listening in the background. She is not trying to be a general chatbot or a cloud product — she lives on a single Windows PC.

## 4. Be affordable

The ambition of the *01* movement this is derived from: the most hackable assistant should be under $100, ideally under $70. FRIDAY follows the same spirit — the only license costs are the free tiers of optional cloud models (Groq, OpenRouter, Gemini, Deepgram). With a local brain (`qwen3:8b` via Ollama) she costs nothing to run.

## 5. Be open-source

Everything here is AGPL-3.0-licensed and meant to be forked, learned from, and improved (see `LICENSE`). Real API keys can't be (and aren't) part of the repository.

## Working rules (in force for every change)

- **Forward-only failover.** When a provider errors or rate-limits, move to the next account/model. Never burn the user's request, never retry into a 6-hour ban, and end with a truthful result — not a canned apology.
- **Protect the core.** The wake-word → STT → brain → tools → TTS loop must stay fast and reliable. Features are built on top of a healthy core, never at its expense.
- **The brain authors the code.** App open/close/click flows go through Open Interpreter (the model writes and runs its own scripts), not through hard-coded trigger phrases. Deterministic routing stays deliberately small.
- **Say what's true.** Tool output, failed screenshots, and internal reasoning are never spoken as if they were assistant thoughts.
- **Never leak secrets.** Credentials never reach logs or transcripts (see `redaction.py`).

## Where this came from

FRIDAY is built on the **01** — an open-source platform for intelligent devices by Open Interpreter, inspired by the Rabbit R1 and the Star Trek computer. This repository is a heavily customized, Windows-focused, voice-first personal assistant built out of that base.