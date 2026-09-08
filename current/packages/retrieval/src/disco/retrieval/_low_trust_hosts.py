"""Discovery's default deny for hosts that are unreviewed by construction.

Blind judges read nine research runs (2026-09-04; 778 cited passages) and docked
citation support every time a report rested a load-bearing claim on one of these
hosts — "uses a Reddit thread and an AI-generated wiki for load-bearing claims",
"leans on LinkedIn posts", "content farms". Seven percent of every citation came
from them: academia.edu (25), linkedin.com (14), reddit.com (11), and a tail of
Facebook, Grokipedia and platform blogs.

Ranking cannot fix this. A Reddit thread can be the best lexical AND semantic
match a query has and still be someone's unsourced opinion — the defect is in
how the page came to exist, not in how well it answers. So the wall sits in
discovery, where the URL is first seen: the page is never fetched, never
chunked, and the passage a report could have cited never exists.

The wall is a default, not a law. A caller that names one of these hosts in
`domains_allow` gets it back, because a question ABOUT Reddit needs Reddit.

Lives outside `live.py` because that module is already over the 700-logical-line
budget; the SearXNG provider is the only caller.
"""

from __future__ import annotations

from .models import SearchHit
from .url_policy import host_matches_domain, url_allowed

# Hosts whose pages are user-generated or unreviewed BY CONSTRUCTION: anyone may
# post, and nothing stands between the post and the reader. This is a list of
# publishing models, not a reputation ranking — a weak peer-reviewed journal is
# not here, and a well-researched Substack still is. Every entry is a
# REGISTRABLE domain, matched by `url_policy.host_matches_domain`, so the
# subdomains ride along for free: `old.reddit.com`, `www.linkedin.com` and
# `sub.substack.com` each match their entry. That match is a plain suffix-label
# comparison rather than a public-suffix lookup — the deliberate simplification
# that keeps a PSL dependency out of the tree, and the reason every entry here
# must be a registrable domain and never a bare multi-label suffix.
LOW_TRUST_HOSTS = frozenset(
    {
        "academia.edu",
        "blogspot.com",
        "facebook.com",
        "grokipedia.com",
        "instagram.com",
        "linkedin.com",
        "medium.com",
        "pinterest.com",
        "quora.com",
        "reddit.com",
        "substack.com",
        "tiktok.com",
        "twitter.com",
        "wordpress.com",
        "x.com",
        "youtube.com",
    }
)


def drop_low_trust(
    hits: list[SearchHit], domains_allow: frozenset[str] | None
) -> tuple[list[SearchHit], int]:
    """The hits worth keeping, and how many the wall removed.

    The count is returned rather than logged here so the provider can put it in
    the diagnostic it already returns: a wall nobody can see removing sources is
    indistinguishable from an engine that stopped finding them.
    """
    denied = LOW_TRUST_HOSTS - {
        host
        for host in LOW_TRUST_HOSTS
        if any(host_matches_domain(allowed, host) for allowed in domains_allow or ())
    }
    kept = [hit for hit in hits if url_allowed(hit.url, None, denied)]
    return kept, len(hits) - len(kept)
