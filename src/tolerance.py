"""
Principled reconciliation tolerance.

Every layer that decides "is this settled_amount close enough to the
merchant's expected net?" reads its tolerance from here — the matcher
(step 2), the validation guard (step 4), and the audit trail (step 5).
Centralizing this avoids drift and keeps the number defensible: we can
say "here is our tolerance rule, and here is exactly one line of code
that implements it."
"""
from decimal import Decimal, ROUND_HALF_UP

_FLOOR = Decimal("0.50")            # sub-rupee drift on small orders
_SCALE = Decimal("0.001")           # 0.1% of gross


def rounding_tolerance(gross_amount: Decimal) -> Decimal:
    """
    Rule: max(₹0.50, 0.1% × gross_amount).

    Why the floor: aggregators routinely round to the nearest paisa, and
    a ₹0.01 discrepancy on a ₹100 order is not a real exception — it
    would drown the queue in noise.

    Why the scaling term: rounding drift on large orders can exceed
    ₹0.50 without indicating a real problem. Fixing an absolute floor
    would either be too loose on small orders or too tight on large
    ones. Scaling with gross keeps the tolerance proportionate.

    Not a magic constant — this is the whole rule, in one place.
    """
    scaled = (gross_amount * _SCALE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return max(_FLOOR, scaled)
