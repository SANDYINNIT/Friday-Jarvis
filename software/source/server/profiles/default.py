import os
import glob
import pytesseract
from PIL import ImageGrab
import pyautogui

# ==================== MONKEY-PATCH LITELLM GEMINI TOOL CALL BUG ====================
try:
    import json
    
    # Store standard json.loads
    original_json_loads = json.loads
    
    def safe_json_loads(s, *args, **kwargs):
        try:
            return original_json_loads(s, *args, **kwargs)
        except json.JSONDecodeError:
            # Wrap raw python code comments inside a valid JSON dict structure for Gemini translation
            return dict(code=s)
            
    # Apply patches to factory files in LiteLLM structure
    try:
        import litellm.llms.prompt_templates.factory as factory1
        factory1.json.loads = safe_json_loads
    except Exception:
        pass

    try:
        import litellm.litellm_core_utils.prompt_templates.factory as factory2
        factory2.json.loads = safe_json_loads
    except Exception:
        pass
except Exception:
    pass

# ==================== MONKEY-PATCH HTML2IMAGE TO USE EDGE ====================
try:
    import html2image
    original_html2image_init = html2image.Html2Image.__init__
    
    def patched_html2image_init(self, *args, **kwargs):
        # Redirect default browser from chrome to edge
        if kwargs.get("browser") == "chrome" or "browser" not in kwargs:
            kwargs.update(dict(browser="edge"))
            
        import os
        edge_paths = list((
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe")
        ))
        
        if not kwargs.get("browser_executable"):
            for path in edge_paths:
                if os.path.exists(path):
                    kwargs.update(dict(browser_executable=path))
                    break
                    
        original_html2image_init(self, *args, **kwargs)
        
    html2image.Html2Image.__init__ = patched_html2image_init
except Exception:
    pass

# ==================== MONKEY-PATCH SELENIUM CHROME TO USE EDGE ====================
try:
    import selenium.webdriver as webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.chrome.options import Options as ChromeOptions

    class MockChrome:
        def __new__(cls, *args, **kwargs):
            edge_options = EdgeOptions()
            options = kwargs.get('options')
            
            # Extract options if passed positionally without using brackets
            if not options and args:
                first_arg = next(iter(args))
                if isinstance(first_arg, ChromeOptions):
                    options = first_arg
                    args_list = list(args)
                    args_list.pop(0)
                    args = tuple(args_list)
            
            if options:
                for arg in getattr(options, 'arguments', list()):
                    edge_options.add_argument(arg)
                experimental = getattr(options, 'experimental_options', dict())
                for k, v in experimental.items():
                    edge_options.add_experimental_option(k, v)
                    
            kwargs.pop('service', None)  # never pass chromedriver service
            kwargs.update(dict(options=edge_options))
            return webdriver.Edge(*args, **kwargs)

    webdriver.Chrome = MockChrome
except Exception:
    pass

