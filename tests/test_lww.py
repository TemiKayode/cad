from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.lww import LWWElementSet, LWWMap, LWWRegister


def test_lww_register_local_set_and_read():
    reg: LWWRegister = LWWRegister(LamportClock(actor="a"))
    assert reg.value is None
    reg.set("hello")
    assert reg.value == "hello"
    reg.set("world")
    assert reg.value == "world"


def test_lww_register_higher_op_id_wins_regardless_of_apply_order():
    reg_a: LWWRegister = LWWRegister(LamportClock(actor="a"))
    reg_b: LWWRegister = LWWRegister(LamportClock(actor="b"))

    op_a = reg_a.set("from-a")  # counter 1
    reg_b.apply(op_a)
    op_b = reg_b.set("from-b")  # counter 2, strictly greater
    reg_a.apply(op_b)

    assert reg_a.value == "from-b"
    assert reg_b.value == "from-b"


def test_lww_register_merge_is_commutative():
    reg_a: LWWRegister = LWWRegister(LamportClock(actor="a"))
    reg_b: LWWRegister = LWWRegister(LamportClock(actor="b"))
    reg_a.set("A1")
    reg_b.set("B1")

    left = LWWRegister(LamportClock(actor="a"))
    left.merge(reg_a)
    left.merge(reg_b)

    right = LWWRegister(LamportClock(actor="a"))
    right.merge(reg_b)
    right.merge(reg_a)

    assert left.value == right.value


def test_lww_map_set_get_delete():
    m: LWWMap = LWWMap(LamportClock(actor="a"))
    m.set("color", "red")
    assert m.get("color") == "red"
    assert "color" in m
    m.delete("color")
    assert m.get("color") is None
    assert "color" not in m
    assert m.get("color", "default") == "default"


def test_lww_map_independent_fields_never_conflict():
    """Concurrent edits to *different* keys must both survive the merge."""
    clock_a = LamportClock(actor="a")
    clock_b = LamportClock(actor="b")
    map_a: LWWMap = LWWMap(clock_a)
    map_b: LWWMap = LWWMap(clock_b)

    map_a.set("color", "red")
    map_b.set("width", 2.5)

    map_a.merge(map_b)
    map_b.merge(map_a)

    assert dict(map_a.items()) == {"color": "red", "width": 2.5}
    assert dict(map_a.items()) == dict(map_b.items())


def test_lww_map_concurrent_write_same_key_converges_deterministically():
    clock_a = LamportClock(actor="a")
    clock_b = LamportClock(actor="b")
    map_a: LWWMap = LWWMap(clock_a)
    map_b: LWWMap = LWWMap(clock_b)

    map_a.set("layer", "sketch")
    map_b.set("layer", "sketch")
    # both tick to counter=1 concurrently (offline), so OpId tiebreak is by actor
    op_a = map_a.set("layer", "layer-from-a")
    op_b = map_b.set("layer", "layer-from-b")

    map_a.apply(op_b)
    map_b.apply(op_a)

    # both replicas must agree on exactly one winner
    assert map_a.get("layer") == map_b.get("layer")
    winner = "layer-from-b" if op_b.op_id > op_a.op_id else "layer-from-a"
    assert map_a.get("layer") == winner


def test_lww_map_delete_then_concurrent_set_resolves_by_op_id():
    clock_a = LamportClock(actor="a")
    clock_b = LamportClock(actor="b")
    map_a: LWWMap = LWWMap(clock_a)
    map_a.set("k", "v1")
    map_b: LWWMap = LWWMap(clock_b)
    map_b.merge(map_a)

    del_op = map_a.delete("k")
    set_op = map_b.set("k", "v2")

    map_a.apply(set_op)
    map_b.apply(del_op)

    assert map_a.get("k") == map_b.get("k")


def test_lww_map_ops_since_delta_sync():
    clock = LamportClock(actor="a")
    m: LWWMap = LWWMap(clock)
    m.set("a", 1)
    m.set("b", 2)
    frontier_after_two = m.frontier()
    m.set("c", 3)

    delta = m.ops_since(frontier_after_two)
    assert len(delta) == 1
    assert delta[0].key == "c"


def test_lww_map_serialization_roundtrip():
    clock = LamportClock(actor="a")
    m: LWWMap = LWWMap(clock)
    m.set("color", "blue")
    m.set("removed_field", "x")
    m.delete("removed_field")

    restored = LWWMap.from_bytes(LamportClock(actor="b"), m.to_bytes())
    assert dict(restored.items()) == dict(m.items())
    assert "removed_field" not in restored


def test_lww_element_set_add_remove_and_merge():
    clock_a = LamportClock(actor="a")
    clock_b = LamportClock(actor="b")
    set_a: LWWElementSet = LWWElementSet(clock_a)
    set_b: LWWElementSet = LWWElementSet(clock_b)

    set_a.add("layer-1")
    set_b.merge(set_a)
    assert "layer-1" in set_b

    remove_op = set_a.remove("layer-1")
    set_b.apply(remove_op)
    assert "layer-1" not in set_b
    assert "layer-1" not in set_a


def test_lww_element_set_concurrent_add_and_remove_converges():
    """Classic CRDT edge case: one replica adds while another concurrently
    removes the same (previously unseen) element id reused later -- here we
    test the more common case of concurrent add-vs-remove on an element both
    replicas already know about, which must converge to the same outcome on
    both sides regardless of delivery order."""
    clock_a = LamportClock(actor="a")
    clock_b = LamportClock(actor="b")
    set_a: LWWElementSet = LWWElementSet(clock_a)
    set_a.add("x")
    set_b: LWWElementSet = LWWElementSet(clock_b)
    set_b.merge(set_a)

    remove_op = set_a.remove("x")
    add_op = set_b.add("x")  # concurrent re-add, later Lamport counter on b's clock

    set_a.apply(add_op)
    set_b.apply(remove_op)

    assert ("x" in set_a) == ("x" in set_b)
