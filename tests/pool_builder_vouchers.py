#!/usr/bin/env python3
"""Focused standalone-builder regression for voucher route criteria."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_builder as core
import pool_builder_web as web


class Snapshot:
    def usable_legendaries(self):
        return ["j_perkeo"]

    def usable_tags(self):
        return [("tag_voucher", 1), ("tag_rare", 1)]

    def usable_vouchers(self):
        return [
            ("v_overstock_norm", ""),
            ("v_overstock_plus", "v_overstock_norm"),
            ("v_tarot_merchant", ""),
        ]


criteria = web.criteria_from_json({
    "voucherRules": [
        {"key": "v_overstock_norm", "min": 1, "max": 4},
        {"key": "v_overstock_plus", "min": 3, "max": 7},
    ],
    "voucherExclusions": ["v_tarot_merchant", "v_overstock_norm"],
}, Snapshot())

text = criteria.text("binary", 1000)
assert "voucher v_overstock_norm 1 4\n" in text
assert "voucher v_overstock_plus 3 7\n" in text
assert "voucher_exclude v_tarot_merchant\n" in text
assert "voucher_exclude v_overstock_norm\n" in text
assert criteria.predicates()
assert "Overstock (A1 through A4)" in criteria.summary()
assert "route cannot purchase Tarot Merchant, Overstock" in criteria.summary()
assert "overstock_norm-a1-4" in criteria.pool_name()
assert "no-buy-tarot_merchant" in criteria.pool_name()

# An exclusion is a route constraint, not a positive filter of its own.
for bad in (
    {"voucherExclusions": ["v_tarot_merchant"]},
    {"voucherRules": [{"key": "v_missing", "min": 1, "max": 2}]},
    {"voucherRules": [{"key": "v_overstock_norm", "min": 1, "max": 2}],
     "voucherExclusions": ["v_tarot_merchant", "v_tarot_merchant"]},
    {"rules": [{"key": "tag_voucher", "min": 1, "max": 2, "count": 1}],
     "voucherRules": [{"key": "v_overstock_norm", "min": 1, "max": 2}],
     "route": "collect"},
):
    try:
        web.criteria_from_json(bad, Snapshot())
    except ValueError:
        pass
    else:
        raise AssertionError("invalid voucher builder criteria were accepted: %r" % bad)

# The same tag/voucher combination is valid when tags are only observed.
observed = web.criteria_from_json({
    "rules": [{"key": "tag_voucher", "min": 1, "max": 2, "count": 1}],
    "voucherRules": [{"key": "v_overstock_norm", "min": 1, "max": 2}],
    "route": "observe",
}, Snapshot())
assert "tag_route observe\n" in observed.text("binary", 1000)

for marker in (
    'id="voucherRules"', 'id="voucherExclusions"',
    "addVoucherRule", "addVoucherExclusion", "voucherRules, voucherExclusions",
):
    assert marker in web.PAGE

print("pool builder vouchers: ok")
