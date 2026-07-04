"""Shared JSON / binary (MessagePack) codec helpers used by every CRDT.

Every CRDT in this package exposes the same four methods:
``to_dict`` / ``from_dict`` (plain JSON-safe structures, used over the
WebSocket wire and for human inspection) and ``to_bytes`` / ``from_bytes``
(MessagePack, used for compact persistence and snapshotting). This module
centralises the OpId <-> wire-format conversion so every CRDT encodes ids
identically.
"""

from __future__ import annotations

from typing import Any

import msgpack

from crdt_cad.crdt.clock import ActorId, OpId


def op_id_to_wire(op_id: OpId) -> list[Any]:
    return [op_id.counter, op_id.actor]


def op_id_from_wire(data: Any) -> OpId:
    counter, actor = data
    return OpId(int(counter), str(actor))


def dumps_msgpack(obj: dict) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def loads_msgpack(data: bytes) -> dict:
    return msgpack.unpackb(data, raw=False)


ActorIdT = ActorId
