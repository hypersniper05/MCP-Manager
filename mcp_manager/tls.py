"""A certificate for this machine, made by the manager itself, so HTTPS needs no openssl or bash.

    python -m mcp_manager.tls [--dir DIR] [NAME ...]

Writes tls-cert.pem and tls-key.pem (what the manager serves) and ca.pem (what a device trusts) into
DIR, and prints ca.pem. localhost, 127.0.0.1 and ::1 are always covered; NAME adds the host names and
addresses clients will really type. Running it again keeps a certificate that already covers them,
because a new one has to be trusted all over again; a new one keeps every name the old one had, so
changing some other setting can never cost a device its trust.

The shape is a root and a server certificate, the one every trust store and TLS client accepts. A
lone self-signed certificate is not: as a CA it would let a stolen key forge any website on every
device trusting it, and without the CA flag a store that keeps only authorities may leave it out
(p11-kit's, on Fedora and Arch). So the root is made here, signs once, and its private key is never written anywhere: nothing on disk can
sign a second certificate. The price is that new names mean a new root, trusted again.
"""

import argparse
import datetime
import ipaddress
import os
import socket
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT, CERT, KEY = "ca.pem", "tls-cert.pem", "tls-key.pem"
ALWAYS = ("localhost", "127.0.0.1", "::1")
DAYS = 825        # Apple's limit for a TLS certificate: macOS and iOS refuse a longer one, trusted or not
RENEW_DAYS = 30   # a certificate this close to its end is replaced rather than kept


def _general_name(name):
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def _subject(common_name):
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MCP-Manager"),
                      x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _load(directory):
    try:
        with open(os.path.join(directory, CERT), "rb") as f:
            return x509.load_pem_x509_certificate(f.read())
    except (OSError, ValueError):
        return None


def covered(directory) -> list:
    """The names the certificate here covers, in its own order; none if there is no certificate."""
    cert = _load(directory)
    if cert is None:
        return []
    return [str(n.value) for n in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value]


def fits(directory, names) -> bool:
    """Does the certificate here cover these names, with time left on it?"""
    cert = _load(directory)
    if cert is None or not all(os.path.exists(os.path.join(directory, n)) for n in (ROOT, KEY)):
        return False
    left = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
    return {str(_general_name(n).value) for n in names} <= set(covered(directory)) \
        and left > datetime.timedelta(days=RENEW_DAYS)


def make(directory, names) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(days=1)          # a day for clocks that run behind
    end = start + datetime.timedelta(days=DAYS)       # the whole span counts toward Apple's limit
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # the date tells two roots apart in a trust store, and the host says whose it is
    root_name = _subject(f"MCP-Manager root, {socket.gethostname()}, {now:%Y-%m-%d}")
    no_usage = dict(content_commitment=False, data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False)
    root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, key_encipherment=False, key_cert_sign=True,
                                         crl_sign=True, **no_usage), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
            .sign(root_key, hashes.SHA256()))
    server = (x509.CertificateBuilder().subject_name(_subject(names[0])).issuer_name(root_name)
              .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(start).not_valid_after(end)
              .add_extension(x509.SubjectAlternativeName([_general_name(n) for n in names]), critical=False)
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, key_cert_sign=False,
                                           crl_sign=False, **no_usage), critical=True)
              # macOS and iOS refuse a server certificate without it
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
              .sign(root_key, hashes.SHA256()))
    os.makedirs(directory, exist_ok=True)
    pem = serialization.Encoding.PEM
    key = server_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    key_path = os.path.join(directory, KEY)
    if os.path.exists(key_path):
        os.remove(key_path)      # os.open's mode only applies to a file it creates
    with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT, 0o600), "wb") as f:
        f.write(key)
    for name, cert in ((CERT, server), (ROOT, root)):
        with open(os.path.join(directory, name), "wb") as f:
            f.write(cert.public_bytes(pem))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mcp_manager.tls",
                                     description="Make a certificate for this machine; prints the file to trust.")
    parser.add_argument("--dir", default="certs", help="where the files go (default: certs)")
    parser.add_argument("names", nargs="*", help="host names and addresses clients will use")
    args = parser.parse_args(argv)
    names = list(dict.fromkeys([*args.names, *ALWAYS]))
    if fits(args.dir, names):
        print(f"keeping the certificate in {args.dir}: it still covers these names", file=sys.stderr)
    else:
        names = list(dict.fromkeys([*names, *covered(args.dir)]))
        make(args.dir, names)
        print(f"wrote {CERT} and {KEY} to {args.dir}, for: {', '.join(names)}", file=sys.stderr)
    with open(os.path.join(args.dir, ROOT), encoding="ascii") as f:
        print(f.read(), end="")


if __name__ == "__main__":
    main()
