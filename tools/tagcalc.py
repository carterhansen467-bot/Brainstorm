#!/usr/bin/env python3
"""
tagcalc.py
Balatro Wraith / Negative+Rare Redeem Optimizer

Adapted from the tester-provided tagcalc (1).py.
Batch CLI: --input seeds.csv --output-dir results --top 1000.
Use --second-tag A4S/A4B to derive the first-copy shop automatically.
The scoring assumptions below are preserved; the search is exact.

What this does
--------------
You enter:
  1) The first baseline Wraith-copy shop exit, e.g. a5boss
  2) Future Negative/Rare tag placements, e.g.
       r7b, n10b, r12s, r15s, r16s, r28s, n31s

The script tries:
  - every possible placement of the two filler/voucher antes
  - every valid Negative/Rare redeem pairing
  - every choice of which pairs to use
  - every possible final redeem choice

It outputs:
  - best filler ante placement
  - chosen redeem pairs
  - k = full BP+BS copies before selling BS
  - g = BP-only gap copies after selling BS
  - multiplier per redeem
  - final endgame spectral count relative to baseline BP+BS

Timing model
------------
- A copy occurs when leaving a shop.
- Baseline starts at the first Wraith-copy shop exit you enter.
- Before the first skip of a redeem, BS are still present, so copies are worth BP+BS.
- After leaving the shop before the first skip, BS are sold.
- Gap copies before redeem are BP-only.
- After the second required tag, you beat the next blind, enter shop, buy BP/BS,
  and those BP/BS are active for the copy when leaving that same shop.
- Taking a skip removes that blind's shop exit/copy.

Tag shorthand
-------------
  r7b    = rare ante 7 big
  n10b   = negative ante 10 big
  r12s   = rare ante 12 small
  rare a8sb
  neg a9bb
  negative a14 small

Filler ante meaning
-------------------
The real RNG timeline has 38 real antes.
Two filler antes can be inserted anywhere.

A filler placement "before real ante 32" means all real ante 32+ tag positions
shift one effective ante later.

A filler placement "after real ante 38" means it only adds time at the end.

Defaults
--------
- start effective ante = first baseline copy ante + 1 after baseline copy position
  is handled internally by absolute copy indexing.
- real ante search range = 1..38
- two filler insertions can be before any real ante 1..38, or after real ante 38.
- only the single best unique route is printed by default.
"""

import itertools
import re


# ----------------------------
# Constants / probabilities
# ----------------------------

RARE_POOL_SIZE = 9.0

P_BP_FROM_RARE = 1.0 / RARE_POOL_SIZE
P_BS_FROM_RARE = 1.0 / RARE_POOL_SIZE

P_BS_ETERNAL = 0.30
P_RARE_EDITION_WASTE = 0.037
P_RARE_NAT_NEG = 0.003

NORMAL_BP_SHARE = 10.0 / 17.0
NORMAL_BS_SHARE = 7.0 / 17.0

FINAL_BP_SHARE = 0.5
FINAL_BS_SHARE = 0.5


def effective_cost_per_joker(normal):
    """
    Effective Double Tags needed per usable BP/BS.

    Normal redeem:
      target = BP OR non-eternal BS
    Final redeem:
      target = BP OR BS, eternal allowed

    Rare edition waste:
      rare tag wasted, negative tag not wasted

    Natural negative:
      rare tag still creates a negative rare, negative tag not consumed

    Therefore:
      rare tags per usable =
          1 / (target_probability * (1 - edition_waste))

      negative tags consumed per rare tag attempt =
          1 - edition_waste - natural_negative

      negative tags per usable =
          above / (target_probability * (1 - edition_waste))

      total double tags per usable =
          rare tags per usable + negative tags per usable
    """
    if normal:
        p_target = P_BP_FROM_RARE + P_BS_FROM_RARE * (1.0 - P_BS_ETERNAL)
    else:
        p_target = P_BP_FROM_RARE + P_BS_FROM_RARE

    p_usable_per_rare_tag = p_target * (1.0 - P_RARE_EDITION_WASTE)
    rare_tags_per_usable = 1.0 / p_usable_per_rare_tag

    p_negative_tag_consumed = 1.0 - P_RARE_EDITION_WASTE - P_RARE_NAT_NEG
    negative_tags_per_usable = p_negative_tag_consumed / p_usable_per_rare_tag

    return rare_tags_per_usable + negative_tags_per_usable


