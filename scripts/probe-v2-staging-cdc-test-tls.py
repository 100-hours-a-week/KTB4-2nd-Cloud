#!/usr/bin/env python3
"""Verify MySQL's SSLRequest handshake from the private Kafka host without logging credentials."""

import argparse
import hashlib
import socket
import ssl
import struct
import subprocess
import tempfile
from pathlib import Path

CLIENT_PROTOCOL_41 = 0x00000200
CLIENT_SSL = 0x00000800
CLIENT_SECURE_CONNECTION = 0x00008000


def read_exact(connection: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        part = connection.recv(length - len(result))
        if not part:
            raise RuntimeError("MySQL closed the handshake")
        result.extend(part)
    return bytes(result)


def verify_mysql_tls(host: str, ca_file: Path) -> tuple[str, str]:
    with socket.create_connection((host, 3306), timeout=5) as connection:
        connection.settimeout(5)
        header = read_exact(connection, 4)
        size = int.from_bytes(header[:3], "little")
        payload = read_exact(connection, size)
        if header[3] != 0 or not payload or payload[0] != 10:
            raise RuntimeError("Unexpected MySQL initial handshake")
        server_version_end = payload.index(b"\0", 1)
        capability_offset = server_version_end + 1 + 4 + 8 + 1
        if len(payload) < capability_offset + 2:
            raise RuntimeError("Incomplete MySQL capabilities")
        server_capabilities = int.from_bytes(payload[capability_offset:capability_offset + 2], "little")
        if not server_capabilities & CLIENT_SSL:
            raise RuntimeError("MySQL server did not advertise TLS")
        flags = CLIENT_PROTOCOL_41 | CLIENT_SSL | CLIENT_SECURE_CONNECTION
        request = struct.pack("<IIB23s", flags, 16 * 1024 * 1024, 45, b"")
        connection.sendall(len(request).to_bytes(3, "little") + b"\x01" + request)
        context = ssl.create_default_context(cafile=str(ca_file))
        # The generated MySQL server certificate has no private DNS SAN.
        # CA verification still rejects a substituted/untrusted server.
        context.check_hostname = False
        with context.wrap_socket(connection, server_hostname=host) as secure:
            certificate = secure.getpeercert(binary_form=True)
            return secure.version() or "unknown", hashlib.sha256(certificate).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--ca-parameter", required=True)
    args = parser.parse_args()
    if not args.ca_parameter.startswith("/yeodam/v2/staging/cdc-test/"):
        parser.error("Expected the dedicated B1 CA parameter")
    result = subprocess.run(
        ["aws", "ssm", "get-parameter", "--region", "ap-northeast-2", "--name", args.ca_parameter,
         "--query", "Parameter.Value", "--output", "text"],
        text=True, capture_output=True, check=True,
    )
    with tempfile.TemporaryDirectory(prefix="yeodam-cdc-tls-", dir="/run") as directory:
        ca_file = Path(directory) / "ca.pem"
        ca_file.write_text(result.stdout)
        ca_file.chmod(0o600)
        version, fingerprint = verify_mysql_tls(args.host, ca_file)
    print(f"host={args.host} tls={version} cert_sha256={fingerprint}")


if __name__ == "__main__":
    main()