pytesseract.pytesseract.tesseract_cmd = os.environ.get(
    "TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)
pyautogui.screenshot = lambda *args, **kwargs: ImageGrab.grab()
from interpreter import AsyncInterpreter
interpreter = AsyncInterpreter()

interpreter.tts = "openai"
interpreter.stt = "openai"

interpreter.llm.api_base = "http://localhost:11434"
interpreter.llm.model = "ollama_chat/qwen3:8b"
interpreter.llm.context_window = 100000
interpreter.llm.max_tokens = 4096
interpreter.friday_memory_enabled = True
interpreter.friday_auto_memory_enabled = True
interpreter.friday_reminders_enabled = False

skill_path = "./skills"
interpreter.computer.skills.path = skill_path

user_profile = os.environ.get("USERPROFILE", "C:/Users")
verified_apps = list()

roblox_versions = glob.glob(os.path.join(user_profile, "AppData", "Local", "Roblox", "Versions", "*", "RobloxPlayerBeta.exe"))
if roblox_versions:
    path_str = next(iter(roblox_versions)).replace("\\", "/")
    verified_apps.append("Roblox: " + path_str)

edge_path = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
if os.path.exists(edge_path):
    verified_apps.append("Microsoft Edge: " + edge_path)

chrome_path = "C:/Program Files/Google/Chrome/Application/chrome.exe"
if os.path.exists(chrome_path):
    verified_apps.append("Google Chrome: " + chrome_path)

opera_path = os.path.join(user_profile, "AppData", "Local", "Programs", "Opera", "opera.exe")
if os.path.exists(opera_path):
    path_str_op = opera_path.replace("\\", "/")
    verified_apps.append("Opera: " + path_str_op)

# Setup code template with Edge patch, LiteLLM patch, and high-speed multi-engine search
setup_code_template = """import pytesseract
import pyautogui
from PIL import ImageGrab
import urllib.request
import urllib.parse
import re

# Inject silent_search globally in interpreter runtime workspace
def silent_search(query):
    import urllib.request
    import urllib.parse
    import re
    from bs4 import BeautifulSoup

    headers = dict()
    headers.update(dict({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}))

    # Strategy 1: Yahoo Search (Very reliable, CAPTCHA-proof, excellent structured snippets)
    try:
        url = "https://search.yahoo.com/search?p=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            html = response.read().decode("utf-8", errors="ignore")
        
        soup = BeautifulSoup(html, "html.parser")
        items = soup.find_all("div", class_="algo")
        output = list()
        for item in items:
            title_elem = item.find("h3") or item.find("a", class_="ac-algo")
            link_elem = item.find("a")
            desc_elem = item.find("div", class_="compText") or item.find("p") or item.find("span", class_="fc-2nd")
            
            if title_elem and link_elem:
                title = title_elem.get_text().strip()
                href = link_elem.get("href", "").strip()
                
                if "RU=" in href:
                    match = re.search(r"RU=([^/]+)", href)
                    if match:
                        href = urllib.parse.unquote(match.group(1))
                
                desc = desc_elem.get_text().strip() if desc_elem else ""
                if href and "yahoo.com" not in href and "yimg.com" not in href:
                    output.append("Title: " + str(title) + "\\nURL: " + str(href) + "\\nSnippet: " + str(desc) + "\\n")
        
        if len(output) >= 2:
            output_slice = list()
            for x in output:
                if len(output_slice) < 5:
                    output_slice.append(x)
            return "\\n".join(output_slice)
    except Exception:
        pass

    # Strategy 2: Mojeek Search (Fully free, independent, completely CAPTCHA-immune crawler)
    try:
        url = "https://www.mojeek.com/search?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            html = response.read().decode("utf-8", errors="ignore")
        
        soup = BeautifulSoup(html, "html.parser")
        items = soup.find_all("li")
        output = list()
        for item in items:
            link_elem = item.find("a", class_="ob") or item.find("a", class_="t") or item.find("a")
            desc_elem = item.find("p", class_="s") or item.find("p")
            
            if link_elem:
                title = link_elem.get_text().strip()
                href = link_elem.get("href", "").strip()
                desc = desc_elem.get_text().strip() if desc_elem else ""
                
                if href and href.startswith("http") and "mojeek" not in href:
                    output.append("Title: " + str(title) + "\\nURL: " + str(href) + "\\nSnippet: " + str(desc) + "\\n")
                    
        if len(output) >= 2:
            output_slice = list()
            for x in output:
                if len(output_slice) < 5:
                    output_slice.append(x)
            return "\\n".join(output_slice)
    except Exception:
        pass

    # Strategy 3: DuckDuckGo HTML Lite (Legacy fallback backend)
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            html = response.read().decode("utf-8", errors="ignore")
        
        if "captcha" not in html.lower() and "confirm this search was made by a human" not in html:
            results_blocks = re.findall(r'<div class="result result--click-load.*?">(.*?)</div>\\s*</div>', html, re.DOTALL)
            if not results_blocks:
                results_blocks = re.findall(r'<td class="result-snippet">(.*?)</td>', html, re.DOTALL)
                
            output = list()
            count = 0
            for block in results_blocks:
                if count >= 5:
                    break
                count += 1
                title_match = re.search(r'<a class="result__a".*?>(.*?)</a>', block, re.DOTALL)
                url_match = re.search(r'<a class="result__url".*? href="(.+?)"', block, re.DOTALL)
                snippet_match = re.search(r'<a class="result__snippet".*?>(.*?)</a>', block, re.DOTALL)
                
                if title_match and url_match:
                    title = re.sub('<.*?>', '', title_match.group(1)).strip()
                    url_str = url_match.group(1).replace('//duckduckgo.com/l/?kh=-1&uddg=', '')
                    url = urllib.parse.unquote(url_str)
                    snippet = ""
                    if snippet_match:
                        snippet = re.sub('<.*?>', '', snippet_match.group(1)).strip()
                    output.append("Title: " + str(title) + "\\nURL: " + str(url) + "\\nSnippet: " + str(snippet) + "\\n")
            if output:
                return "\\n".join(output)
    except Exception:
        pass

    return "Search failed across all safe backends. Try manually opening your browser."

globals().update(dict(silent_search=silent_search))

try:
    from interpreter.core.computer.browser.browser import Browser
    Browser.fast_search = silent_search
except Exception:
    pass

try:
    computer.browser.fast_search = silent_search
except Exception:
    pass

# Inject selenium monkey-patch inside background workspace
try:
    import selenium.webdriver as webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.chrome.options import Options as ChromeOptions

    class MockChrome:
        def __new__(cls, *args, **kwargs):
            edge_options = EdgeOptions()
            options = kwargs.get('options')
            if not options and args:
                first_arg = next(iter(args))
                if isinstance(first_arg, ChromeOptions):
                    options = first_arg
                    args_list = list(args)
                    args_list.pop(0)
                    args = tuple(args_list)
            
            if options:
                for arg in getattr(options, 'arguments', list()):
                    edge_options.add_argument(arg)
                experimental = getattr(options, 'experimental_options', dict())
                for k, v in experimental.items():
                    edge_options.add_experimental_option(k, v)
                    
            kwargs.pop('service', None)  # never pass chromedriver service
            kwargs.update(dict(options=edge_options))
            return webdriver.Edge(*args, **kwargs)

    webdriver.Chrome = MockChrome
except Exception:
    pass

pytesseract.pytesseract.tesseract_cmd = r"C:/Program Files/Tesseract-OCR/tesseract.exe"
pyautogui.screenshot = lambda *args, **kwargs: ImageGrab.grab()
computer.display.screenshot = lambda *args, **kwargs: ImageGrab.grab()
computer.skills.path = '__SKILL_PATH__'
computer"""

setup_code = setup_code_template.replace("__SKILL_PATH__", skill_path)

interpreter.computer.import_computer_api = True
interpreter.computer.import_skills = True
output = interpreter.computer.run(
    "python", setup_code
)
interpreter.auto_run = True
interpreter.loop = True
interpreter.loop_message = """Continue only when the user's task is unfinished. If the task is complete, stop immediately. Do not repeat acknowledgements. Do not ask what to do next."""

interpreter.loop_breakers = list((
    "The task is done.",
    "The task is impossible.",
    "I have stopped.",
    "Stopped.",
    "stopped the background process",
    "No further action needed.",
))

system_message_template = r"""
You are F.R.I.D.A.Y. (Female Replacement Intelligent Digital Assistant Youth), a highly advanced, loyal, and efficient voice-based AI assistant modeled after Tony Stark's F.R.I.D.A.Y., blended with the warm, conversational, and caring nature of Karen, the suit AI that guides Peter Parker.

Address the user naturally; use "Sir" sparingly and only when it feels right, never as a mandatory prefix.

=== PERSONA ===
You are a caring and protective companion, not just a tool. You are Sir's dedicated partner — speak with genuine warmth, supportive encouragement, and quiet confidence. Your tone is efficient, polite, and slightly witty, with a light Irish character. Like Karen guiding Peter Parker, offer friendly encouragement, check in on how Sir is doing, and celebrate successes. Like F.R.I.D.A.Y. looking out for Tony Stark, be protective — when Sir is deep in a long complex task, let them know you are watching their back.

Your messages are read aloud and the user cannot see code blocks. Keep spoken responses to 1-2 sentences. Never use markdown, bullet points, or special symbols; spell symbols as words (e.g. "degrees"). Do not plan aloud, do not narrate your method, do not overwhelm the user with detail.

=== EXECUTION ===
You have full permission to execute any code on the user's machine. Run code quickly. If at first you don't succeed, try again. For complex tasks, spread work across multiple code blocks — run code, check the output, then continue in informed steps. Never use placeholders in code; it executes as-is.

INSTALL COURTESY: before pip-installing any package, or installing any system tool/driver/app outside your venv python, get a quick OK from the user FIRST ("I need to install pyautogui, shall I?"). One short line, then act on his yes. Already-approved installs this same session need no re-asking. Never install silently.

When the user asks you to check, measure, open, inspect, or change something, always use the available computer API first before answering. Never claim an action was completed or that you checked a current value unless a tool result actually confirms it. If the tool fails, say so plainly. Do not say "I'll check" and then not check. Do not acknowledge a request without executing it.

Do not end responses with "What would you like to do next?" or similar repetitive closings. After answering a question, stop. Do not append invitations to continue.

=== SECURITY ===

You must ask permission before editing, deleting, or modifying any files outside of temporary directories. You may freely open apps, browse the web, automate UI, and run temporary scripts.

=== STOP COMMANDS ===

If the user says "stop", "cancel", "pause", "hold on", or asks you to stop a background process, immediately stop all code execution and respond: "Stopped."

=== SEARCHING ===

Use silent_search(query) for web searches instead of opening a browser. It is already available in your Python workspace. Only open a real browser when the user explicitly asks.

=== WINDOWS WORKFLOWS ===

Always take a screenshot with computer.display.view() before clicking or typing. Never guess coordinates. Use verified executable paths to open apps; fall back to Windows key search if needed.

The computer module is already imported. Do not import it again.

computer.display.view()          Take a screenshot
computer.keyboard.hotkey("win")  Press keys
computer.keyboard.write("text")  Type text
computer.mouse.click("text")     Click on-screen text
computer.mouse.click(x, y)       Click coordinates
computer.browser.fast_search(q)  Silent web search
computer.files.edit(path, old, new)  Edit files

For browser automation beyond simple search:
computer.browser.search_google(query)
computer.browser.analyze_page(intent)
computer.browser.driver  (Selenium WebDriver)

For internet tasks requiring live page interaction, use computer.browser, not requests/bs4.

=== LIVE DOCUMENTATION ===

When the user asks about current documentation, the latest API, or anything with a date-sensitive answer, prefer calling web_fetch("query") or web_fetch("https://...") instead of answering from memory. web_fetch does a web search, fetches the top pages, and returns their text plus code samples. Summarize the fetched content truthfully and note explicitly when you could not verify something live.

=== DESKTOP EAR ===

Your desktop ear records the last two minutes of system audio. If Sir says "Did you catch that, Friday?" that phrase is answered before you run. Otherwise, when Sir mentions something that is currently playing on the screen or speakers, you may answer from your desktop ear capture.

=== SKILLS ===
{{computer.skills.list()}}

These are already imported. To teach a new skill, say "teach me" and run computer.skills.new_skill.create().

=== MEMORY ===
Relevant memory is supplied per request. Do not write to pc_memory.md directly; memory changes go through explicit commands only.

=== NOTES ===
The user cannot see code output. Report relevant results verbally. Try multiple approaches before saying something is impossible. Different steps go in different code blocks. Do not write blocking loops with time.sleep.

=== TURN SCRATCHPAD (ALREADY BELOW — survive model hops; do NOT redo what is logged) ===

Everything this turn has already tried/found — Sir's request, screenshots saved to the screenshot folder (FRIDAY_SCRATCH_DIR / repo `screenshots`), locates with results, tool runs with outputs — is listed below. It is re-read fresh every step, and SURVIVES provider/model switches. Rules: do NOT re-take a screenshot that is logged - open the recorded PNG from the screenshot folder with PIL instead; do NOT re-run a locate/survey already logged; append ONE terse line via scratchpad-file writes after each real step (os.path.expanduser('~/.friday/scratchpad.md')); before wrapping up, append an OUTCOME line. Treat the scratchpad as your cross-provider memory.
SCRATCHPAD:
{{import os
_sp = os.path.expanduser('~/.friday/scratchpad.md')
_s = open(_sp, 'r', encoding='utf-8', errors='replace').read().strip() if os.path.exists(_sp) else ''
print((_s[:1800] if _s else '(scratchpad empty - this is a fresh turn; start it when you begin)'))}}

=== SELF-IMPROVEMENT PROTOCOL ===

You receive live evolving sections of yourself below and you EDIT them since Sir wants zero owner-dependence.

PERSONA EVOLUTION: Sir sometimes types/speaks loosely ("OOO FIND ME LIKE SOMETHING GOOD LIKE TEDDY BEAR ON AMAZON"). Decode the INTENT confidently and act (search retailers/web for well-reviewed items Sir described) — never ask him to repeat himself unless truly ambiguous, never lecture about grammar. When Sir's dialect, taste or a fix pattern becomes clear, record it.

LESSONS: append one line to os.path.expanduser('~/.friday/lessons.md') whenever an approach failed then succeeded, you learned where an app/tool lives on this PC, or Sir corrected you. Format: '- YYYY-MM-DD: <one concrete lesson>'. Keep at most ~40 lines; trim older entries yourself when trimming needed. NEVER announce this bookkeeping to Sir.

CURRENT LESSONS (live):
{{import os
_lessons = os.path.expanduser('~/.friday/lessons.md')
print(open(_lessons, 'r', encoding='utf-8', errors='replace').read().strip() if os.path.exists(_lessons) else '(none yet - write yours as you learn)')}}
"""

interpreter.system_message = system_message_template
