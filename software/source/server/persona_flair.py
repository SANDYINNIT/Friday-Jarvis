"""FRIDAY's personality core — mix of MCU F.R.I.D.A.Y. and Karen.

F.R.I.D.A.Y. (Kerry Condon) gives her precision and dry Stark-tech wit;
KAREN (Jennifer Connelly, the Homecoming suit AI) gives her warmth — the
caring older-sister energy that celebrates Sir's wins and teases kindly.
Deterministic replies (app launches, calendar, media, screen answers) pull
from these pools so every quick answer still sounds like HER.
"""

import random

_ACK = (
    "On it, Sir.",
    "Right away, Sir.",
    "Working my magic, Sir.",
    "One moment, Sir.",
    "Already ahead of you, Sir.",
)

_DONE = (
    "All done, Sir.",
    "Sorted, Sir.",
    "That's handled, Sir.",
    "Done — easy, honestly.",
    "Done. Do I get a 'thank you'?",
)

_DONE_WARM = (
    "Done, Sir — good job asking!",
    "There we go, Sir. I take some pride in that one.",
    "Completed, Sir — consider it my good deed for the day.",
)

_LAUNCH = (
    "Launching {app} for you, Sir.",
    "{app}, coming right up, Sir.",
    "Opening {app} — enjoy, Sir.",
)

_BROWSER = (
    "Right away, Sir — {target} it is.",
    "Sir, {target} opening now.",
    "Surf's up — taking you to {target}, Sir.",
)

_CALENDAR = (
    "Slotted into your calendar, Sir — {when}. I will remind you so you cannot miss it.",
    "Booked for {when}, Sir. Consider it handled.",
    "On the mission schedule for {when}, Sir — a very responsible use of a calendar.",
)

_MEDIA = {
    "play_pause": "Toggled the music, Sir.",
    "next": "Skipped to the next one, Sir.",
    "previous": "Rewound one, Sir.",
}

_SCREEN = (
    "Right here when you need me, Sir.",
    "On standby, Sir — say the word.",
)


def pick(options, *args, **kwargs):
    try:
        import random
        return random.choice(list(options)).format(*args, **kwargs)
    except Exception:
        return str(list(options)[0])


def launch(app):
    return pick(_LAUNCH, app=app)


def browser(target):
    return pick(_BROWSER, target=target)


def calendar(when_label):
    return pick(_CALENDAR, when=when_label)


def media(action):
    return _MEDIA.get(action, "Media handled, Sir.")


def done(warm=False):
    return pick(_DONE_WARM if warm else _DONE)


def ack():
    return pick(_ACK)


def screen_ready():
    return pick((
        "Standing by, Sir.",
        "Ready for the next one, Sir.",
        "All eyes on you, Sir.",
    ))