C_NORMAL = effective_cost_per_joker(True)
C_FINAL = effective_cost_per_joker(False)


# ----------------------------
# Parsing
# ----------------------------

BLIND_INDEX = {
    "s": 0, "sb": 0, "small": 0,
    "b": 1, "bb": 1, "big": 1,
    "boss": 2, "bo": 2
}

BLIND_SHORT = {
    0: "SB",
    1: "BB",
    2: "Boss"
}

BLIND_LONG = {
    0: "Small",
    1: "Big",
    2: "Boss"
}


def parse_blind_text(text):
    t = text.strip().lower()
    if t not in BLIND_INDEX:
        raise ValueError("Unknown blind: " + text)
    return BLIND_INDEX[t]


def parse_shop_exit(text):
    """
    Parses shop exit positions like:
      a5boss
      a6sb
      a6bb
      5boss
    """
    raw = text.strip().lower().replace(" ", "")
    m = re.match(r"^a?(\d+)(sb|bb|small|big|boss|s|b)$", raw)
    if not m:
        raise ValueError("Could not parse shop exit position: %r" % text)
    ante = int(m.group(1))
    blind = parse_blind_text(m.group(2))
    return (ante, blind)


def parse_tag_token(token):
    """
    Parses:
      r7b
      n10b
      rare a8sb
      neg a9bb
      negative a14 small
    Returns dict with kind, real_ante, blind, label.
    """
    raw = token.strip().lower()
    raw = raw.replace(":", " ")
    raw = re.sub(r"\s+", " ", raw)

    # Compact form: r7b, n10bb, rare8sb if someone types it.
    m = re.match(r"^(r|rare|n|neg|negative)\s*a?(\d+)\s*(sb|bb|small|big|s|b)$", raw.replace(" ", ""))
    if m:
        kind_raw = m.group(1)
        ante = int(m.group(2))
        blind_raw = m.group(3)
    else:
        parts = raw.split()
        if len(parts) < 2:
            raise ValueError("Could not parse tag token: %r" % token)

        kind_raw = parts[0]
        if kind_raw not in ("r", "rare", "n", "neg", "negative"):
            raise ValueError("Tag must start with rare/r or neg/n: %r" % token)

        rest = "".join(parts[1:])
        m2 = re.match(r"^a?(\d+)(sb|bb|small|big|s|b)$", rest)
        if m2:
            ante = int(m2.group(1))
            blind_raw = m2.group(2)
        elif len(parts) >= 3:
            ante_text = parts[1].replace("a", "")
            if not ante_text.isdigit():
                raise ValueError("Could not parse ante in: %r" % token)
            ante = int(ante_text)
            blind_raw = parts[2]
        else:
            raise ValueError("Could not parse tag token: %r" % token)

    if kind_raw in ("r", "rare"):
        kind = "rare"
    else:
        kind = "neg"

    blind = parse_blind_text(blind_raw)
    if blind == 2:
        raise ValueError("Tags cannot be on Boss blind: %r" % token)

    label = ("%s A%d%s" % (kind.upper(), ante, BLIND_SHORT[blind]))
    return {
        "kind": kind,
        "real_ante": ante,
        "blind": blind,
        "label": label,
    }


def parse_tags(text):
    """
    Parses a comma/semicolon/newline separated list. Also handles space-separated
    compact tokens like:
      r7b n10b r12s
    """
    if text is None:
        return []

    text = text.strip()
    if not text:
        return []

    if "," in text or ";" in text or "\n" in text:
        chunks = [x.strip() for x in re.split(r"[,\n;]+", text) if x.strip()]
    else:
        words = text.split()
        chunks = []
        i = 0
        while i < len(words):
            w = words[i].lower()
            # compact token like r7b
            if re.match(r"^(r|rare|n|neg|negative)a?\d+(sb|bb|small|big|s|b)$", w):
                chunks.append(words[i])
                i += 1
            elif w in ("r", "rare", "n", "neg", "negative"):
                if i + 1 >= len(words):
                    raise ValueError("Dangling tag kind at end of tag list")
                # rare a8sb OR rare a8 sb
                if i + 2 < len(words) and re.match(r"^a?\d+$", words[i + 1].lower()):
                    chunks.append(words[i] + " " + words[i + 1] + " " + words[i + 2])
                    i += 3
                else:
                    chunks.append(words[i] + " " + words[i + 1])
                    i += 2
            else:
                raise ValueError("Could not parse near token: %r" % words[i])

    tags = [parse_tag_token(c) for c in chunks]
    tags.sort(key=lambda t: (t["real_ante"], t["blind"], t["kind"]))
    return tags


