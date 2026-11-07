"""Boot config plumbing (config.load / config.tls_kwargs). Fast, no network.

TLS matters here because a half-configured certificate is the dangerous case: the
operator thinks the key crosses the network encrypted, and it does not. So the
all-or-nothing rule is the behaviour under test, not the file format.
"""

import pytest

from mcp_manager import config

ENV_VARS = ("MCPM_TLS_CERT", "MCPM_TLS_KEY", "MCPM_ADMIN_TOKEN", "MCPM_DB",
            "MCPM_MODELS_DIR", "MCPM_HOST")


@pytest.fixture
def clean_env(monkeypatch):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def write_cfg(tmp_path, body="boot:\n"):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def certs(tmp_path, name="tls"):
    crt, key = tmp_path / f"{name}-cert.pem", tmp_path / f"{name}-key.pem"
    crt.write_text("cert")
    key.write_text("key")
    return str(crt), str(key)


def test_the_default_bind_is_localhost(clean_env, tmp_path):
    boot, _ = config.load(write_cfg(tmp_path))
    assert boot["host"] == "127.0.0.1"          # local-only unless the operator decides otherwise
    boot, _ = config.load(write_cfg(tmp_path, "boot:\n  host: 10.0.0.5\n"))
    assert boot["host"] == "10.0.0.5"           # the file can name an interface


def test_env_can_widen_the_bind(clean_env, tmp_path, monkeypatch):
    # The container must bind 0.0.0.0 for its port mapping to answer; env beats the file,
    # like every other override.
    monkeypatch.setenv("MCPM_HOST", "0.0.0.0")
    boot, _ = config.load(write_cfg(tmp_path, "boot:\n  host: 127.0.0.1\n"))
    assert boot["host"] == "0.0.0.0"


def test_without_tls_the_config_carries_none_and_uvicorn_gets_nothing(clean_env, tmp_path):
    boot, _ = config.load(write_cfg(tmp_path))
    assert boot["tls_cert"] is None and boot["tls_key"] is None
    assert config.tls_kwargs(boot) == {}


def test_tls_paths_from_the_file_become_uvicorn_kwargs(clean_env, tmp_path):
    crt, key = certs(tmp_path)
    # single-quoted YAML: a Windows temp path has backslashes, and double quotes would
    # read those as escapes
    boot, _ = config.load(write_cfg(tmp_path, f"boot:\n  tls_cert: '{crt}'\n  tls_key: '{key}'\n"))
    assert config.tls_kwargs(boot) == {"ssl_certfile": crt, "ssl_keyfile": key}


def test_env_beats_the_file_for_tls_paths(clean_env, tmp_path, monkeypatch):
    crt, key = certs(tmp_path, "from-env")
    other, _ = certs(tmp_path, "from-file")
    monkeypatch.setenv("MCPM_TLS_CERT", crt)
    monkeypatch.setenv("MCPM_TLS_KEY", key)
    boot, _ = config.load(write_cfg(tmp_path, f"boot:\n  tls_cert: '{other}'\n  tls_key: '{other}'\n"))
    assert config.tls_kwargs(boot) == {"ssl_certfile": crt, "ssl_keyfile": key}


def test_half_a_tls_config_stops_the_start_with_a_named_missing_file(clean_env, tmp_path, monkeypatch):
    crt, _ = certs(tmp_path)
    monkeypatch.setenv("MCPM_TLS_CERT", crt)          # the key stays unset
    boot, _ = config.load(write_cfg(tmp_path))
    with pytest.raises(SystemExit) as e:
        config.tls_kwargs(boot)
    assert "(unset)" in str(e.value)                  # the message says which one

    monkeypatch.setenv("MCPM_TLS_KEY", str(tmp_path / "gone.pem"))
    boot, _ = config.load(write_cfg(tmp_path))
    with pytest.raises(SystemExit) as e:
        config.tls_kwargs(boot)
    assert "gone.pem" in str(e.value)                 # and names the file that is not there
