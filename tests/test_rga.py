import random

from hypothesis import given, settings
from hypothesis import strategies as st

from crdt_cad.crdt.clock import LamportClock, VectorClock
from crdt_cad.crdt.rga import RGA


def test_sequential_local_inserts_preserve_order():
    rga: RGA = RGA(LamportClock(actor="a"))
    op1 = rga.insert_after(None, "H")
    op2 = rga.insert_after(op1.id, "e")
    rga.insert_after(op2.id, "y")
    assert rga.values() == ["H", "e", "y"]


def test_delete_hides_value_but_keeps_tombstone_as_anchor():
    rga: RGA = RGA(LamportClock(actor="a"))
    op1 = rga.insert_after(None, "A")
    rga.insert_after(op1.id, "B")
    rga.delete(op1.id)
    assert rga.values() == ["B"]
    # inserting after a tombstoned node must still work (it remains a valid anchor)
    rga.insert_after(op1.id, "X")
    assert rga.values() == ["X", "B"]
    assert len(rga) == 2  # tombstones excluded from len()


def test_concurrent_inserts_at_same_anchor_converge():
    clock_a = LamportClock(actor="a")
    clock_b = LamportClock(actor="b")
    rga_a: RGA = RGA(clock_a)
    rga_b: RGA = RGA(clock_b)

    root = rga_a.insert_after(None, "root")
    rga_b.apply_insert(root)

    # both replicas concurrently insert right after "root", offline from each other
    op_a = rga_a.insert_after(root.id, "from-a")
    op_b = rga_b.insert_after(root.id, "from-b")

    # deliver in opposite orders to each side
    rga_a.apply_insert(op_b)
    rga_b.apply_insert(op_a)

    assert rga_a.values() == rga_b.values()
    assert set(rga_a.values()) == {"root", "from-a", "from-b"}


def test_merge_is_commutative_and_idempotent_with_deletes():
    clock_a = LamportClock(actor="a")
    clock_b = LamportClock(actor="b")
    rga_a: RGA = RGA(clock_a)
    root = rga_a.insert_after(None, "root")

    rga_b: RGA = RGA(clock_b)
    rga_b.apply_insert(root)

    rga_a.insert_after(root.id, "a1")
    op_a2 = rga_a.insert_after(root.id, "a2")
    rga_a.delete(op_a2.id)

    rga_b.insert_after(root.id, "b1")

    left: RGA = RGA(LamportClock(actor="merger1"))
    left.merge(rga_a)
    left.merge(rga_b)

    right: RGA = RGA(LamportClock(actor="merger2"))
    right.merge(rga_b)
    right.merge(rga_a)

    assert left.values() == right.values()

    # idempotent: merging again changes nothing
    changed = left.merge(rga_a)
    assert changed is False
    assert left.values() == right.values()


def test_three_offline_replicas_converge_after_full_mesh_merge():
    root_clock = LamportClock(actor="seed")
    seed: RGA = RGA(root_clock)
    root = seed.insert_after(None, "root")

    replicas = {}
    for name in ("a", "b", "c"):
        r: RGA = RGA(LamportClock(actor=name))
        r.apply_insert(root)
        replicas[name] = r

    # each replica edits independently while "offline"
    replicas["a"].insert_after(root.id, "a1")
    replicas["a"].insert_after(root.id, "a2")

    replicas["b"].insert_after(root.id, "b1")
    op_b2 = replicas["b"].insert_after(root.id, "b2")
    replicas["b"].delete(op_b2.id)

    replicas["c"].insert_after(root.id, "c1")

    # full mesh merge, each pair in a different order
    order = [("a", "b"), ("b", "c"), ("c", "a"), ("a", "c"), ("b", "a"), ("c", "b")]
    for x, y in order:
        replicas[x].merge(replicas[y])

    final = replicas["a"].values()
    for name in ("b", "c"):
        # bring every replica fully up to date and compare
        replicas[name].merge(replicas["a"])
        assert replicas[name].values() == final

    assert set(final) == {"root", "a1", "a2", "b1", "c1"}  # b2 was deleted


def test_ops_since_delta_sync_returns_only_new_and_deletes():
    clock = LamportClock(actor="a")
    rga: RGA = RGA(clock)
    op1 = rga.insert_after(None, "x")
    op2 = rga.insert_after(op1.id, "y")
    frontier = rga.frontier()

    rga.insert_after(op2.id, "z")
    rga.delete(op1.id)

    delta = rga.ops_since(frontier)
    kinds = sorted(type(op).__name__ for op in delta)
    assert kinds == ["RGADeleteOp", "RGAInsertOp"]

    # applying just the delta to a replica that already had the frontier state
    # must reproduce the exact same final sequence
    catch_up: RGA = RGA(LamportClock(actor="b"))
    catch_up.apply_insert(op1)
    catch_up.apply_insert(op2)
    for op in delta:
        catch_up.apply(op)
    assert catch_up.values() == rga.values()