# ----------------------------
# Timeline
# ----------------------------

def shift_real_ante(real_ante, fillers):
    """
    fillers are insertion points p:
      p <= 38 means insert before real ante p
      p = 39 means after real ante 38

    Effective ante for a real ante shifts by number of filler points <= real_ante.
    """
    shift = 0
    for p in fillers:
        if p <= real_ante:
            shift += 1
    return real_ante + shift


def pos_key(ante, blind):
    return ante * 3 + blind


def make_event(kind, ante, blind, tag_kind="", tag_label="", real_ante=None):
    return {
        "kind": kind,          # "copy" or "tag"
        "ante": ante,
        "blind": blind,
        "tag_kind": tag_kind,
        "tag_label": tag_label,
        "real_ante": real_ante,
    }


def event_desc(e):
    if e["kind"] == "tag":
        return "%s A%d %s (real A%d)" % (
            e["tag_kind"].upper(),
            e["ante"],
            BLIND_LONG[e["blind"]],
            e["real_ante"],
        )
    return "A%d %s shop exit" % (e["ante"], BLIND_LONG[e["blind"]])


def build_events(tags, fillers, effective_start_ante, effective_end_ante):
    """
    Build event list with tag events before each blind's copy event.
    Taking a tag skips that blind, so the copy event for that blind is removed.
    """
    shifted_tags = []
    for t in tags:
        eff_ante = shift_real_ante(t["real_ante"], fillers)
        shifted_tags.append({
            "kind": t["kind"],
            "real_ante": t["real_ante"],
            "ante": eff_ante,
            "blind": t["blind"],
            "label": t["label"],
        })

    events = []
    for ante in range(effective_start_ante, effective_end_ante + 1):
        for blind in (0, 1, 2):
            if blind in (0, 1):
                for t in shifted_tags:
                    if t["ante"] == ante and t["blind"] == blind:
                        events.append(make_event(
                            "tag", ante, blind,
                            tag_kind=t["kind"],
                            tag_label=t["label"],
                            real_ante=t["real_ante"],
                        ))
            events.append(make_event("copy", ante, blind))
    return events


def first_copy_index(events, first_copy_pos):
    ante, blind = first_copy_pos
    for i, e in enumerate(events):
        if e["kind"] == "copy" and e["ante"] == ante and e["blind"] == blind:
            return i
    raise ValueError("First baseline copy position is outside timeline: A%d %s" % (ante, BLIND_LONG[blind]))


def redeem_shop_after_second_tag(events, second_tag_idx):
    """
    After the second skip, beat the next blind, then redeem in that shop.
    In the event list, this is the first copy event after the second tag whose
    blind was not skipped.
    """
    skipped_blind = (events[second_tag_idx]["ante"], events[second_tag_idx]["blind"])

    for j in range(second_tag_idx + 1, len(events)):
        e = events[j]
        if e["kind"] == "copy":
            # If this is the copy for the skipped second-tag blind, skip it.
            if (e["ante"], e["blind"]) == skipped_blind:
                continue
            return j
    return None


def copy_count_between(events, start_idx_exclusive, end_idx_exclusive, skipped_tag_indices):
    """
    Count copy events in (start, end), excluding copy events for skipped blinds.
    """
    skipped_blinds = set()
    for idx in skipped_tag_indices:
        e = events[idx]
        skipped_blinds.add((e["ante"], e["blind"]))

    count = 0
    for j in range(start_idx_exclusive + 1, end_idx_exclusive):
        e = events[j]
        if e["kind"] == "copy":
            if (e["ante"], e["blind"]) not in skipped_blinds:
                count += 1
    return count


