"""Adaptive: the tools each app is likely to reach for, learned from its own calls.

An adaptive group is hybrid -- the admin's defaults, search_tools, call_tool -- plus up to CAP
tools picked per KEY (one key = one app, so one user's habits never shape another's list). A
new key starts with none; a tool has to earn its place, keeps it while it is used, and loses it
when it falls out of use or something more likely needs the slot.

The score, per tool B, in the app's own call count (time only passes when the app works, so a
week away does not erase what it does):

    score(B) = slow(B) + fast(B) + sum over A of slow(A) * P(B follows A)

  slow, fast   every call adds 1 and everything else decays: a recency-frequency blend (CRF,
               Lee et al., SIGMETRICS 1999 -- LRU and LFU are the two ends of one spectrum) at
               two speeds -- habit (half-life SLOW_HALF calls) and what this stretch of work is
               about (FAST_HALF). Greenberg's command-history studies: most repeats are recent,
               but a large share are not, so neither end alone.
  chains       P(B follows A) is a first-order transition table that forgets: on A -> B the
               row of A keeps CHAIN of each entry and B gains 1 - CHAIN (IPAM, Davison & Hirsh
               1998, alpha 0.8 on UNIX command sequences). Weighted by how much A is used, it
               pulls in the tool that usually comes NEXT -- get_job after generate_image --
               even when that follower is rarely called on its own.

Membership has hysteresis, because every change to a client's tool list costs it the prompt
cache: in at ENTER, out below EXIT, and a full list only takes a newcomer that beats its weakest
member by MARGIN. One use scores 2.0 (slow + fast), so ENTER 2.5 means a tool needs a second
use before it is listed -- the list starts empty and fills as the app shows what it does.

Measured, not chosen (2026-10-17): the live history replayed -- 1,584 calls from six real apps --
asking before every call whether the tool was already in the list that ranking would show.

  ranking                                   hit, live   changes/call   hit, list frozen   at a server's
                                            (re-list)                  per session        first use (n=35)
  10 most recent (MRU)                        0.765        0.235          0.339            0.629
  10 most frequent (MFU)                      0.655        0.086          0.426            0.514
  one speed, half-life 40, chains             0.708        0.126          0.350            0.629
  two speeds + chains, in after one use       0.696        0.121          0.390            0.686
  two speeds + chains, in after two (this)    0.677        0.105          0.376            0.629

The first use of a server in a session is where a listed tool saves a search -- after it,
hybrid already lists that server's tools. Chains added 11-14 points at that moment in every
two-speed variant. Waiting for a second use costs two of those 35 and two points overall, and
is the point: a tool called once is not yet a habit. Close to the best on every measure while
changing the list less than half as often as MRU. Re-measure if the apps or the catalogue change.
"""

CAP = 10            # adaptive tools listed by default, on top of the admin's defaults (measured)
MAX_CAP = 50        # a group may list more: more of each app's habits, more tokens in every request
SLOW_HALF = 160     # calls: habit
FAST_HALF = 20      # calls: the current stretch of work
CHAIN = 0.8         # IPAM's alpha: the share of a transition row that survives one new transition
ENTER, EXIT, MARGIN = 2.5, 1.0, 0.2
HISTORY = 2000      # calls replayed per key at start: older ones weigh < 0.02% at SLOW_HALF
_SLOW, _FAST = 0.5 ** (1 / SLOW_HALF), 0.5 ** (1 / FAST_HALF)
_DUST = 1e-3        # weights below this are dropped: they can never reach EXIT again on their own


