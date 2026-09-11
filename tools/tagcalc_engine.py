"""Exact, batch-friendly optimizer for the supplied tagcalc scoring model.

States at the same redeem shop retain a Pareto frontier of Blueprint (BP) and
Brainstorm (BS) counts. Keeping only the largest total loses valid better
routes: a smaller total can have more BP left after selling BS. Every later
transition and final score is a positive linear function of BP and BS, so
componentwise dominance is safe. Probability and redeem formulas live in
tagcalc; this module changes how that model is searched, not its assumptions.
"""

from itertools import combinations_with_replacement

try:
    import tagcalc as model
except ImportError:
    from tools import tagcalc as model


class _State:
    __slots__ = ("T", "r", "BP", "BS", "previous", "pair_index", "shop", "k", "g", "M")

    def __init__(self, T, r, previous, pair_index, shop, k=0, g=0, M=0):
        self.T, self.r = T, r
        self.BP, self.BS = T * (1.0 - r), T * r
        self.previous, self.pair_index, self.shop = previous, pair_index, shop
        self.k, self.g, self.M = k, g, M


def _integer(value, name, first, last):
    if type(value) is not int or not first <= value <= last:
        raise ValueError("%s must be an integer from %d to %d" % (name, first, last))
    return value


def _tags(tags):
    result = []
    occupied = {}
    for tag in tags:
        if not isinstance(tag, dict):
            raise ValueError("Each tag must be an object")
        ante = _integer(tag.get("real_ante"), "Real tag ante", 1, 39)
        blind = _integer(tag.get("blind"), "Tag blind", 0, 1)
        kind = tag.get("kind")
        if kind not in ("neg", "rare"):
            raise ValueError("Tag kind must be neg or rare")
        if ante == 39:
            continue  # The user's RNG timeline ends at real Ante 38.
        key = (ante, blind)
        if key in occupied:
            if occupied[key] != kind:
                raise ValueError("A physical tag placement cannot be both Negative and Rare")
            continue
        occupied[key] = kind
        label = tag.get("label", "%s A%d%s" % (kind.upper(), ante, model.BLIND_SHORT[blind]))
        if not isinstance(label, str):
            raise ValueError("Tag label must be text")
        result.append({"real_ante": ante, "blind": blind, "kind": kind, "label": label})
    result.sort(key=lambda tag: (tag["real_ante"], tag["blind"], tag["kind"]))
    return result


def _baseline(first_copy_pos, last_ante):
    if not isinstance(first_copy_pos, (list, tuple)) or len(first_copy_pos) != 2:
        raise ValueError("First copy position must contain an ante and blind")
    ante = _integer(first_copy_pos[0], "First copy ante", 1, last_ante)
    blind = _integer(first_copy_pos[1], "First copy blind", 0, 2)
    return 3 * (ante - 1) + blind


