"""What the model is told about a tool, and what it is told when it gets one wrong.

Every round trip a model spends on a wrong guess is a round trip it is not spending on the
task. So a call that can be understood is run, with a note saying what was fixed; a call
that cannot is answered with exactly what to send instead -- never a bare "invalid".

Conservative on purpose. A fix is applied only when there is one reading of what the model
meant (a different case, a number sent as a string, a null for an optional argument). A call
is refused here only when the upstream would refuse it too (a missing required argument) or
when a near-miss argument name shows the model meant something else. Everything else goes to
the upstream, whose own error comes back with the usage attached.
"""

import difflib
import json
import re

_SAME = object()


def short(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def norm(name: str) -> str:
    """A name with its spelling taken out: case, separators and a trailing plural.
    listTabs, list-tab and LIST_TABS all come out as `listtab`."""
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return n[:-1] if len(n) > 3 and n.endswith("s") and not n.endswith("ss") else n


def _props(schema):
    schema = schema if isinstance(schema, dict) else {}
    props = schema.get("properties")
    return schema, (props if isinstance(props, dict) else {})


def kind(spec: dict) -> str:
    if not isinstance(spec, dict):
        return "any"
    if "enum" in spec:
        return " | ".join(json.dumps(v) for v in spec["enum"][:10])
    t = spec.get("type")
    if isinstance(t, list):
        t = " | ".join(x for x in t if x != "null") or "null"
    if t == "array":
        items = spec.get("items")
        return kind(items) + "[]" if isinstance(items, dict) and items else "array"
    if not t:
        # Optional[...] from pydantic arrives as anyOf [{type: x}, {type: null}]
        for alt in ("anyOf", "oneOf"):
            if isinstance(spec.get(alt), list):
                kinds = [kind(s) for s in spec[alt] if isinstance(s, dict) and s.get("type") != "null"]
                return " | ".join(kinds) or "any"
        return "object" if "$ref" in spec or "properties" in spec else "any"
    return t


def _range(spec: dict) -> str:
    lo, hi = spec.get("minimum"), spec.get("maximum")
    if lo is not None and hi is not None:
        return f" {lo}–{hi}"
    return f" ≥{lo}" if lo is not None else f" ≤{hi}" if hi is not None else ""


def brief(text: str, limit: int = 160) -> str:
    """The opening of a description: whole sentences up to `limit` characters, else the first
    one clipped. A tool's first sentence says what it is for; the rest is how to drive it, which
    the model needs once it has picked the tool, not while it is choosing one."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end >= 20 else short(text, limit)


def signature(schema, compact_optional: bool = False) -> str:
    """A tool's arguments as one line a model can act on: `url: string (the page);
    new_tab?: boolean = false`. A trailing ? marks an optional one.

    `compact_optional`: what an argument is for only where it decides whether the call works --
    the required ones, briefly. An optional one keeps its type and default, which is enough to use
    it; describing every optional argument of every match more than doubled a search result, and a
    call that gets one wrong is answered with the full usage anyway."""
    schema, props = _props(schema)
    if not props:
        return "none"
    required = set(schema.get("required") or [])
    parts = []
    for pname, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        part = f"{pname}{'' if pname in required else '?'}: {kind(spec)}{_range(spec)}"
        default = spec.get("default")
        if default is not None and not (compact_optional and default in ("", [], {})):
            part += f" = {json.dumps(default)}"
        if spec.get("description") and not (compact_optional and pname not in required):
            part += f" ({short(spec['description'], 60 if compact_optional else 90)})"
        parts.append(part)
    return "; ".join(parts)


def arg_names(schema, limit: int = 6) -> str:
    """`prompt, size?, seed?, …` -- the names alone, for an overview of a whole server."""
    schema, props = _props(schema)
    required = set(schema.get("required") or [])
    names = [p + ("" if p in required else "?") for p in props]
    return ", ".join(names[:limit]) + (", …" if len(names) > limit else "") if names else "none"


def compact(name: str, schema) -> str:
    """name(a, b?, c?) -- for listing a whole server's tools without spending a page on it."""
    args = arg_names(schema)
    return f"{name}({'' if args == 'none' else args})"


def example(name: str, schema, via_call_tool: bool) -> str:
    """A call the model can copy and fill in: the required arguments, with placeholders."""
    schema, props = _props(schema)
    args = {}
    for p in schema.get("required") or []:
        spec = props.get(p) if isinstance(props.get(p), dict) else {}
        args[p] = spec["enum"][0] if spec.get("enum") else f"<{kind(spec)}>"
    if via_call_tool:
        return "call_tool " + json.dumps({"name": name, "arguments": args})
    return f"{name} {json.dumps(args)}"


def usage(name: str, schema, via_call_tool: bool) -> str:
    """How to call it, in full: every argument on its own line, then an example."""
    schema, props = _props(schema)
    required = set(schema.get("required") or [])
    lines = [f"How to call {name}:"]
    for pname, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        line = f"  {pname}{'' if pname in required else '?'}: {kind(spec)}{_range(spec)}"
        if spec.get("default") is not None:
            line += f" = {json.dumps(spec['default'])}"
        if spec.get("description"):
            line += f" — {short(spec['description'], 120)}"
        lines.append(line)
    if not props:
        lines.append("  it takes no arguments")
    lines.append("  e.g. " + example(name, schema, via_call_tool))
    return "\n".join(lines)


# ---- checking a call before it goes out --------------------------------------------

def check(schema, args: dict):
    """(arguments to send, fixes made, problems that stop the call, warnings).

    Fixes are told to the model alongside the result, so it learns the right form and
    the next call needs none.
    """
    schema, props = _props(schema)
    required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
    closed = schema.get("additionalProperties") is False
    fixes, problems, warnings, out = [], [], [], {}

    by_norm = {}
    for p in props:
        by_norm.setdefault(norm(p), []).append(p)
    for given, value in args.items():
        if given in props:
            out[given] = value
            continue
        free = [p for p in props if p not in args and p not in out]
        same = [p for p in by_norm.get(norm(given), []) if p in free]
        if len(same) == 1:
            out[same[0]] = value
            fixes.append(f'"{given}" is spelled "{same[0]}"')
            continue
        # A near-miss key keeps the model's own value; only the key is ours. So rename when it
        # can only mean one thing: the one missing required argument it resembles, or the one
        # argument it closely resembles.
        score = {p: difflib.SequenceMatcher(None, norm(given), norm(p)).ratio() for p in free}
        wanted = [p for p in free if p in required and score[p] >= 0.6]
        alike = [p for p in free if score[p] >= 0.8]
        target = wanted[0] if len(wanted) == 1 else alike[0] if len(alike) == 1 else None
        if target:
            out[target] = value
            fixes.append(f'"{given}" is "{target}"')
            continue
        nearest = max(free, key=score.get) if free and max(score.values()) >= 0.6 else None
        hint = f' (nearest: "{nearest}")' if nearest else ""
        if closed:
            # the upstream would reject it outright; running without it costs nothing more
            fixes.append(f'dropped "{given}", which is not one of its arguments{hint}')
        else:
            out[given] = value
            warnings.append(f'"{given}" is not one of its declared arguments; sent as-is{hint}')

    for p, value in list(out.items()):
        spec = props.get(p)
        if not isinstance(spec, dict):
            continue
        if value is None and p not in required and not _nullable(spec):
            del out[p]
            fixes.append(f'"{p}" is optional: leave it out rather than sending null')
            continue
        fixed = _coerce(spec, value)
        if fixed is not _SAME:
            out[p] = fixed
            fixes.append(f'"{p}" takes {kind(spec)}: {json.dumps(value)} became {json.dumps(fixed)}')

    for r in required:
        if r not in out:
            spec = props.get(r) if isinstance(props.get(r), dict) else {}
            what = f" — {short(spec['description'], 90)}" if spec.get("description") else ""
            problems.append(f'missing required argument "{r}" ({kind(spec)}){what}')
    return out, fixes, problems, warnings


def _nullable(spec: dict) -> bool:
    t = spec.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    alts = spec.get("anyOf") or spec.get("oneOf") or []
    return any(isinstance(a, dict) and a.get("type") == "null" for a in alts)


def _base_type(spec: dict):
    t = spec.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        ts = [x for x in t if x != "null"]
        return ts[0] if len(ts) == 1 else None
    alts = [a.get("type") for a in (spec.get("anyOf") or spec.get("oneOf") or [])
            if isinstance(a, dict) and a.get("type") != "null"]
    return alts[0] if len(alts) == 1 and isinstance(alts[0], str) else None


def _coerce(spec: dict, v):
    """The one obvious reading of a value sent in the wrong shape, or _SAME."""
    if v is None:
        return _SAME
    if isinstance(spec.get("enum"), list) and isinstance(v, str) and v not in spec["enum"]:
        hits = [e for e in spec["enum"] if isinstance(e, str) and e.lower() == v.strip().lower()]
        return hits[0] if len(hits) == 1 else _SAME
    t = _base_type(spec)
    if t in ("integer", "number") and isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return _SAME
        if f.is_integer():
            return int(f)
        return f if t == "number" else _SAME
    if t == "boolean":
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        if isinstance(v, int) and not isinstance(v, bool) and v in (0, 1):
            return bool(v)
    if t == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    if t in ("array", "object") and isinstance(v, str):
        try:
            parsed = json.loads(v)
        except ValueError:
            parsed = _SAME
        if (t == "array" and isinstance(parsed, list)) or (t == "object" and isinstance(parsed, dict)):
            return parsed
        return [v] if t == "array" else _SAME
    if t == "array" and not isinstance(v, list):
        return [v]                                  # one value where a list goes
    return _SAME
