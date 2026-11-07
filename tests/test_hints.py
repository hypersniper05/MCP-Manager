"""The coaching rules, without a server: what gets fixed, what gets refused, what passes.

A fix is applied only when there is one reading of what the model meant. These pin that
line down, because a fix on the wrong side of it runs a call nobody asked for.
"""

from mcp_manager import hints

READ_TEXT = {"type": "object", "additionalProperties": False, "properties": {
    "target": {"type": "string"}, "frame": {"type": "string"},
    "maxLength": {"type": "integer", "minimum": 500, "maximum": 40000}}}
NAVIGATE = {"type": "object", "additionalProperties": False, "required": ["url"],
            "properties": {"url": {"type": "string"}, "tab": {"type": "string"}}}
OPEN = {"type": "object", "properties": {
    "flag": {"type": "boolean"}, "tags": {"type": "array", "items": {"type": "string"}},
    "opts": {"type": "object"}, "mode": {"type": "string", "enum": ["fast", "slow"]},
    "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
    "id": {"type": "string"}, "limit": {"type": "integer"}}}


def test_a_name_spelt_differently_is_the_same_name():
    assert hints.norm("listTabs") == hints.norm("list-tab") == hints.norm("LIST_TABS") == "listtab"
    assert hints.norm("process") == "process", "a double s is not a plural"


def test_case_and_strings_for_numbers_are_fixed_and_said():
    out, fixes, problems, _ = hints.check(READ_TEXT, {"maxlength": "1000"})
    assert out == {"maxLength": 1000} and not problems
    assert any("maxLength" in f for f in fixes) and any("1000" in f for f in fixes)


def test_a_near_miss_key_for_the_missing_required_argument_is_renamed():
    """The value is the model's own; only the key is ours, and it can mean one thing."""
    out, fixes, problems, _ = hints.check(NAVIGATE, {"uri": "about:blank"})
    assert out == {"url": "about:blank"} and not problems and fixes


def test_a_missing_required_argument_stops_the_call():
    _, _, problems, _ = hints.check(NAVIGATE, {})
    assert problems == ['missing required argument "url" (string)']


def test_junk_on_a_closed_schema_is_dropped_but_kept_on_an_open_one():
    out, fixes, problems, _ = hints.check(READ_TEXT, {"reason": "because"})
    assert out == {} and not problems and "dropped" in fixes[0]
    out, _, _, warnings = hints.check(OPEN, {"reason": "because"})
    assert out == {"reason": "because"} and warnings


def test_obvious_shapes_are_coerced():
    out, fixes, problems, _ = hints.check(OPEN, {
        "flag": "TRUE", "tags": "solo", "opts": '{"a": 1}', "mode": "FAST", "id": 42, "limit": "7"})
    assert out == {"flag": True, "tags": ["solo"], "opts": {"a": 1}, "mode": "fast", "id": "42", "limit": 7}
    assert len(fixes) == 6 and not problems


def test_null_for_an_optional_argument_is_left_out_unless_null_is_allowed():
    out, fixes, _, _ = hints.check(OPEN, {"limit": None, "note": None})
    assert out == {"note": None}, "note is Optional; limit is not"
    assert any("limit" in f for f in fixes)


def test_what_cannot_be_read_one_way_is_left_for_the_upstream():
    out, fixes, problems, _ = hints.check(OPEN, {"limit": "lots", "mode": "medium"})
    assert out == {"limit": "lots", "mode": "medium"} and not fixes and not problems


def test_usage_names_every_argument_and_gives_a_call_to_copy():
    text = hints.usage("navigate", NAVIGATE, via_call_tool=True)
    assert "url: string" in text and "tab?: string" in text
    assert 'call_tool {"name": "navigate", "arguments": {"url": "<string>"}}' in text
    assert 'navigate {"url": "<string>"}' in hints.usage("navigate", NAVIGATE, via_call_tool=False)
    assert "500–40000" in hints.signature(READ_TEXT)
