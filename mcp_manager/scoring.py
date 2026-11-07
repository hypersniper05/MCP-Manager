"""Scoring — query in, ranked tools out.

Two modes, one seam: everything above this module treats retrieval as `query -> ranked
names`, so nothing above it changes when the mode changes.

  semantic  cosine similarity between EmbeddingGemma vectors, from the model running in this
            project. Used as soon as it has loaded and every tool has a vector.
  lexical   idf-weighted token overlap, rescaled to the same 0..1 scale. The fallback while
            the model is downloading or loading, or if it cannot -- so the floor, the portal
            and the model all see one kind of number either way.

`top_k` returns (exposed_name, score) best-first, nothing below MIN_SCORE. The query's own
vector is passed in already embedded: fetching it is an await, and this module stays sync.
"""

import logging
import math
import re
from collections import Counter

log = logging.getLogger("mcpm")

# Floors, measured on the live catalogue (107 tools, 16 labelled requests, 5 that nothing here
# can do) for embeddinggemma-300m q4 -- not chosen. Re-measure if the model or the document
# format changes: each model has its own cosine scale (EmbeddingGemma 2 put unrelated tools at
# 0.56; this one puts them at 0.15).
# Word matching is scaled so its best hit is 1.0, and keeps what reaches half of that.
# Cosine: unrelated tools score ~0.15 (p99 0.31), requests nothing here can do top out at 0.28,
# and the right tool never scored under 0.33 -- so a 0.30 floor. Then -- the part that does the
# work -- only matches within 0.12 of the best one: the smallest window that never lost the right
# answer (0.10 lost one), keeping a median of 5-6. The ranking is the trustworthy part; a score of
# 0.5 does not mean "50% correct".
MIN_SCORE = 0.5        # word matching, relative to its best hit
SEMANTIC_FLOOR = 0.30  # cosine
RELATIVE = 0.12        # cosine: keep matches within this much of the best one
# A down server is pointed at only if it is clearly the place for the request: the best server by
# meaning, at SERVER_FLOOR or above and SERVER_LEAD ahead of the next. Measured: the right server
# scored 0.29-0.71 and led by 0.13-0.49; requests nothing here can do stayed under 0.20 or led by
# under 0.05; an ambiguous one ("click the login button") led by 0.006 and is rightly left alone.
SERVER_FLOOR = 0.25
SERVER_LEAD = 0.10

STOP = set("the a an and or of to in for on with your you it is what how can need want "
           "please show give get make do does".split())


def document(tool_name, description, title=None, upstream=None, server=None):
    """The text a tool is matched on -- and the one text that gets embedded, so the two
    modes can never disagree about what a tool is.

    Name, then the admin's own words if they wrote any (the gateway resolves that before
    calling this), then the server's. The server line earns its place: it is the only thing
    that tells a tool which domain it lives in, and most tools are exposed under a bare
    name like `status` or `reload` that carries no domain of its own. It carries the server's
    description too: "search the web" ranked the browser's generic `search` above SearXNG's own
    web search, whose server is described as exactly that.

    Only the opening of a long description: a tool that lists results, suggestions, corrections,
    answers and infoboxes embeds as all of those, and drifts away from the one thing it is for.

    Parameter names were left out on purpose: measured, they moved the ranking less than
    they added noise.
    """
    where = f"server: {upstream}" + (f" - {' '.join(server.split())[:200]}" if server else "") if upstream else ""
    return "\n".join(x for x in [tool_name, (title or "").strip(), _lead(description), where] if x)


