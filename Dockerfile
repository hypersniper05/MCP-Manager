# syntax=docker/dockerfile:1
#
# One stage: everything installs from prebuilt wheels, including the embedding runtime
# (onnxruntime + tokenizers) that runs EmbeddingGemma (embeddinggemma-300m) inside the manager.
#
# Nothing here is optional in the way that quietly breaks at 2am: if the embedding model can't
# load, search falls back to word matching and the manager still serves. The image needs
# nothing else running -- no model server, on the host or anywhere.

FROM python:3.12-slim
WORKDIR /app

# Upgrade pip first: the pip that ships in the base image (25.x) is the one package a vuln
# scan flags here (PYSEC-2026-1795/1796/196/2875/2876/3721), so take its fix before it is used
# to install anything. The runtime deps carry floors past their own known advisories --
# h11>=0.16 for the HTTP request-smuggling fix, a current cryptography -- and float above them
# so a rebuild --pull keeps picking up patches.
RUN pip install --no-cache-dir --quiet --root-user-action=ignore --upgrade pip \
 && pip install --no-cache-dir --quiet --root-user-action=ignore \
      mcp pyyaml uvicorn "h11>=0.16" httpx2 "cryptography>=44" onnxruntime tokenizers huggingface-hub

COPY mcp_manager/ ./mcp_manager/
COPY portal/ ./portal/
COPY config.yaml ./

# The database and the model both live under /data, which is a volume: the model is fetched from
# Hugging Face on first start and never again, so a rebuild of the image must not lose it.
RUN useradd -m mcpm && mkdir -p /data && chown -R mcpm:mcpm /data
USER mcpm

# Absolute paths here, relative in config.yaml: the container must not write its database -- or
# a 300 MB model -- into the image layer.
ENV MCPM_DB=/data/mcp_manager.db \
    MCPM_MODELS_DIR=/data/models/embeddinggemma-300m \
    PYTHONUNBUFFERED=1

EXPOSE 8899
CMD ["python", "-m", "mcp_manager", "config.yaml"]
