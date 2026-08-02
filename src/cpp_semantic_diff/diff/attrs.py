"""The attribute comparison, in one place, for both halves of the delta.

ARCH-001 §4.3 makes attributes flat scalars precisely so that an attribute diff is
*mechanical*: `attrs["virtual"] = false -> true` becomes `NodeAttrChanged(virtual, false,
true)` with no per-kind logic. §4.2 gives edges `attrs` on the same terms -- `access` on
`inherits`, `virtual` on `calls` -- so the same mechanism produces `EdgeAttrChanged`.

It lives here rather than in `nodes.py` because two copies of this rule would not stay
identical, and the two ways it can drift are both silent: an implementation that compared
the *intersection* of the key sets would lose an attribute that appeared or disappeared,
and one that compared with `==` alone would swallow a producer flipping an attribute's
type, since `False == 0` in Python. Neither has a symptom in the output -- the event simply
is not there.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from cpp_semantic_diff.model import AttrValue


def changed_attrs(
    base: Mapping[str, AttrValue], head: Mapping[str, AttrValue]
) -> Iterator[tuple[str, AttrValue, AttrValue]]:
    """Every attribute that differs, as (name, old, new). One event's worth each.

    The **union** of the two key sets, not the intersection: an attribute that appears or
    disappears is a real change -- `virtual` dropped from a method reads exactly like
    `virtual` set to false -- and an intersection would lose it silently.

    Sorted, so the event list is a function of the two models and not of dict insertion
    order (ARCH-001 §3).
    """
    for attr in sorted(base.keys() | head.keys()):
        old = base.get(attr)
        new = head.get(attr)
        if not same_value(old, new):
            yield attr, old, new


def same_value(old: AttrValue, new: AttrValue) -> bool:
    """Attribute equality, with the type. `False == 0` in Python.

    A producer that flips an attribute's type is a change to the model, and comparing by
    `==` alone would swallow exactly the cases that are hardest to spot afterwards. An
    attribute that is absent on one side and explicitly None on the other compares equal
    and yields no event: the IR admits None as a value (§4.3) and gives no way to tell the
    two apart, so distinguishing them here would be inventing a difference.
    """
    return type(old) is type(new) and old == new
