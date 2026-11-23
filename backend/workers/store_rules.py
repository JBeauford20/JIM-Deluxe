"""Store handling rules from the Home Depot store master ("Home Depot Store List", first sheet).

The store master is the business's list of how each store must be handled: do-not-ship
stores, SKU / variety restrictions, cross-dock routing and the merchandising partner. This
module is the single definition of what each restriction MEANS; the loader
(services/store_master.py) maps the sheet text to a rule code on stores.sku_rule, and the
engine, exports and UI all read the rule through here. Pure Python, no DB.

Rule shape (ipr_settings key `store_sku_rules` overrides/extends DEFAULT_RULES by code):
  label            text shown to people
  routine          False = the store gets NO routine replenishment at all
  directed_only    True = product ships only "when directed": the store is explicitly added
                   to a promotion, or an approved scenario names the store (Josh 2026-11-22)
  allow_only       routine replenishment is limited to SKUs matching ANY of:
                     families       skus.family values
                     programs       skus.program values
                     legacy_groups  skus.legacy_product_group values
                     ipr_families   ipr_family_map.ipr_family values
                     holiday        True = holiday / seasonal SKUs
  deny_genus       plant genera that may never be put on this store's carts (variety level:
                   HD SKUs are assortments, so the genus lives on the availability line)
  tag              short handling tag printed on the cart config / pack plan

Fail closed: a store carrying a rule code this module does not know is blocked from routine
replenishment until the rule is defined — a restriction is never silently ignored.
"""
from collections import defaultdict

DEFAULT_RULES = {
    "no_hedera": {
        "label": "No Hedera",
        "routine": True,
        "deny_genus": ["Hedera"],
        "tag": "NO HEDERA",
    },
    "littles_collectors_only": {
        "label": "Only 9cm Littles, Collectors, Rare Collectors, Holiday, or Promo",
        "routine": True,
        # Josh 2026-11-22: "9cm Littles" = the Littles program only — NOT Littles Ceramic and
        # NOT the 9cm hanging basket.
        "allow_only": {"programs": ["Littles"],
                       "legacy_groups": ["Collectors", "Rare Collectors"],
                       "holiday": True},
    },
    "holiday_only_directed": {
        "label": "Only Holiday when directed",
        "routine": False,
        "directed_only": True,
        "allow_only": {"holiday": True},
    },
}

# Sheet text (normalised) -> rule code. Matching is on the normalised text so punctuation /
# case / spacing differences in a future copy of the sheet still resolve.
_TEXT_TO_CODE = {
    "no hedera": "no_hedera",
    "only 9cm littles collectors rare collectors holiday or promo": "littles_collectors_only",
    "only holiday when directed": "holiday_only_directed",
}

HOLIDAY_IPR_FAMILIES = {"Holiday", "Seasonal/Holiday"}
UNKNOWN_RULE = {"label": "Unrecognised restriction — blocked until defined", "routine": False,
                "unknown": True}


def norm_text(v):
    """'Only 9cm Littles, Collectors, ...' -> 'only 9cm littles collectors ...'"""
    s = "".join(ch if ch.isalnum() else " " for ch in str(v or "").lower())
    return " ".join(s.split())


def rule_code_for_text(text):
    """Rule code for a 'SKU Restrictions' cell; None when blank; 'unknown:<text>' when the
    text is not a known restriction (the loader refuses those until they are defined)."""
    t = norm_text(text)
    if not t:
        return None
    return _TEXT_TO_CODE.get(t) or f"unknown:{t[:60]}"


def merged_rules(setting=None):
    """DEFAULT_RULES with the `store_sku_rules` setting layered on top (per rule code)."""
    out = {k: dict(v) for k, v in DEFAULT_RULES.items()}
    for code, rule in (setting or {}).items():
        if isinstance(rule, dict):
            out[code] = {**out.get(code, {}), **rule}
    return out


def rule_for(store, rules=None):
    """The handling rule for a store row (dict with 'sku_rule'), or None when unrestricted."""
    code = ((store or {}).get("sku_rule") or "").strip()
    if not code:
        return None
    rules = DEFAULT_RULES if rules is None else rules
    r = rules.get(code)
    return {**r, "code": code} if r else {**UNKNOWN_RULE, "code": code}


def is_holiday_sku(sku):
    sku = sku or {}
    return bool(sku.get("is_holiday")) or (sku.get("ipr_family") or "") in HOLIDAY_IPR_FAMILIES \
        or (sku.get("family") or "") == "Holiday"


