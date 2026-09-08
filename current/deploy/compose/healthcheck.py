"""Container healthcheck: GET the URL in argv[1], exit 0 when it answers.

A one-argument script rather than `python -c "...urlopen('http://...')"` on
purpose. Compose implementations that translate the exec-form `test:` into a
single shell string (podman-compose 1.0.6, stock on Ubuntu 24.04) re-quote each
argument, so any quote, semicolon or parenthesis inside an inline program turns
the healthcheck into a broken shell command — the container then reports
`unhealthy` forever while the service itself is answering normally. Every
argument here is free of shell metacharacters, so the translation survives.
"""

import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    sys.exit(0 if response.status < 400 else 1)