def _pair_description(tags, coordinates, pair):
    a, b, _, _, shop = pair
    first, second = tags[a], tags[b]

    def description(index):
        tag = tags[index]
        return "%s A%d %s (real A%d)" % (
            tag["kind"].upper(), coordinates[index] // 3 + 1,
            model.BLIND_LONG[tag["blind"]], tag["real_ante"])

    return {
        "neg_label": first["label"] if first["kind"] == "neg" else second["label"],
        "rare_label": first["label"] if first["kind"] == "rare" else second["label"],
        "first_desc": description(a), "second_desc": description(b),
        "redeem_shop_desc": "A%d %s shop exit" % (shop // 3 + 1, model.BLIND_LONG[shop % 3]),
    }


def _path(final, tags, coordinates, pairs):
    source, pair_index, score, T, k, g, M, F = final
    chain = []
    current = source
    while current.previous is not None:
        chain.append(current)
        current = current.previous
    chain.reverse()
    result = []
    for state in chain:
        result.append(model.make_choice(
            state.previous.pair_index, state.pair_index,
            _pair_description(tags, coordinates, pairs[state.pair_index]),
            False, state.T, state.T, state.r, 0, state.k, state.g, state.M,
            1.0 - state.previous.r))
    result.append(model.make_choice(
        source.pair_index, pair_index, _pair_description(tags, coordinates, pairs[pair_index]),
        True, score, T, 0.5, F, k, g, M, 1.0 - source.r))
    return result


def _solve(tags, fillers, baseline, real_ante_max):
    # A blind's copy coordinate also identifies the tag offered before it.
    # A skipped second Small redeems at Big; a skipped Big redeems at Boss.
    coordinates = [3 * (tag["real_ante"] - 1 + sum(point <= tag["real_ante"] for point in fillers))
                   + tag["blind"] for tag in tags]
    pairs = [(a, b, coordinates[a], coordinates[b], coordinates[b] + 1)
             for a, tag in enumerate(tags) for b in range(a + 1, len(tags))
             if tag["kind"] != tags[b]["kind"]
             and tags[b]["real_ante"] <= real_ante_max]
    # All routes ending at the same shop have identical future choices, even
    # if their first tag differed. Share their frontier instead of retaining
    # dominated states separately for each pair.
    states_by_shop = {}
    initial = _State(1.0, 7.0 / 17.0, None, -1, baseline)
    best_score, final = -1.0, None
    copies = 3 * (real_ante_max + len(fillers))
    normal_multiplier = model.normal_redeem_multiplier
    final_multiplier = model.final_redeem_multiplier
    next_r = model.next_r_after_normal_redeem
    previous_first = None
    sources = []
    for pi, pair in enumerate(pairs):
        _, _, first, second, shop = pair
        g, F = second - first - 1, copies - shop
        if first != previous_first:
            sources = ([initial] if initial.shop < first else []) + sorted(
                (state for end, frontier in states_by_shop.items() if end < first for state in frontier),
                key=lambda state: state.pair_index)
            previous_first = first
        # Every state created in this first-tag group ends after `first`, so
        # it cannot become eligible until a later group. Reusing sources here
        # does not omit any valid transition.
        frontier = states_by_shop.get(shop, [])
        for source in sources:
            # Includes the preceding baseline/redeem shop's exit copy, just
            # like copy_count_between(events, last_shop_idx - 1, first_idx).
            k = first - source.shop
            M_final = final_multiplier(source.r, k, g)
            T_final = source.T * M_final
            score = T_final * F
            if M_final > 0 and score > best_score:
                best_score = score
                final = (source, pi, score, T_final, k, g, M_final, F)
            M = normal_multiplier(source.r, k, g)
            if M <= 0:
                continue
            T = source.T * M
            r = next_r(source.r, k, g, M)
            BP, BS = T * (1.0 - r), T * r
            if any(old.BP >= BP and old.BS >= BS for old in frontier):
                continue
            frontier = [old for old in frontier if old.BP > BP or old.BS > BS]
            frontier.append(_State(T, r, source, pi, shop, k, g, M))
        states_by_shop[shop] = frontier
    return best_score, (_path(final, tags, coordinates, pairs) if final else []), fillers


def optimize_for_fillers(tags, fillers, first_copy_pos, real_ante_min=1, real_ante_max=38):
    """Search every valid route for one fixed filler placement.

    The return value and route dictionaries match tagcalc.optimize_for_fillers.
    No redeem route returns (-1.0, [], fillers), as in the supplied script.
    """
    _integer(real_ante_min, "First real ante", 1, 38)
    _integer(real_ante_max, "Last real ante", real_ante_min, 38)
    fillers = tuple(sorted(_integer(point, "Filler insertion", 1, real_ante_max + 1)
                           for point in fillers))
    if len(fillers) > 2:
        raise ValueError("At most two filler antes are supported")
    baseline = _baseline(first_copy_pos, real_ante_max + len(fillers))
    return _solve(_tags(tags), fillers, baseline, real_ante_max)


def optimize(tags, first_copy_pos):
    """Return the best (score, path, fillers), searching both filler antes.

    Filler points in the same gap between recorded tag antes shift every tag
    identically and leave the final copy count unchanged. Only the earliest
    point in each such gap is needed; choosing it preserves the original
    earliest-filler tie break. Input and returned route objects are independent.
    """
    tags = _tags(tags)
    baseline = _baseline(first_copy_pos, 40)
    first_allowed = min(first_copy_pos[0] + 1, 39)
    points = sorted({first_allowed} | {
        tag["real_ante"] + 1 for tag in tags if tag["real_ante"] >= first_allowed})
    best = (-1.0, [], (first_allowed, first_allowed))
    for fillers in combinations_with_replacement(points, 2):
        candidate = _solve(tags, fillers, baseline, 38)
        if candidate[0] > best[0]:
            best = candidate
    return best