def routine_blocked(rule):
    """True when the store gets no routine replenishment at all."""
    return bool(rule) and not rule.get("routine", True)


def sku_allowed(rule, sku, directed=False):
    """(allowed, reason) for one SKU at a store under `rule` in the engine.
    sku: dict with family / program / legacy_product_group / ipr_family / is_holiday.
    directed: an approved scenario explicitly names this store for this SKU — the only way a
    'when directed' store receives anything, and then only its allowed products."""
    if not rule:
        return True, None
    if rule.get("unknown"):
        return False, f"store rule '{rule.get('code')}' is not defined: no replenishment"
    if routine_blocked(rule) and not (directed and rule.get("directed_only")):
        return False, f"store rule '{rule.get('label', rule.get('code'))}': no routine replenishment"
    allow = rule.get("allow_only")
    if not allow:
        return True, None
    sku = sku or {}
    if (sku.get("family") or "") in (allow.get("families") or []):
        return True, None
    if (sku.get("program") or "") in (allow.get("programs") or []):
        return True, None
    if (sku.get("legacy_product_group") or "") in (allow.get("legacy_groups") or []):
        return True, None
    if (sku.get("ipr_family") or "") in (allow.get("ipr_families") or []):
        return True, None
    if allow.get("holiday") and is_holiday_sku(sku):
        return True, None
    return False, f"not in the store's allowed product list ({rule.get('label', rule.get('code'))})"


def denied_genera(rule):
    """Lower-cased plant genera that may not ship to a store under `rule`."""
    return frozenset(g.strip().lower() for g in ((rule or {}).get("deny_genus") or []) if g and g.strip())


def promo_eligible(rule, directed=False):
    """May the store be in a promotion / holiday program's scope?
    directed = the store was explicitly added to that program."""
    if not rule:
        return True
    if rule.get("unknown"):
        return False
    if rule.get("directed_only"):
        return bool(directed)
    return True


def handling_notes(store, rules=None):
    """Short routing / ops notes for a store: cross dock + restriction label."""
    out = []
    cd = ((store or {}).get("cross_dock") or "").strip()
    if cd:
        out.append(f"X-Dock: {cd}")
    r = rule_for(store, rules)
    if r:
        out.append(r.get("tag") or r.get("label") or r["code"])
    return "; ".join(out)


class VarietyPools:
    """Per-SKU supply split by plant genus, so a store that may not receive a genus is
    allocated only from the rest. Stores with no restriction draw the 'sensitive' genera
    (those some rule denies) first, which keeps unrestricted stock for restricted stores."""

    def __init__(self, sensitive=()):
        self.rem = defaultdict(lambda: defaultdict(int))     # sku -> genus -> units
        self.total = defaultdict(lambda: defaultdict(int))
        self.sensitive = {g.lower() for g in sensitive}

    def add(self, sku_id, genus, units):
        g = (genus or "").strip().lower()
        self.rem[sku_id][g] += int(units or 0)
        self.total[sku_id][g] += int(units or 0)

    def set_unlimited(self, sku_id, units):
        self.rem[sku_id] = defaultdict(int, {"": int(units)})
        self.total[sku_id] = defaultdict(int, {"": int(units)})

    def left(self, sku_id, deny=frozenset()):
        return sum(u for g, u in self.rem[sku_id].items() if g not in deny)

    def denied_supply(self, sku_id, deny):
        """Units of this SKU's ORIGINAL supply that a store with `deny` may not receive."""
        return sum(u for g, u in self.total[sku_id].items() if g in deny) if deny else 0

    def supply(self, sku_id, deny=frozenset()):
        return sum(u for g, u in self.total[sku_id].items() if g not in deny)

    def take(self, sku_id, units, deny=frozenset()):
        """Draw `units` (caller checked left()); sensitive genera first when permitted."""
        need = int(units)
        pool = self.rem[sku_id]
        for g in sorted((g for g in pool if g not in deny),
                        key=lambda g: (g not in self.sensitive, g)):
            if need <= 0:
                break
            t = min(need, pool[g])
            pool[g] -= t
            need -= t
        return int(units) - need

    def give_back(self, sku_id, units, deny=frozenset()):
        """Return units to the pool (order cut after allocation). Goes back to a genus the
        store could have drawn from; restricted-safe stock first."""
        if units <= 0:
            return
        pool, tot = self.rem[sku_id], self.total[sku_id]
        left = int(units)
        for g in sorted((g for g in tot if g not in deny), key=lambda g: (g in self.sensitive, g)):
            room = tot[g] - pool[g]
            t = min(left, room)
            pool[g] += t
            left -= t
            if left <= 0:
                break