def copy_count_from(events, start_idx_inclusive, skipped_tag_indices):
    """
    Count copy events from start_idx inclusive to end, excluding skipped blinds.
    """
    skipped_blinds = set()
    for idx in skipped_tag_indices:
        e = events[idx]
        skipped_blinds.add((e["ante"], e["blind"]))

    count = 0
    for j in range(start_idx_inclusive, len(events)):
        e = events[j]
        if e["kind"] == "copy":
            if (e["ante"], e["blind"]) not in skipped_blinds:
                count += 1
    return count


def make_candidate_pairs(events):
    tag_indices = []
    for i, e in enumerate(events):
        if e["kind"] == "tag":
            tag_indices.append(i)

    pairs = []
    for a_i in range(len(tag_indices)):
        i = tag_indices[a_i]
        e1 = events[i]
        for b_i in range(a_i + 1, len(tag_indices)):
            j = tag_indices[b_i]
            e2 = events[j]

            if set([e1["tag_kind"], e2["tag_kind"]]) != set(["neg", "rare"]):
                continue

            shop_idx = redeem_shop_after_second_tag(events, j)
            if shop_idx is None:
                continue

            neg_label = e1["tag_label"] if e1["tag_kind"] == "neg" else e2["tag_label"]
            rare_label = e1["tag_label"] if e1["tag_kind"] == "rare" else e2["tag_label"]

            pairs.append({
                "first_idx": i,
                "second_idx": j,
                "shop_idx": shop_idx,
                "neg_label": neg_label,
                "rare_label": rare_label,
                "first_desc": event_desc(e1),
                "second_desc": event_desc(e2),
                "redeem_shop_desc": event_desc(events[shop_idx]),
            })

    pairs.sort(key=lambda p: (p["first_idx"], p["second_idx"]))
    return pairs


# ----------------------------
# Optimizer
# ----------------------------

def normal_redeem_multiplier(r, k, g):
    return (1.0 - r) + (k + r + g * (1.0 - r)) / C_NORMAL


def final_redeem_multiplier(r, k, g):
    return (1.0 - r) + (k + r + g * (1.0 - r)) / C_FINAL


def next_r_after_normal_redeem(r, k, g, M):
    # J/T_old = spectral_spend/c
    j_over_t = (k + r + g * (1.0 - r)) / C_NORMAL
    return (NORMAL_BS_SHARE * j_over_t) / M


def make_choice(prev_pair_index, pair_index, pair, final, score, T_after, r_after, F, k, g, M, temp_drop):
    return {
        "prev_pair_index": prev_pair_index,
        "pair_index": pair_index,
        "final": final,
        "score": score,
        "T_after": T_after,
        "r_after": r_after,
        "F": F,
        "k": k,
        "g": g,
        "M": M,
        "temp_drop": temp_drop,
        "neg_label": pair["neg_label"],
        "rare_label": pair["rare_label"],
        "first_desc": pair["first_desc"],
        "second_desc": pair["second_desc"],
        "redeem_shop_desc": pair["redeem_shop_desc"],
    }


def optimize_for_fillers(tags, fillers, first_copy_pos, real_ante_min, real_ante_max):
    # Retain all nondominated BP/BS compositions; a maximum total alone can
    # discard a route that would produce a higher final score.
    try:
        from tagcalc_engine import optimize_for_fillers as optimize_exact
    except ImportError:
        from tools.tagcalc_engine import optimize_for_fillers as optimize_exact
    return optimize_exact(tags, fillers, first_copy_pos, real_ante_min, real_ante_max)


def route_key(result):
    score, path, fillers = result
    key = [round(score, 9)]
    for ch in path:
        key.append((
            ch["neg_label"],
            ch["rare_label"],
            ch["final"],
            ch["k"],
            ch["g"],
            round(ch["M"], 9),
            round(ch["T_after"], 9),
            ch["F"],
        ))
    return tuple(key)


def dedupe_results(results):
    deduped = []
    seen = set()
    for res in results:
        key = route_key(res)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(res)
    return deduped


def format_fillers(fillers):
    out = []
    for p in fillers:
        if p <= 38:
            out.append("before real ante %d" % p)
        else:
            out.append("after real ante 38")
    return ", ".join(out)


