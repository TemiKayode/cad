from crdt_cad.crdt.clock import LamportClock, OpId, VectorClock


def test_op_id_total_order_by_counter_then_actor():
    assert OpId(1, "a") < OpId(2, "a")
    assert OpId(2, "a") < OpId(2, "b")
    assert OpId(2, "b") > OpId(2, "a")
    assert OpId(1, "z") < OpId(2, "a")  # counter dominates actor


def test_lamport_clock_tick_strictly_increasing():
    clock = LamportClock(actor="a")
    ids = [clock.tick() for _ in range(5)]
    assert [i.counter for i in ids] == [1, 2, 3, 4, 5]
    assert all(i.actor == "a" for i in ids)


def test_lamport_clock_observe_advances_but_never_rewinds():
    clock = LamportClock(actor="a", counter=5)
    clock.observe(3)
    assert clock.counter == 5  # observing a smaller counter is a no-op
    clock.observe(10)
    assert clock.counter == 10
    next_id = clock.tick()
    assert next_id.counter == 11


def test_vector_clock_has_seen_and_record():
    vc = VectorClock()
    op = OpId(3, "a")
    assert not vc.has_seen(op)
    vc.record(op)
    assert vc.has_seen(op)
    assert vc.has_seen(OpId(2, "a"))  # anything <= recorded counter
    assert not vc.has_seen(OpId(4, "a"))


def test_vector_clock_merge_is_pointwise_max_commutative():
    vc1 = VectorClock({"a": 3, "b": 1})
    vc2 = VectorClock({"a": 1, "b": 5, "c": 2})
    merged1 = vc1.merge(vc2)
    merged2 = vc2.merge(vc1)
    assert merged1 == merged2
    assert merged1.to_dict() == {"a": 3, "b": 5, "c": 2}


def test_vector_clock_dominates_and_concurrent():
    ancestor = VectorClock({"a": 1})
    descendant = VectorClock({"a": 2, "b": 1})
    assert descendant.dominates(ancestor)
    assert not ancestor.dominates(descendant)
    assert not ancestor.concurrent_with(ancestor)

    branch_a = VectorClock({"a": 2, "b": 0})
    branch_b = VectorClock({"a": 0, "b": 2})
    assert branch_a.concurrent_with(branch_b)


def test_vector_clock_roundtrip_dict():
    vc = VectorClock({"a": 4, "b": 2})
    restored = VectorClock.from_dict(vc.to_dict())
    assert restored == vc
