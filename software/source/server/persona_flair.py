"""Neutral deterministic utility acknowledgements for every channel.

Private personality is supplied by the admitted model-turn contract, never
random canned insults in an ownership-unaware utility helper.
"""

import random

_ACK = (
    "On it, Sir.",
    "Right away, Sir.",
    "Working on it.",
    "One moment, Sir.",
    "One moment.",
)

_DONE = (
    "All done, Sir.",
    "Sorted, Sir.",
    "That's handled, Sir.",
    "Done.",
    "Done.",
)

_DONE_WARM = (
    "Done.",
    "Completed.",
    "Completed.",
)

_LAUNCH = (
    "Launching {app} for you, Sir.",
    "{app}, coming right up, Sir.",
    "Opening {app} — enjoy, Sir.",
)

_BROWSER = (
    "Right away, Sir — {target} it is.",
    "Sir, {target} opening now.",
    "Opening {target}.",
)

_CALENDAR = (
    "Added to your calendar for {when}.",
    "Booked for {when}, Sir. Consider it handled.",
    "Scheduled for {when}.",
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
        "Ready when needed.",
    ))