def print_result(rank, result):
    score, path, fillers = result

    print("=" * 80)
    print("Rank #%d" % rank)
    print("Hieroglyph, then Petroglyph: %s" % format_fillers(fillers))
    print("Final endgame spectral count / baseline T0: %.6fx" % score)
    print("")

    if not path:
        print("No future Negative + Rare redeem route found; this seed is unscored.")
        return

    for i, ch in enumerate(path, start=1):
        label = "FINAL REDEEM" if ch["final"] else "normal redeem"
        print("%d. %s" % (i, label))
        print("   Pair: %s + %s" % (ch["neg_label"], ch["rare_label"]))
        print("   First tag:  %s" % ch["first_desc"])
        print("   Second tag: %s" % ch["second_desc"])
        print("   Redeem shop: %s" % ch["redeem_shop_desc"])
        print("   k full BP+BS copies before selling BS: %d" % ch["k"])
        print("   g BP-only gap copies after selling BS: %d" % ch["g"])
        print("   temporary T after BS sell: %.6fx of pre-sell T" % ch["temp_drop"])
        print("   redeem multiplier M: %.6fx" % ch["M"])
        print("   T after redeem / baseline T0: %.6fx" % ch["T_after"])

        if ch["final"]:
            print("   post-final spectral-copy shop exits F: %d" % ch["F"])
            print("   final score = F * T_final: %.6fx baseline" % ch["score"])
        else:
            print("   next BS fraction r: %.6f" % ch["r_after"])
        print("")


def run_optimizer(first_copy_text, tags_text, top):
    first_copy_pos = parse_shop_exit(first_copy_text)
    tags = parse_tags(tags_text)

    # Filler insertion points:
    # 1..38 = before real ante N
    # 39 = after real ante 38
    #
    # IMPORTANT:
    # Fillers before or at the baseline-copy ante happen before baseline is established.
    # They should not create extra post-baseline Wraith-copy time.
    #
    # Since this optimizer scores only post-baseline Wraith growth, restrict filler
    # insertions to AFTER the baseline copy's real ante.
    #
    # Example:
    #   baseline-copy = a4boss
    #   allowed filler points = 5..39
    # because "before real ante 5" is the first filler that occurs after A4 Boss.
    baseline_real_ante = first_copy_pos[0]
    first_allowed_filler_point = baseline_real_ante + 1
    if first_allowed_filler_point < 1:
        first_allowed_filler_point = 1
    if first_allowed_filler_point > 39:
        first_allowed_filler_point = 39

    filler_points = list(range(first_allowed_filler_point, 40))

    results = []
    for fillers in itertools.combinations_with_replacement(filler_points, 2):
        score, path, f = optimize_for_fillers(
            tags=tags,
            fillers=tuple(sorted(fillers)),
            first_copy_pos=first_copy_pos,
            real_ante_min=1,
            real_ante_max=38,
        )
        if score > 0 and path:
            results.append((score, path, f))

    results.sort(key=lambda x: x[0], reverse=True)
    results = dedupe_results(results)

    print("Supplied probability model (exact route search)")
    print("----------------------------------------")
    print("C_NORMAL = %.6f double tags per normal usable BP/BS" % C_NORMAL)
    print("C_FINAL  = %.6f double tags per final usable BP/BS" % C_FINAL)
    print("Normal redeem composition: BP=%.6f, BS=%.6f" % (NORMAL_BP_SHARE, NORMAL_BS_SHARE))
    print("Final redeem composition:  BP=%.6f, BS=%.6f" % (FINAL_BP_SHARE, FINAL_BS_SHARE))
    print("Baseline first-copy shop exit: %s" % first_copy_text)
    print("Tags parsed: %d" % len(tags))
    print("Allowed filler insertion points: before real ante %d through after real ante 38" % first_allowed_filler_point)
    print("")

    if not results:
        print("No valid redeem route found.")
        return

    for rank, result in enumerate(results[:top], start=1):
        print_result(rank, result)


def main(argv=None):
    # Imports stay lazy so this file remains useful as an importable scoring model.
    try:
        from tagcalc_batch import main as batch_main
    except ImportError:
        from tools.tagcalc_batch import main as batch_main
    return batch_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