def test_serialization_roundtrip_json_and_bytes():
    clock = LamportClock(actor="a")
    rga: RGA = RGA(clock)
    op1 = rga.insert_after(None, "p1")
    op2 = rga.insert_after(op1.id, "p2")
    rga.insert_after(op2.id, "p3")
    rga.delete(op1.id)

    restored_json = RGA.from_dict(LamportClock(actor="b"), rga.to_dict())
    assert restored_json.values() == rga.values()

    restored_bytes = RGA.from_bytes(LamportClock(actor="c"), rga.to_bytes())
    assert restored_bytes.values() == rga.values()


def test_compact_drops_stable_tombstone_values_but_preserves_order_and_anchoring():
    clock = LamportClock(actor="a")
    rga: RGA = RGA(clock)
    op1 = rga.insert_after(None, "keep-1")
    op2 = rga.insert_after(op1.id, "will-be-compacted")
    op3 = rga.insert_after(op2.id, "keep-2")
    del_op = rga.delete(op2.id)

    # not yet stable: no vector clock claims to have seen the delete
    assert rga.compact(VectorClock()) == 0

    safe_vc = VectorClock()
    safe_vc.record(del_op.op_id)
    compacted = rga.compact(safe_vc)
    assert compacted == 1
    assert rga.values() == ["keep-1", "keep-2"]  # tombstone still hidden from reads

    # the compacted node must still work as an anchor for a fresh insert
    new_op = rga.insert_after(op2.id, "inserted-after-compacted-tombstone")
    assert rga.values() == ["keep-1", "inserted-after-compacted-tombstone", "keep-2"]

    # compacting again is a no-op (value already gone)
    assert rga.compact(safe_vc) == 0
    del new_op, op3


def test_compact_is_safe_for_a_normal_catch_up_client():
    """The documented common case: a client that has neither the insert
    nor the delete yet still ends up correctly tombstoned after applying
    both ops from ops_since, even though the value was compacted away."""
    clock = LamportClock(actor="a")
    rga: RGA = RGA(clock)
    op1 = rga.insert_after(None, "a")
    op2 = rga.insert_after(op1.id, "b")
    del_op = rga.delete(op2.id)

    safe_vc = VectorClock()
    safe_vc.record(del_op.op_id)
    rga.compact(safe_vc)

    catch_up: RGA = RGA(LamportClock(actor="fresh"))
    for op in rga.ops_since(VectorClock()):
        catch_up.apply(op)
    assert catch_up.values() == ["a"]  # "b" was deleted; its null value never surfaces


def test_delete_arriving_before_its_insert_is_buffered_then_applied():
    clock = LamportClock(actor="a")
    rga: RGA = RGA(clock)
    op1 = rga.insert_after(None, "x")
    op2 = rga.insert_after(op1.id, "y")
    del_op = rga.delete(op2.id)

    late: RGA = RGA(LamportClock(actor="b"))
    late.apply_delete(del_op)  # delete arrives first (out of causal order)
    assert late.values() == []  # nothing to show yet
    late.apply_insert(op1)
    late.apply_insert(op2)  # insert now arrives, should immediately be tombstoned
    assert late.values() == ["x"]


# -- property-based fuzz test -------------------------------------------------


@st.composite
def _rga_program(draw):
    """A short random program of (actor, kind, value_or_target_index) steps."""
    n = draw(st.integers(min_value=3, max_value=12))
    steps = []
    for _ in range(n):
        actor = draw(st.sampled_from(["a", "b", "c"]))
        kind = draw(st.sampled_from(["insert", "insert", "insert", "delete"]))
        value = draw(st.integers(min_value=0, max_value=1000))
        steps.append((actor, kind, value))
    return steps


@given(_rga_program())
@settings(max_examples=60, deadline=None)
def test_random_programs_always_converge_across_replicas(steps):
    actors = ["a", "b", "c"]
    replicas = {name: RGA(LamportClock(actor=name)) for name in actors}
    all_insert_ids = []

    rnd = random.Random(0)
    for actor, kind, value in steps:
        r = replicas[actor]
        if kind == "insert" or not all_insert_ids:
            anchor = rnd.choice([None, *all_insert_ids]) if all_insert_ids else None
            op = r.insert_after(anchor, value)
            all_insert_ids.append(op.id)
            for other_name, other in replicas.items():
                if other_name != actor:
                    other.apply_insert(op)
        else:
            target = rnd.choice(all_insert_ids)
            op = r.delete(target)
            for other_name, other in replicas.items():
                if other_name != actor:
                    other.apply(op)

    reference = replicas["a"].values()
    for name in ("b", "c"):
        assert replicas[name].values() == reference
