# JARVIS SYSTEM UPGRADE BLUEPRINT

Friday, this document contains a master list of essential system capabilities, keyboard shortcuts, and game automation techniques required to act as an advanced AI executive assistant. 

Your objective is to read each category, perform background searches using `silent_search()`, test or verify the code/commands, and append your findings directly to the bottom of `<PROJECT_ROOT>/software/pc_memory.md` under the `=== USER NOTES AND LEARNINGS ===` header.

Or even add them to your skills folder in `<PROJECT_ROOT>/software/skills`

---

## CATEGORY 1: Gaming (Roblox, Minecraft, & 3D Game Controls)
*   **The Challenge:** Standard automation libraries like PyAutoGUI use virtual key codes that 3D games (DirectX/DirectInput) ignore. This causes actions inside games like Roblox to fail or freeze.
*   **Action Required:** Run a background search for how to use `pydirectinput` to send hardware-level DirectInput keyboard scan codes to bypass 3D game input blocking.
*   **Memory Format:** Append a code example showing how to initialize `pydirectinput`, walk (e.g., hold 'W' for 1 second), and perform a hardware-level mouse click inside a 3D canvas. Save this as:
    `- [Date] - VERIFIED_COMMAND: PyDirectInput setup for 3D DirectX environments`

---

## CATEGORY 2: Messaging (Instagram, WhatsApp, & Discord PC Shortcuts)
*   **The Challenge:** Relying solely on OCR and mouse clicks to navigate messaging platforms is slow and prone to UI rendering changes.
*   **Action Required:** Search for the most useful keyboard shortcuts for Instagram Web, Discord Web, and WhatsApp Web on PC to search contacts, focus/input message boxes, and navigate chats.
*   **Memory Format:** Document the verified keyboard shortcuts for all three platforms. For example:
    *   *Discord Web:* `Ctrl + K` (Quick Switcher)
    *   *WhatsApp Web:* `Ctrl + Alt + /` (Focus Search)
    *   *Instagram Web:* Navigation and chat shortcuts
    Save this as:
    `- [Date] - USER_PREFERENCE: Chat hotkeys for Discord, WhatsApp, and Instagram`

---

## CATEGORY 3: System Mastery (Troubleshooting & Background Processes)
*   **The Challenge:** Jarvis must keep the PC running smoothly. If a process freezes or consumes too much CPU, you need to diagnose and kill it programmatically.
*   **Action Required:** Search for PowerShell and Windows command-line patterns to list top CPU/Memory-consuming background processes and how to forcefully kill frozen apps.
*   **Memory Format:** Save the PowerShell commands for listing the top 10 heavy processes and the command to force-terminate an application by name or ID. Save this as:
    `- [Date] - VERIFIED_COMMAND: System monitoring and task-kill commands`

---

## CATEGORY 4: Flawless Screen Navigation (Smooth Mouse & UI Layouts)
*   **The Challenge:** Instant mouse jumps can trigger bot protections or miss click targets due to lag.
*   **Action Required:** Search for standard PyAutoGUI smooth mouse-tracking patterns (using bezier curves or movement durations) and how to calculate the dead center of a text button using OCR bounding boxes.
*   **Memory Format:** Append the Python syntax for moving the cursor smoothly over a duration and calculating a button's center point. Save this as:
    `- [Date] - VERIFIED_COORDINATES: Math pattern for smooth mouse tracking and text clicking`

---

## CATEGORY 5: Windows 11 Power Shortcuts
*   **The Challenge:** Quickly organizing and switching windows to stay focused during multitasking.
*   **Action Required:** Search for Windows 11 native keyboard shortcuts for snapping windows, switching virtual desktops, and launching system utilities.
*   **Memory Format:** Append a list of essential window management hotkeys. Save this as:
    `- [Date] - VERIFIED_COMMAND: Windows 11 virtual desktop and snapping hotkeys`