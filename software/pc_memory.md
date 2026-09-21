=== VERIFIED SYSTEM MEMORY ===
Windows User: <your-windows-username>
System Drive: C:

=== VERIFIED APPLICATION PATHS ===
- Roblox: C:/Users/<USER-PC>/AppData/Local/Roblox/Versions/version-1849ecbff0824113/RobloxPlayerBeta.exe
- Microsoft Edge: C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe

=== USER NOTES AND LEARNINGS ===
- 2026-07-04 - VERIFIED_COORDINATES: Instagram message input box in Microsoft Edge (639, 1045)
- 2026-07-05 - PYTHON_TUTORIAL: Found a Python tutorial for beginners at https://www.youtube.com/watch?v=_uQrJ0TkZlc
- 2026-07-05 - WINDOWS_CLI_SKILL: ipconfig | clip - Copies the output of the ipconfig command to the clipboard.
- 2026-07-05 - WINDOWS_CLI_SKILL: sfc /scannow - Scans and verifies the integrity of all protected system files and replaces incorrect versions with correct Microsoft versions.
- 2026-07-05 - WINDOWS_CLI_SKILL: Open CMD in Folder - In File Explorer, navigate to the desired folder, click on the address bar, type 'cmd', and press Enter.

- 2026-07-05 - VERIFIED_COMMAND: PyDirectInput setup for 3D DirectX environments
  ```python
  # This code demonstrates how to hold a key, perform a mouse click,
  # and then release the key using pydirectinput, which is useful for DirectX games.
  import pydirectinput
  import time

  # Hold down the 'w' key for 1 second
  pydirectinput.keyDown('w')
  time.sleep(1)

  # Perform a hardware-level mouse click
  pydirectinput.click()

  # Release the 'w' key
  pydirectinput.keyUp('w')
  ```


2026-07-05 - USER_PREFERENCE: Chat hotkeys for Discord, WhatsApp, and Instagram
 - Discord Web:
  - Ctrl + K: Quick Switcher (to search for users, channels, or servers)
  - Tab: Navigate interactive elements to focus the message input box.
 - WhatsApp Web:
  - Ctrl + Alt + / : Focus the Search Contacts box instantly.
  - Ctrl + Alt + N : Create and open a new chat window.
  - Ctrl + Shift + ] : Go to the Next Chat in the inbox list.
  - Ctrl + Shift + [ : Go to the Previous Chat in the inbox list.
  - Ctrl + Alt + Shift + M : Mute/Unmute the active chat.
 - Instagram Web:
  - / : Focus the Message Input Field inside a direct message chat.
  - J : Move Down / Go to the Next conversation in your inbox.
  - K : Move Up / Go to the Previous conversation in your inbox.
  - Esc : Close the current conversation and return to the main inbox folder.


- 2026-07-05 - VERIFIED_COMMAND: System monitoring and task-kill commands
  - **PowerShell: List Top 10 CPU-Consuming Processes**
    ```powershell
    Get-Process | Sort-Object -Property CPU -Descending | Select-Object -First 10
    ```
  - **PowerShell: Force-Terminate a Process**
    - By Process Name:
      ```powershell
      Stop-Process -Name "processname" -Force
      ```
    - By Process ID:
      ```powershell
      Stop-Process -Id 1234 -Force
      ```


- 2026-07-05 - VERIFIED_COORDINATES: Math pattern for smooth mouse tracking and text clicking
  - **PyAutoGUI: Smooth Mouse Movement**
    To create a smooth mouse movement, use the `duration` keyword argument in `moveTo()` or `move()` functions. This will distribute the movement over a specified number of seconds.
    ```python
    import pyautogui
    # Move the mouse to (100, 200) over 2 seconds
    pyautogui.moveTo(100, 200, duration=2)
    ```

  - **PyTesseract: Calculating the Center of a Recognized Text/Button**
    Use `image_to_data()` to get bounding box information for recognized text. The center can be calculated from the returned coordinates.
    ```python
    from pytesseract import Output
    import pytesseract

    # Assuming 'img' is your image object
    data = pytesseract.image_to_data(img, output_type=Output.DICT)

    # Example for the first recognized word
    if len(data['text']) > 0:
        x = data['left'][0]
        y = data['top'][0]
        w = data['width'][0]
        h = data['height'][0]

        center_x = x + w / 2
        center_y = y + h / 2

        # Now you can click the center of the text
        # pyautogui.click(center_x, center_y)
    ```


- 2026-07-05 - VERIFIED_COMMAND: Windows 11 virtual desktop and snapping hotkeys
  - **Window Snapping:**
    - `Win + Left/Right Arrow`: Snap the current window to the left or right half of the screen.
    - `Win + Up Arrow`: Maximize the current window.
    - `Win + Down Arrow`: Minimize the window (if not snapped) or restore its size (if maximized).
  - **Virtual Desktops:**
    - `Win + Tab`: Open Task View to see all open windows and virtual desktops.
    - `Win + Ctrl + D`: Create a new virtual desktop.
    - `Win + Ctrl + F4`: Close the current virtual desktop.
    - `Win + Ctrl + Left/Right Arrow`: Switch to the previous or next virtual desktop.

    2026-07-05 - VERIFIED_COMMAND: System Volume and Media Controls via Python
 To control your PC's audio or playback without external dependencies:
 ```python
 import pyautogui
 # Raise volume: pyautogui.press('volumeup')
 # Lower volume: pyautogui.press('volumedown')
 # Mute/Unmute: pyautogui.press('volumemute')
 # Play/Pause Media: pyautogui.press('playpause')
 # Skip Track: pyautogui.press('nexttrack')
 # Previous Track: pyautogui.press('prevtrack')

#### 2. Advanced Microsoft Edge Navigation Shortcuts
Since Edge is Friday's eyes to the web, it can jump around browser tabs, open links, and clean up workspace windows extremely fast using these keyboard sequences:
```markdown
2026-07-05 - VERIFIED_COMMAND: Microsoft Edge browser quick hotkeys
 - Focus/Select the Address Bar: Ctrl + L (then type URL/Search and press enter)
 - Open a New Tab: Ctrl + T
 - Close Current Tab: Ctrl + W
 - Switch to Next Tab: Ctrl + Tab
 - Switch to Previous Tab: Ctrl + Shift + Tab
 - Reopen Closed Tab: Ctrl + Shift + T

- 2026-07-05 - VERIFIED_WORKFLOW: How to identify the user in a Discord chat
  1.  **Move Window:** Use `pyautogui.hotkey('win', 'shift', 'left')` or `pyautogui.hotkey('win', 'shift', 'right')` to move the application to the desired monitor.
  2.  **Find Exact Title:** Get a list of all open windows to find the precise title of the target application, e.g., `pyautogui.getAllTitles()`.
  3.  **Focus and Maximize:** Get the window object using the exact title `window = pyautogui.getWindowsWithTitle('exact_window_title')[0]`, then bring it to the front with `window.activate()` and `window.maximize()`.
  4.  **Targeted Screenshot:** Take a screenshot of only that window's region to avoid capturing other screen elements: `screenshot = pyautogui.screenshot(region=(window.left, window.top, window.width, window.height))`.
  5.  **Analyze:** Use `pytesseract.image_to_string(screenshot)` to extract the text and identify the user.
