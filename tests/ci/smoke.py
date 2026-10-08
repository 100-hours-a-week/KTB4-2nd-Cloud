"""One authenticated photo journey against the three release images in isolated CI."""

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.request
import uuid
import zlib
from http.cookies import SimpleCookie

BASE = "http://127.0.0.1:18080/api"
JWT_SECRET = b"ci-only-secret-key-with-at-least-32-bytes"
SID = os.environ.get("CI_AUTH_SID", "00000000-0000-4000-8000-000000000001")


def request(url, method="GET", data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return response.status, response.headers, json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        body = error.read(4000).decode("utf-8", errors="replace")
        raise AssertionError(f"{method} {url}: HTTP {error.code}: {body}") from error


def jwt():
    now = int(time.time())

    def encode(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).rstrip(b"=")

    signing_input = b".".join((
        encode({"alg": "HS256", "typ": "JWT"}),
        encode({"iss": "http://localhost:18080", "aud": ["yeodam-api"],
                "iat": now, "exp": now + 1800, "sub": "900001", "sid": SID}),
    ))
    signature = base64.urlsafe_b64encode(
        hmac.new(JWT_SECRET, signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    return (signing_input + b"." + signature).decode()


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
        )

    width = height = 64
    pixels = b"".join(b"\x00" + b"\x80\x30\x20" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels))
            + chunk(b"IEND", b""))


def multipart(photo):
    boundary = "yeodam-ci-" + uuid.uuid4().hex
    body = bytearray()
    for name, value, filename, content_type in (
        ("attachments[]", photo, "ci.png", "image/png"),
        ("batchNo", b"1", None, None),
        ("totalAttachmentCount", b"1", None, None),
        ("complete", b"true", None, None),
    ):
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"".encode())
        if filename:
            body.extend(f"; filename=\"{filename}\"".encode())
        body.extend(b"\r\n")
        if content_type:
            body.extend(f"Content-Type: {content_type}\r\n".encode())
        body.extend(b"\r\n" + value + b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def main():
    assert request("http://127.0.0.1:13000/api/health")[0] == 200
    assert request("http://127.0.0.1:18000/health")[2]["status"] == "ok"
    assert request(BASE + "/actuator/health")[0] == 200

    token = jwt()
    csrf_status, csrf_headers, csrf_body = request(BASE + "/auth/csrf")
    assert csrf_status == 200
    cookies = SimpleCookie()
    for header in csrf_headers.get_all("Set-Cookie", []):
        cookies.load(header)
    context = cookies["CSRF_CONTEXT"].value
    headers = {
        "Cookie": f"accessToken={token}; CSRF_CONTEXT={context}",
        "X-CSRF-TOKEN": csrf_body["data"]["token"],
    }
    assert request(BASE + "/users/me", headers=headers)[0] == 200

    trip_body = json.dumps({
        "tripName": "CI 여행", "startDate": "2026-09-10",
        "endDate": "2026-09-11", "regionCodes": ["50110"],
    }).encode()
    created, _, result = request(
        BASE + "/trips", "POST", trip_body,
        {**headers, "Content-Type": "application/json"},
    )
    assert created == 201, result
    trip_id = result["data"]["tripId"]

    photo_body, content_type = multipart(png())
    processed, _, result = request(
        BASE + f"/trips/{trip_id}/initial-attachments", "POST", photo_body,
        {**headers, "Content-Type": content_type}, timeout=240,
    )
    assert processed == 200, result
    assert result["data"]["status"] == "COMPLETED", result

    viewed, _, result = request(BASE + f"/trips/{trip_id}", headers=headers)
    assert viewed == 200, result
    assert result["data"]["tripId"] == trip_id, result
    # CI PNG에는 위치 정보가 없어 AI가 미분류하며, 조회 수는 ACTIVE 사진만 센다.
    assert result["data"]["attachmentCount"] == 0, result
    print("FE Health 및 BE→AI 사진 접수·처리·조회 계약 통과")


if __name__ == "__main__":
    main()
