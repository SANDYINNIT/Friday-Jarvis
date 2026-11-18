"""Bounded display-only motion; coordinates already validated by the overlay owner."""
import math


class BuddyMotion:
    def __init__(self):
        self.position = None
        self.binding = None
        self.seen = None
        self.phase = "following"
        self.started = 0

    def step(self, pointer, guide, binding, now, *, reduced=False):
        pointer = tuple(float(v) for v in pointer)
        if guide is not None and (not isinstance(guide, dict) or not isinstance(guide.get("key"),str)
                or any(type(guide.get(k)) not in (int,float) or not math.isfinite(guide[k])
                       or abs(guide[k]) > 1_000_000 for k in ("x","y"))):
            guide = None
        if self.position is None or self.binding != binding:
            self.position, self.binding = pointer, binding
            self.seen, self.phase = None, "following"
        key = guide.get("key") if guide else None
        if reduced:
            self.position = (float(guide["x"]), float(guide["y"])) if guide else pointer
            self.phase = "pointing" if guide else "following"
            self.seen = key
            return self.position
        if key is None and self.phase != "following":
            # Stop/expiry/new owner removes guidance immediately, not after a flight.
            self.phase, self.position = "following", pointer
        if key is not None and key != self.seen:
            self.seen, self.phase = key, "flying"
            self.origin, self.target = self.position, (float(guide["x"]), float(guide["y"]))
            self.started = now
            self.duration = min(1.2, max(.35, math.dist(self.origin, self.target)/800))
        if self.phase in ("flying", "returning"):
            if self.phase == "returning" and math.dist(pointer, self.return_pointer) > 100:
                self.phase, self.position = "following", pointer
            else:
                progress = min(1, max(0, (now-self.started)/self.duration))
                t = progress*progress*(3-2*progress)
                height = min(math.dist(self.origin, self.target)*.2, 80)
                midpoint = ((self.origin[0]+self.target[0])/2,
                            (self.origin[1]+self.target[1])/2-height)
                self.position = tuple((1-t)**2*a+2*(1-t)*t*b+t*t*c
                                      for a,b,c in zip(self.origin, midpoint, self.target))
                if progress == 1:
                    self.phase = "pointing" if self.phase == "flying" else "following"
                    self.started = now
        elif self.phase == "pointing" and now-self.started >= 2.5:
            self.phase, self.origin, self.target = "returning", self.position, pointer
            self.return_pointer, self.started, self.duration = pointer, now, .6
        elif self.phase == "following":
            self.position = tuple(a+(b-a)*.35 for a,b in zip(self.position,pointer))
        return self.position