class Model:
    """One app's habits. Tools are (upstream, tool) -- the upstream's own names, which survive
    a collision renaming what clients call them."""

    __slots__ = ("slow", "fast", "chain", "last", "picks")

    def __init__(self):
        self.slow, self.fast, self.chain = {}, {}, {}
        self.last = None
        self.picks = []          # in, in the order they earned it: stable for the client's list

    def observe(self, tool, exclude=(), cap=CAP) -> bool:
        """One call. True if the picks changed."""
        for weights, keep in ((self.slow, _SLOW), (self.fast, _FAST)):
            for t in weights:
                weights[t] *= keep
            weights[tool] = weights.get(tool, 0.0) + 1.0
        if self.last is not None and self.last != tool:
            row = self.chain.setdefault(self.last, {})
            for t in row:
                row[t] *= CHAIN
            row[tool] = row.get(tool, 0.0) + (1 - CHAIN)
        self.last = tool
        self._sweep()
        return self.reselect(exclude, cap)

    def scores(self) -> dict:
        s = {t: w + self.fast.get(t, 0.0) for t, w in self.slow.items()}
        for a, wa in self.slow.items():
            for b, p in self.chain.get(a, {}).items():
                s[b] = s.get(b, 0.0) + wa * p
        return s

    def reselect(self, exclude=(), cap=CAP) -> bool:
        """Hysteresis, so the list does not flicker: members stay while they score EXIT; a
        free slot takes anything at ENTER; a full list swaps its weakest member only for a
        newcomer at least MARGIN better. Excluded tools (the group's own defaults, already
        listed) never take a slot. A cap lowered since the last call drops the weakest first."""
        s = self.scores()
        keep = [t for t in self.picks if t not in exclude and s.get(t, 0.0) >= EXIT]
        while len(keep) > cap:
            keep.remove(min(keep, key=lambda t: s.get(t, 0.0)))
        for c in sorted((t for t in s if t not in keep and t not in exclude and s[t] >= ENTER),
                        key=lambda t: (-s[t], t)):
            if len(keep) < cap:
                keep.append(c)
                continue
            weakest = min(keep, key=lambda t: s.get(t, 0.0))
            if s[c] > s.get(weakest, 0.0) * (1 + MARGIN):
                keep[keep.index(weakest)] = c          # the slot, not the end: the order holds
        changed = keep != self.picks
        self.picks = keep
        return changed

    def _sweep(self):
        for weights in (self.slow, self.fast):
            for t in [t for t, w in weights.items() if w < _DUST]:
                del weights[t]
        for a in list(self.chain):
            row = self.chain[a]
            for t in [t for t, p in row.items() if p < _DUST]:
                del row[t]
            if not row or a not in self.slow:
                del self.chain[a]


class Adaptive:
    """Every key's Model. Learning runs for every key whatever its group's mode, so switching a
    group to adaptive starts from what its apps already do -- a key with no calls starts empty."""

    def __init__(self):
        self.models = {}

    def observe(self, key_id, tool, exclude=(), cap=CAP) -> bool:
        if key_id is None:
            return False
        return self.models.setdefault(key_id, Model()).observe(tool, exclude, cap)

    def recap(self, key_id, exclude=(), cap=CAP) -> bool:
        """The key's group changed its cap, its defaults or the key moved: pick again now, not
        at the next call."""
        m = self.models.get(key_id)
        return m.reselect(exclude, cap) if m else False

    def picks(self, key_id, exclude=()) -> list:
        m = self.models.get(key_id)
        return [t for t in m.picks if t not in exclude] if m else []

    def ranked(self, key_id, n=CAP + 5) -> list:
        """(tool, score, picked), best first -- for the settings page: what is listed, and what
        is next in line."""
        m = self.models.get(key_id)
        if not m:
            return []
        s = m.scores()
        return [(t, round(v, 2), t in m.picks) for t, v in sorted(s.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

    def forget(self, key_id):
        self.models.pop(key_id, None)

    def rebuild(self, rows, exclude_for=lambda key_id: (), cap_for=lambda key_id: CAP):
        """From the events log, oldest first: (key_id, upstream, tool). The log is the memory --
        nothing else is stored, so a restart relearns exactly what it knew."""
        self.models = {}
        for key_id, upstream, tool in rows:
            self.observe(key_id, (upstream, tool), exclude_for(key_id), cap_for(key_id))
