"""The certificate the installers make, held to a real TLS handshake -- deterministic, no network
beyond loopback.

Every client that must accept it is judged by OpenSSL's rules here, including the strict ones newer
Pythons use. What it must never be is a way to sign anything else: no root key may reach the disk.
"""

import datetime
import os
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from mcp_manager import config, tls

NAMES = ["mcp.example", "192.0.2.7", *tls.ALWAYS]


def _load(directory, name):
    with open(os.path.join(directory, name), "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    directory = str(tmp_path_factory.mktemp("tls"))
    tls.make(directory, NAMES)
    return directory


def _handshake(directory, server_hostname, strict=True, address="127.0.0.1"):
    """A TLS server on a loopback port with this certificate, and a client trusting only ca.pem."""
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(os.path.join(directory, tls.CERT), os.path.join(directory, tls.KEY))
    client = ssl.create_default_context(cafile=os.path.join(directory, tls.ROOT))
    if strict:                                   # what Python 3.13 turns on by default
        client.verify_flags |= ssl.VERIFY_X509_STRICT
    listener = socket.socket(socket.AF_INET)
    listener.bind((address, 0))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        try:
            with server.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):          # the client refusing is what some tests want
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(listener.getsockname(), timeout=5) as raw:
            with client.wrap_socket(raw, server_hostname=server_hostname) as tlssock:
                return tlssock.version()
    finally:
        listener.close()
        thread.join(timeout=5)


@pytest.mark.parametrize("name", NAMES)
def test_a_client_trusting_the_root_accepts_every_name(made, name):
    assert _handshake(made, name) in ("TLSv1.2", "TLSv1.3")


def test_a_name_the_certificate_does_not_list_is_refused(made):
    with pytest.raises(ssl.SSLCertVerificationError, match="mismatch"):
        _handshake(made, "somewhere.else")


def test_a_client_that_trusts_nothing_refuses_it(made, tmp_path):
    other = str(tmp_path)
    tls.make(other, NAMES)
    mixed = str(tmp_path / "mixed")
    os.makedirs(mixed)
    for name, source in ((tls.CERT, made), (tls.KEY, made), (tls.ROOT, other)):
        with open(os.path.join(source, name), "rb") as src, open(os.path.join(mixed, name), "wb") as dst:
            dst.write(src.read())
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(mixed, "localhost")


def test_nothing_on_disk_can_sign_another_certificate(made):
    assert sorted(os.listdir(made)) == sorted([tls.ROOT, tls.CERT, tls.KEY])
    assert open(os.path.join(made, tls.ROOT)).read().count("PRIVATE KEY") == 0
    server = _load(made, tls.CERT)
    assert server.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert server.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign is False


def test_the_certificate_meets_what_apple_and_the_trust_stores_demand(made):
    root, server = _load(made, tls.ROOT), _load(made, tls.CERT)
    for cert in (root, server):                  # from start to end, not from today: 826 is refused
        assert cert.not_valid_after_utc - cert.not_valid_before_utc <= datetime.timedelta(days=825)
    usage = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in usage
    assert server.public_key().key_size >= 2048 and server.signature_hash_algorithm.name == "sha256"
    # what goes into a trust store is marked as an authority, for the stores that keep only those
    constraints = root.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical and constraints.value.ca and constraints.value.path_length == 0
    assert server.issuer == root.subject


@pytest.mark.skipif(os.name == "nt", reason="Windows has no such file modes")
def test_the_key_is_readable_by_its_owner_alone(made):
    assert os.stat(os.path.join(made, tls.KEY)).st_mode & 0o077 == 0


def test_running_it_again_keeps_a_certificate_that_still_fits(tmp_path, capsys):
    """A new certificate would have to be trusted all over again on every device."""
    directory = str(tmp_path)
    tls.main(["--dir", directory, "mcp.example"])
    first = capsys.readouterr().out
    assert first.startswith("-----BEGIN CERTIFICATE-----") and "PRIVATE" not in first
    tls.main(["--dir", directory, "mcp.example"])
    assert capsys.readouterr().out == first


def test_leaving_a_name_out_keeps_the_certificate(tmp_path, capsys):
    """Running the installer again to change a port must not cost every device its trust."""
    directory = str(tmp_path)
    tls.main(["--dir", directory, "mcp.example"])
    first = capsys.readouterr().out
    tls.main(["--dir", directory])
    assert capsys.readouterr().out == first
    assert _handshake(directory, "mcp.example")


def test_a_new_name_means_a_new_certificate_that_keeps_the_old_names(tmp_path, capsys):
    directory = str(tmp_path)
    tls.main(["--dir", directory, "mcp.example"])
    first = capsys.readouterr().out
    tls.main(["--dir", directory, "another.example"])
    assert capsys.readouterr().out != first
    assert _handshake(directory, "another.example") and _handshake(directory, "mcp.example")


def test_a_renewal_keeps_the_names(tmp_path, monkeypatch, capsys):
    directory = str(tmp_path)
    tls.main(["--dir", directory, "mcp.example"])
    monkeypatch.setattr(tls, "RENEW_DAYS", tls.DAYS + 1)      # every certificate reads as near its end
    tls.main(["--dir", directory])
    assert "mcp.example" in tls.covered(directory)


def test_one_near_its_end_is_replaced(tmp_path, monkeypatch):
    directory = str(tmp_path)
    names = list(tls.ALWAYS)
    tls.make(directory, names)
    assert tls.fits(directory, names)
    monkeypatch.setattr(tls, "RENEW_DAYS", tls.DAYS + 1)
    assert not tls.fits(directory, names)


def test_the_manager_serves_these_files_as_they_are(made):
    boot = {"tls_cert": os.path.join(made, tls.CERT), "tls_key": os.path.join(made, tls.KEY)}
    assert config.tls_kwargs(boot) == {"ssl_certfile": boot["tls_cert"], "ssl_keyfile": boot["tls_key"]}


# ---- localhost until someone says otherwise --------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_docker_publishes_on_localhost_unless_told_otherwise():
    with open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8") as f:
        assert '"${MCPM_PUBLISH:-127.0.0.1}:${MCPM_PORT:-8899}:8899"' in f.read()


def test_a_bare_install_binds_localhost_unless_told_otherwise(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPM_HOST", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("boot: {}\n", encoding="utf-8")
    boot, _ = config.load(str(path))
    assert boot["host"] == "127.0.0.1"


def test_the_shipped_config_names_nobodys_servers():
    import yaml
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        shipped = yaml.safe_load(f)
    assert shipped["boot"]["host"] == "127.0.0.1"
    assert not shipped.get("upstreams") and not shipped.get("keys")
    assert {g["name"] for g in shipped["groups"]} == {"default", "full"}
