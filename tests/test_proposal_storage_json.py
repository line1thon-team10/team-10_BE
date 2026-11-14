from copy import deepcopy

import pytest

from app.repositories.proposals import InvalidProposalJSON, canonical_hash, canonical_object


def test_proposal_canonical_json_sorts_keys_but_preserves_text_arrays_and_nulls():
    value = {"z": [True, 0, 1.5, None, "  원문\n "], "a": {"b": "😀"}}
    before = deepcopy(value)
    assert canonical_object(value) == before
    assert canonical_hash(value) == canonical_hash('{"a":{"b":"😀"},"z":[true,0,1.5,null,"  원문\\n "]}')
    assert canonical_hash({"text": " a "}) != canonical_hash({"text": "a"})
    assert canonical_hash({"items": [1, 2]}) != canonical_hash({"items": [2, 1]})
    copied = canonical_object(value)
    copied["z"].append("changed")
    assert value == before


@pytest.mark.parametrize("value", [
    {"x": float("nan")}, {"x": float("inf")}, {"x": float("-inf")},
    {1: "integer key"}, {True: "boolean key"}, {"x": {None: "null key"}},
    {"x": "\x00"}, {"\x00": "value"}, {"x": ["\ud800"]}, {"\udfff": "value"},
    {"x": {"nested": (1, 2)}}, {"x": {1, 2}}, {"x": object()},
    '{"x":1,"x":2}', '{"x":{"a":1,"\\u0061":2}}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e9999}',
    '{"x":"\\u0000"}', '{"\\u0000":"value"}', '{"x":"\\ud800"}',
    '{"x":1} garbage', '{"x":}', '[]', 'null', '1', '"text"',
    [], None, 1, True,
])
def test_proposal_json_rejects_values_that_cannot_be_preserved_consistently(value):
    with pytest.raises(InvalidProposalJSON):
        canonical_object(value)
    with pytest.raises(InvalidProposalJSON):
        canonical_hash(value)


@pytest.mark.parametrize("list_cycle", [False, True])
def test_proposal_json_rejects_cycles_but_allows_shared_noncyclic_objects(list_cycle):
    value = {}
    value["self"] = [value] if list_cycle else value
    with pytest.raises(InvalidProposalJSON, match="Circular"):
        canonical_object(value)
    shared = {"id": "material"}
    assert canonical_object({"first": shared, "second": shared}) == {"first": shared, "second": shared}


def test_proposal_raw_json_decodes_a_valid_unicode_surrogate_pair():
    assert canonical_object('{"symbol":"\\ud83d\\ude00"}') == {"symbol": "😀"}
