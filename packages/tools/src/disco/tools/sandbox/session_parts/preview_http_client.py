"""One-shot loopback HTTP client, executed inside the selected sandbox.

Only stdlib imports: this source is carried over the existing exec channel.
The parent validates the port and frames all input as data, never shell source.
"""

import base64
import http.client
import json
import sys


def main() -> None:
    source = sys.argv[1]
    if source.startswith("@"):
        with open(source[1:], encoding="ascii") as staged:
            source = staged.read(3 * 1024 * 1024)
    request = json.loads(base64.b64decode(source, validate=True))
    connection = http.client.HTTPConnection("127.0.0.1", request["port"], timeout=15)
    try:
        connection.putrequest(request["method"], request["target"], skip_accept_encoding=True)
        for name, value in request["headers"]:
            connection.putheader(name, value)
        body = base64.b64decode(request["body"], validate=True)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        headers = response.getheaders()
        if sum(len(k) + len(v) for k, v in headers) > 65536:
            raise ValueError("response headers too large")
        expected_length = response.length
        if expected_length is not None and expected_length > 25 * 1024 * 1024:
            raise ValueError("response body too large")
        content = response.read(25 * 1024 * 1024 + 1)
        if expected_length is not None and len(content) != expected_length:
            raise ValueError("incomplete HTTP response")
        if len(content) > 25 * 1024 * 1024:
            raise ValueError("response body too large")
        print(json.dumps([response.status, headers, base64.b64encode(content).decode("ascii")]))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