def _lead(text, limit=300) -> str:
    """The opening sentences of a description, up to about `limit` characters."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > 80 else cut


# ---------------------------------------------------------------- embeddings

# EmbeddingGemma's task prompts (Google's model card): a search query and the documents it is
# matched against are embedded differently, and that asymmetry is what retrieval is trained on.
QUERY_PROMPT = "task: search result | query: {text}"
DOCUMENT_PROMPT = "title: {title} | text: {text}"


async def embed(texts, cfg, kind="document"):
    """Vectors for these texts from the embedding model running in this project (see
    embedder.py). `kind` is "query" for what someone is looking for, "document" for what is
    being searched -- a document's first line (the tool's name) is its title. None on any
    failure: a cold or absent model degrades search to word matching, never takes it down."""
    import asyncio
    if not (cfg or {}).get("file") or not texts:
        return None
    from . import embedder                          # imported only when actually used
    return await asyncio.to_thread(embedder.embed, [_prompted(t, kind) for t in texts], cfg)


def _prompted(text, kind):
    if kind == "query":
        return QUERY_PROMPT.format(text=" ".join(text.split()))
    title, _, rest = text.partition("\n")
    return DOCUMENT_PROMPT.format(title=title.strip() or "none", text=" ".join(rest.split()) or title.strip())


def normalise(vec):
    """L2-normalised, so cosine is a plain dot product -- whatever the model hands back."""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return tuple(x / norm for x in vec)


# ---------------------------------------------------------------- index

def build(entries, cache=None) -> dict:
    """entries: {exposed_name: document_text} -> index.

    `cache` is the gateway's {document_text: vector}. Keyed by the TEXT, not the name: an
    edited description is a different document and must not inherit the old vector -- while
    an unchanged one costs nothing to rebuild.

    Keyed by the name a client would actually call, collisions included -- the gateway owns
    that mapping and this module must not invent its own version of it.
    """
    doc_freq, docs = Counter(), {}
    for key, text in entries.items():
        # the whole tool name is a token too: `list_tabs` has to answer to "list tabs",
        # and splitting on __ alone would lose the words inside it
        toks = _tokens(text) | _tokens(key.rsplit("__", 1)[-1].replace("_", " "))
        docs[key] = toks
        for w in set(toks):
            doc_freq[w] += 1
    vectors = {k: (cache or {}).get(text) for k, text in entries.items()}
    # only a COMPLETE index gets to call itself semantic. Ranking some tools by cosine and
    # the rest by token overlap inside one list would be comparing two different things --
    # so either every tool has a vector, or the whole search is lexical.
    mode = "semantic" if vectors and all(v is not None for v in vectors.values()) else "lexical"
    return {"docs": docs, "df": doc_freq, "n": max(len(docs), 1), "vectors": vectors, "mode": mode}


# ---------------------------------------------------------------- ranking

def top_k(index, query: str, qvec=None, k: int | None = None, names=None) -> list[tuple[str, float]]:
    """(exposed_name, score) at or above MIN_SCORE, best first. `k=None` returns everything
    that clears the floor, so the caller can page through it instead of re-searching.

    `names` limits the search to those tools (one server's, say) BEFORE ranking: the floors are
    relative to the best match, and the best match among all servers would cut away every match
    on the one that was asked for."""
    if not (query or "").strip():
        # An empty query is a caller asking for everything it can reach, not for nothing --
        # which is what a model sends when it tries to page past the end of a search it
        # already ran. Return the whole catalogue unranked (score means nothing here) instead
        # of answering "no matches" to a question that was perfectly answerable.
        return [(name, 0.0) for name in sorted(index["docs"]) if names is None or name in names]
    by_meaning = index.get("mode") == "semantic" and qvec
    scored = semantic(index, qvec, names) if by_meaning else lexical(index, query, names)
    scored.sort(key=lambda kv: -kv[1])
    if by_meaning and scored:
        # A floor alone lets too much through: "search the web" once cleared one for 43 tools.
        # What matters is distance from the best match.
        scored = [kv for kv in scored if kv[1] >= scored[0][1] - RELATIVE]
    return scored if k is None else scored[:k]


def semantic(index, qvec, names=None) -> list[tuple[str, float]]:
    """Cosine. The vectors are normalised, so the dot product IS the similarity: 1.0 for
    text that means the same thing, and it cannot leave [0, 1] -- which is what makes a
    fixed floor (SEMANTIC_FLOOR) mean something."""
    if not qvec:
        return []
    qvec = normalise(qvec)
    out = []
    for name, doc in index["vectors"].items():
        if doc is None or len(doc) != len(qvec) or (names is not None and name not in names):
            continue
        s = max(0.0, min(1.0, sum(a * b for a, b in zip(qvec, doc))))
        if s >= SEMANTIC_FLOOR:
            out.append((name, round(s, 3)))
    return out


def lexical(index, query: str, names=None) -> list[tuple[str, float]]:
    """idf-weighted token overlap, rescaled so the best hit is 1.0. Raw BM25 numbers
    (1.961 and friends) meant nothing next to a cosine, and a floor was meaningless on them."""
    qt = _tokens(query)
    if not qt:
        return []
    df, n = index["df"], index["n"]

    def weight(word):
        # A word in every document has an idf of 0, and with one server registered that is most
        # words: a first search for what it does found nothing. A floor, not smoothing, so the
        # rarer word still decides and every ranking that already worked stays as it was.
        return max(math.log((n + 1) / (df.get(word, 0) + 1)), 0.01)

    raw = [(key, sum(weight(w) for w in (qt & toks)) / math.sqrt(len(toks)))
           for key, toks in index["docs"].items() if qt & toks and (names is None or key in names)]
    best = max((s for _, s in raw), default=0.0)
    if best <= 0:
        return []
    return [(key, round(s / best, 3)) for key, s in raw if s / best >= MIN_SCORE]


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP and len(w) > 1}


def _stem(w: str) -> str:
    """Plural to singular, crudely: "tab" has to find list_tabs. Applied to the query and the
    documents alike, so a word it mangles (status -> statu) still matches itself."""
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
