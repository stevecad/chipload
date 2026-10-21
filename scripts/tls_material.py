"""Bounded certificate inspection and offline TLS chain/hostname verification."""

import ssl
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

FILES = (
    "certificates/api.crt",
    "certificates/api.key",
    "certificates/identity.crt",
    "certificates/identity.key",
    "identity-ca.pem",
)
MAX_BYTES = 1024 * 1024


def read(path):
    with path.open("rb") as source:
        value = source.read(MAX_BYTES + 1)
    if len(value) > MAX_BYTES:
        raise ValueError("TLS material exceeds its size limit")
    return value


def certificates(path):
    result = x509.load_pem_x509_certificates(read(path))
    if not 1 <= len(result) <= 32:
        raise ValueError("TLS certificate count exceeds its limit")
    return result


def metadata(certificate):
    return {
        "sha256": certificate.fingerprint(hashes.SHA256()).hex(),
        "valid_from": certificate.not_valid_before_utc.isoformat(),
        "expires_at": certificate.not_valid_after_utc.isoformat(),
    }


def verify(root, role, hostname):
    # Reject encrypted keys without allowing OpenSSL to prompt for a password.
    serialization.load_pem_private_key(read(root / f"certificates/{role}.key"), password=None)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(
        root / f"certificates/{role}.crt", root / f"certificates/{role}.key"
    )
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    client_context.load_verify_locations(cafile=root / "identity-ca.pem")
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    peer_in, peer_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(incoming, outgoing, server_hostname=hostname)
    server = server_context.wrap_bio(peer_in, peer_out, server_side=True)
    completed = [False, False]
    # Perform a real TLS handshake entirely in memory. No listener, credentials,
    # network request or environment-selected TLS key log is involved.
    for _ in range(20):
        for index, connection, output, target in (
            (0, client, outgoing, peer_in),
            (1, server, peer_out, incoming),
        ):
            if not completed[index]:
                try:
                    connection.do_handshake()
                    completed[index] = True
                except ssl.SSLWantReadError:
                    pass
            data = output.read()
            if data:
                target.write(data)
        if all(completed):
            return
    raise ValueError("offline TLS handshake did not complete")


def inspect(root, values):
    chains = {role: certificates(root / f"certificates/{role}.crt") for role in ("api", "identity")}
    return {
        "leaves": {
            role: {"hostname": values[role + "_host"], **metadata(chain[0])}
            for role, chain in chains.items()
        },
        "chains": {role: [metadata(cert) for cert in chain] for role, chain in chains.items()},
        "trust": [metadata(cert) for cert in certificates(root / "identity-ca.pem")],
    }


def validate(root, values, *, minimum_days=0):
    if (
        not isinstance(minimum_days, int)
        or isinstance(minimum_days, bool)
        or not 0 <= minimum_days <= 365
    ):
        raise ValueError("minimum certificate validity must be between 0 and 365 days")
    report = inspect(root, values)
    minimum = datetime.now(timezone.utc) + timedelta(days=minimum_days)
    for role, chain in report["chains"].items():
        if any(datetime.fromisoformat(details["expires_at"]) <= minimum for details in chain):
            raise ValueError(f"{role} certificate has insufficient remaining validity")
        verify(root, role, values[role + "_host"])
    return report
