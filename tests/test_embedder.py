"""The embedding seam, tested without a model.

The model runs inside the manager, but the suite does not load it: onnxruntime and the model
files stay optional, so what gets tested here is the contract around them -- absence degrades to
lexical instead of failing, and the ranking, the scale and the floor behave the same either way.
"""

from mcp_manager import embedder, scoring

DOCS = {"list_tabs": "list_tabs\nList open tabs: id, url, title, active.\nserver: browser",
        "screenshot": "screenshot\nScreenshot the viewport or an element.\nserver: browser",
        "generate_image": "generate_image\nCreate a new image from a text prompt.\nserver: imagegen"}


def test_no_model_configured_means_lexical():
    assert embedder.configured({}) is False              # no file, nothing to fetch
    assert embedder.configured({"file": "m.gguf"}) is True


def test_queries_and_documents_carry_embeddinggemmas_task_prompts():
    """Retrieval is trained asymmetric: a query and the documents it searches are prompted
    differently, and a document's first line -- the tool's name -- is its title."""
    assert scoring._prompted("take a screenshot", "query") == "task: search result | query: take a screenshot"
    doc = scoring.document("screenshot", "Screenshot the page.", upstream="browser")
    assert scoring._prompted(doc, "document") == "title: screenshot | text: Screenshot the page. server: browser"


def test_missing_model_and_no_repo_is_not_an_error():
    """A box without the model and nothing to fetch it from must simply be lexical."""
    assert embedder.model_path({"dir": "models", "file": "definitely-not-here.gguf"})
    assert embedder.embed(["x"], {"dir": "models", "file": "definitely-not-here.gguf"}) is None


def test_absent_vectors_fall_back_to_lexical():
    idx = scoring.build(DOCS)
    assert idx["mode"] == "lexical"
    hits = scoring.top_k(idx, "list the open tabs")
    assert hits and hits[0][0] == "list_tabs" and 0.0 <= hits[0][1] <= 1.0


def _cache(vectors):
    """{document text: vector}, which is what the gateway keeps."""
    return {DOCS[name]: scoring.normalise(v) for name, v in vectors.items()}


def test_when_vectors_are_present_the_same_seam_ranks_by_cosine():
    """Hand-built vectors, so the seam is tested without a model: the ranking, the 0..1 scale
    and the floor are the same machinery whichever way the vectors arrived."""
    idx = scoring.build(DOCS, _cache({"list_tabs": [1, 0, 0], "screenshot": [0, 1, 0],
                                      "generate_image": [0, 0, 1]}))
    assert idx["mode"] == "semantic"
    hits = scoring.top_k(idx, "about tabs", qvec=[0.98, 0.2, 0.0])
    assert hits[0][0] == "list_tabs"
    assert all(0.0 <= s <= 1.0 for _, s in hits)
    assert all(s >= scoring.MIN_SCORE for _, s in hits), "the floor holds in both modes"


def test_a_partial_index_does_not_claim_to_be_semantic():
    """Some tools embedded and some not is not a semantic index: mixing cosine with token
    overlap in one ranked list would compare two different things."""
    idx = scoring.build(DOCS, _cache({"list_tabs": [1, 0, 0]}))
    assert idx["mode"] != "semantic"


def test_an_edited_description_does_not_inherit_the_old_vector():
    """Vectors are cached by the text they were made from. Keyed by tool name, an edit kept
    the old vector and search never moved -- in exactly the mode nobody tests by hand."""
    cache = _cache({"list_tabs": [1, 0, 0], "screenshot": [0, 1, 0], "generate_image": [0, 0, 1]})
    edited = {**DOCS, "list_tabs": "list_tabs\nSomething else entirely.\nserver: browser"}
    idx = scoring.build(edited, cache)
    assert idx["vectors"]["list_tabs"] is None and idx["mode"] == "lexical"


def test_the_document_is_what_both_modes_match_on():
    """One document, used by both engines -- they can never disagree about what a tool is."""
    doc = scoring.document("screenshot", "Take a picture of the viewport.", upstream="browser")
    assert "screenshot" in doc and "Take a picture" in doc and "browser" in doc


def test_the_document_carries_the_servers_own_description():
    """"search the web" picked the browser's generic search over SearXNG's own web search: the
    server's description is the strongest hint of what its tools are for."""
    doc = scoring.document("searxng_web_search", "Search the web using SearXNG.", upstream="websearch",
                           server="Searxng web search, and view")
    assert "server: websearch - Searxng web search, and view" in doc


def test_a_long_description_is_embedded_by_its_opening():
    long = "Search the web using SearXNG. " + "Returns results, suggestions and infoboxes. " * 20
    doc = scoring.document("searxng_web_search", long)
    assert doc.startswith("searxng_web_search\nSearch the web using SearXNG.") and len(doc) < 360


def test_semantic_search_keeps_only_matches_near_the_best():
    """Cosine clusters just above the floor: 43 tools 'matched' a vague query."""
    docs = {f"t{i}": f"t{i}" for i in range(5)}
    vecs = {"t0": [1, 0], "t1": [0.97, 0.243], "t2": [0.8, 0.6], "t3": [0.75, 0.66], "t4": [0.7, 0.71]}
    idx = scoring.build(docs, {docs[k]: scoring.normalise(v) for k, v in vecs.items()})
    names = [n for n, _ in scoring.top_k(idx, "q", qvec=[1, 0])]
    assert names == ["t0", "t1"], names       # 1.0 and 0.97 stay; 0.8 and below are > 0.1 behind
