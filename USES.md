# Use Cases

Real, everyday ways to use FRIDAY. Some are built in today, some are what the assistant is climbing toward.

## The always-on assistant

The flagship use case. FRIDAY sits in the background, wakes on her word, and handles your request hands-free while you keep working.

- **"Open Discord."** She resolves the app, launches it, verifies it opened, and confirms.
- **"Take a screenshot, did it open?"** She captures the screen, reads it (OCR + vision), and answers truthfully — even when a Windows prompt blocks her view.
- **"Pause the music."** Media keys via her deterministic command router.
- **"Close the browser and shut the lid later."** Window control + scheduled tasks.

## Hands-free computer control

Because her brain authors and runs its own Python, the ceiling is whatever the computer can do:

- clicking buttons by reading the screen (screenshot → locate → click)
- typing, hotkeys, window focus/minimize/switching
- running PowerShell/Python, reading system state, managing processes
- browsing the web and summarizing live pages

## Personal memory & bookkeeping

- **"Remind me to call my mom at 8."** Safe text reminders (never execute commands on their own).
- **"Add a meeting tomorrow 10 to 11."** Lands in the HUD Calendar — what she announces is what the Calendar shows.
- **Memory.** Facts she learns ("My name is Sandy", "Minecraft is Java") are stored and recalled in later turns; lessons she learns from mistakes are injected live into her thinking every turn.

## Phone control (Telegram)

Message the assistant from your phone and get answers, confirmations, or live screenshots back — allowlisted so strangers get silence.

- **"What's on my screen?"** → she texts you the screenshot and describes it.
- **"Did my script finish?"** → real-world async status checks.

## Never interrupted, never run dry

Scenario failover that keeps the conversation flowing without drama:

- Free-tier rate limits hit → silently rotate to the next key/model (Groq → OpenRouter → Gemini → local), never a 6-hour ban, never a canned apology.
- Voice call in progress on Discord/WhatsApp/Telegram → FRIDAY stays completely quiet (`social_guard`).
- Whisper hallucination → rejected before it ever reaches the brain (`speech_filters`).
- Tool loop going in circles → detected and stopped (tool-cap loop guard).

## The student / maker's co-pilot

Something to script, debug, or research? FRIDAY is a private assistant for turning the computer into a tool — for school projects, home automation, learning to code, and everyday life.