def _fill_by_rank(want, pool, rank, distribute):
    """Draw `want` units from `pool` [[line_id, units]] a rank at a time (lowest rank first;
    lines of one rank share proportionally). rank: {line_id: number}; missing = last."""
    got, left = {}, int(want)
    for rk in sorted({rank.get(lid, 10 ** 6) for lid, _u in pool}):
        if left <= 0:
            break
        tier = [x for x in pool if rank.get(x[0], 10 ** 6) == rk]
        for lid, q in distribute(min(left, sum(u for _l, u in tier)), tier):
            got[lid] = got.get(lid, 0) + q
            left -= q
    return got


def assign_lines(total_by_group, lines, distribute, rank=None):
    """Split a SKU's shipped units across its availability lines so no group draws a line
    it may not receive.

    rank            optional {line_id: number} — lines with a lower number are used up first
                    (availability grade: grade 3, mature / contract-grower stock, goes before
                    grade 2). Never overrides a variety restriction.

    total_by_group  {deny_frozenset: units shipped to stores with that deny set}
    lines           [[line_id, units_available, genus_lower]]
    distribute      fn(total, [[line_id, units]]) -> [(line_id, qty)]  (largest remainder)
    Returns {deny_frozenset: {line_id: qty}}; a group's shortfall (no permitted line left)
    is simply absent — the caller writes those units without an availability line.
    Most-restricted groups reserve first so their permitted stock is never taken by others.
    """
    cap = {lid: int(u) for lid, u, _g in lines}
    genus = {lid: (g or "") for lid, _u, g in lines}
    out = {}
    for deny in sorted(total_by_group, key=lambda d: (-len(d), sorted(d))):
        want = int(total_by_group[deny] or 0)
        if want <= 0:
            out[deny] = {}
            continue
        ok = [[lid, cap[lid]] for lid, _u, _g in lines if cap[lid] > 0 and genus[lid] not in deny]
        if rank:
            # grade first; among lines of one grade, unrestricted stores still take the
            # varieties restricted stores cannot have before the ones they can
            rk = dict(rank)
            if not deny:
                for x in ok:
                    sens_ = genus[x[0]] and any(genus[x[0]] in d for d in total_by_group if d)
                    rk[x[0]] = rk.get(x[0], 10 ** 6) + (0 if sens_ else 0.5)
            got = _fill_by_rank(want, ok, rk, distribute)
        elif not deny:
            # unrestricted stores use up the sensitive lines first
            sens = [x for x in ok if genus[x[0]] and any(genus[x[0]] in d for d in total_by_group if d)]
            rest = [x for x in ok if x not in sens]
            got = {}
            s_take = min(want, sum(u for _l, u in sens))
            for lid, q in distribute(s_take, sens):
                got[lid] = got.get(lid, 0) + q
            for lid, q in distribute(want - s_take, rest):
                got[lid] = got.get(lid, 0) + q
        else:
            got = dict(distribute(want, ok))
        for lid, q in got.items():
            cap[lid] -= q
        out[deny] = got
    return out


def assign_sku_lines(by_group, lines, line_plant, distribute, rank=None):
    """assign_lines plus named-variety groups.

    by_group keys are either a deny frozenset (ordinary stores) or ("variety", frozenset of
    Aster codes) for a directed shipment of one named variety. Named varieties are served
    FIRST and only from lines whose plant code is in their set; what they take is removed
    from those lines before anyone else is assigned. Returns {group: {line_id: qty}}.
    line_plant: {line_id: Aster code}.
    """
    lines = [[lid, int(av), g] for lid, av, g in lines]
    out = {}
    for grp in sorted((g for g in by_group if isinstance(g, tuple)), key=lambda g: sorted(g[1])):
        pool = [[lid, av] for lid, av, _g in lines if av > 0 and line_plant.get(lid) in grp[1]]
        got = dict(distribute(int(by_group[grp] or 0), pool))
        for x in lines:
            x[1] -= got.get(x[0], 0)
        out[grp] = got
    rest = {g: u for g, u in by_group.items() if not isinstance(g, tuple)}
    out.update(assign_lines(rest, lines, distribute, rank=rank))
    return out
