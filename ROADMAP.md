# Roadmap

Status of the FRIDAY personal assistant, in priority order. The core (voice loop + local brain + tool use) is **working**; the items below are the next things on the board.

## Core stability (ongoing)

- [ ] Real-world voice reliability under free-tier API limits (rate-limit rotation verified daily)
- [ ] Silence-detection, barge-in, and wake-word accuracy hardening
- [ ] Latency budget: wake word → spoken reply under 2 seconds locally

## Memory & learning

- [ ] Semantic/graph memory on top of the SQLite store (embedding layer exists, opt-in)
- [ ] Smarter lesson extraction — decide what becomes a long-term lesson vs. noise
- [ ] Persistent chat/conversation history in the HUD

## Model routing

- [ ] Full capability-based routing UI: GENERAL / VISION / CODING / DEEP_REASONING / FAST / EXTERNAL
- [ ] Codin and reasoning model slots beyond the current fast/general tiers
- [ ] Automatic second opinions via AI-to-AI delegation (boundary exists)

## Environments (opt-in adapters — built, not wired to the voice path yet)

- [ ] Home Assistant integration (REST/WebSocket adapter exists)
- [ ] MCP client runtime (isolated adapter exists)
- [ ] Agent coordinator for parallel delegation tasks (exists, not imported)

## Experimental

- [ ] LiveKit server mode (voice-enabled LiveKit meetings)
- [ ] Fully offline path (local coqui TTS / all-local brain) as a first-class profile
- [ ] Multi-device clients (Android/iOS app, ESP32) — inherited from the 01 base

---

Legend: checked = shipped. This list reflects the project's own priorities — protect the core, then add features that materially improve usefulness.