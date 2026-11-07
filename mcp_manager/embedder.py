"""The embedding model, running inside this project -- the only source of embeddings.

embeddinggemma-300m (EmbeddingGemma gen 1), 4-bit, run in-process with onnxruntime. Nothing outside
the manager is called for embeddings. On first start the model (~200 MB) and its tokenizer are
pulled from Hugging Face into the models directory -- a volume in the container -- and never again.

Why this one, measured on the live catalogue against EmbeddingGemma 2 (int8): it ranked the right
tool better (mean rank 1.38 vs 1.81), embedded a query in 12 ms instead of 58, and held ~600 MB
instead of ~910. Gen 1's int8 export is a trap: ~1.7 GB once loaded, and slower than the 4-bit.

Why onnxruntime: the ONNX export runs the whole text pipeline -- tokens in, the pooled and projected
768-dimension vector out -- on a runtime with prebuilt wheels everywhere, and runs either generation.

Optional at runtime, never fatal: a missing model, a missing library or an offline hub degrades
search to word matching, it does not take the manager down. The libraries are imported inside the
functions, so a box without them (the test suite runs lexical on purpose) never needs them.
"""

import logging
import threading
from pathlib import Path

log = logging.getLogger("mcpm")

_load_lock = threading.Lock()
_model = None                    # (onnx session, tokenizer), loaded once on first use and kept
_failed = False                  # don't retry a model that failed to load on every search
BATCH = 16                       # texts per run: padding is masked, so batching changes nothing but speed


def configured(cfg: dict) -> bool:
    """True when a model file is named, i.e. embeddings are meant to run."""
    return bool(model_path(cfg))


def model_path(cfg: dict) -> Path | None:
    """Where the ONNX file lives (or will live). Downloading is `ensure_model`'s job, called once
    at startup and never on the search path."""
    d, f = (cfg or {}).get("dir") or "models", (cfg or {}).get("file")
    return Path(d) / f if f else None


def _files(cfg: dict) -> list[str]:
    """Every file the model needs, as paths inside the repo: the graph, its external weights,
    and the tokenizer."""
    f = cfg["file"]
    return [f, f + "_data", cfg.get("tokenizer", "tokenizer.json")]


def ensure_model(cfg: dict) -> Path | None:
    """Fetch the model files from Hugging Face into the models dir. Returns the model path, or
    None if they aren't there and couldn't be fetched.

    Called from the startup task, never from a request: first run pulls a few hundred MB, and a
    search that silently triggered a download would look like a hang.
    """
    path = model_path(cfg)
    if not path:
        return None
    root = Path(cfg.get("dir") or "models")
    missing = [f for f in _files(cfg) if not (root / f).exists()]
    if not missing:
        return path
    repo = cfg.get("repo")
    if not repo:
        log.info("embedding model files missing (%s) and no repo configured to fetch them", missing)
        return None
    try:
        from huggingface_hub import hf_hub_download        # only needed to download
        log.info("downloading %s: %s (first run only)", repo, ", ".join(missing))
        root.mkdir(parents=True, exist_ok=True)
        for f in missing:
            hf_hub_download(repo_id=repo, filename=f, local_dir=str(root))
        size = sum((root / f).stat().st_size for f in _files(cfg)) // 2**20
        log.info("embedding model ready: %s (%d MB)", repo, size)
        return path
    except Exception as e:                                 # noqa: BLE001
        log.warning("could not fetch %s (%s): search stays lexical", repo, e)
        return None


def embed(texts: list[str], cfg: dict) -> list[list[float]] | None:
    """Embed in-process. Returns None on any problem -- the caller falls back to lexical.

    The texts arrive already carrying their task prompt (scoring.embed adds it). Synchronous by
    design: onnxruntime blocks, so scoring calls this through a thread.
    """
    global _model, _failed
    if _failed or not texts:
        return None
    path = model_path(cfg)
    if not path or not path.exists():
        return None
    try:
        if _model is None:
            with _load_lock:
                if _model is None:                     # two threads can get here at once
                    _model = _load(path, cfg)
        session, tok, extra = _model
        import numpy as np
        out = []
        for i in range(0, len(texts), BATCH):
            enc = tok.encode_batch(texts[i:i + BATCH])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            vecs = session.run(["sentence_embedding"], {"input_ids": ids, "attention_mask": mask, **extra})[0]
            out += [v.tolist() for v in vecs]
        return out
    except Exception as e:                             # noqa: BLE001
        _failed = True
        log.warning("in-process embeddings failed (%s): search stays lexical", e)
        return None


def _load(path: Path, cfg: dict):
    import onnxruntime as ort
    from tokenizers import Tokenizer
    log.info("loading embedding model %s", path.name)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = int(cfg.get("threads", 4))
    session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
    if "sentence_embedding" not in {o.name for o in session.get_outputs()}:
        raise RuntimeError(f"{path.name} has no sentence_embedding output: not an EmbeddingGemma export")
    # Text only. Some exports take more than tokens -- EmbeddingGemma 2's also wants image, video
    # and audio features -- so every other input the model declares is fed empty. Either
    # generation loads; the floors in scoring.py are measured for one, though.
    import numpy as np
    extra = {i.name: np.zeros((0, i.shape[-1] if isinstance(i.shape[-1], int) else 512), dtype=np.float32)
             for i in session.get_inputs() if i.name not in ("input_ids", "attention_mask")}
    tok = Tokenizer.from_file(str(Path(cfg.get("dir") or "models") / cfg.get("tokenizer", "tokenizer.json")))
    tok.enable_truncation(int(cfg.get("max_tokens", 2048)))    # tool descriptions are short; bound the odd one
    tok.enable_padding(pad_id=tok.token_to_id("<pad>") or 0)
    return session, tok, extra
