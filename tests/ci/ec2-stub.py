"""Return one already-running CI instance; never contact AWS."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        query = parse_qs(self.rfile.read(size).decode("utf-8"))
        if query.get("Action") != ["DescribeInstances"]:
            self.send_error(400, "Only DescribeInstances is allowed in CI")
            return

        body = b"""<?xml version="1.0" encoding="UTF-8"?>
<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <requestId>ci-only</requestId>
  <reservationSet><item><reservationId>r-ci</reservationId><instancesSet><item>
    <instanceId>i-ci-only</instanceId>
    <instanceState><code>16</code><name>running</name></instanceState>
  </item></instancesSet></item></reservationSet>
</DescribeInstancesResponse>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
