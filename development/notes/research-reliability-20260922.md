# Research reliability and report qualifications

Owner authorization: investigate and fix the stalled research paths, preserve
provider-neutral behavior, validate on a clean blackbox VM, reconcile CI authority,
merge, publish images, and verify installation. The 2026-09-23 follow-up explicitly
includes runtime, report quality, and PDF/Markdown qualification follow-ups.

## Behavior

Request controls are explicit validated configuration. No model, provider label,
or hostname selects behavior. Optional request overlays rejected before output
receive one request-local retry using the original body. Reasoning remains
separate from visible output across streaming and buffered protocol paths.

Source reading preserves full evidence and checks quotation offsets locally.
An empty visible reply triggers a bounded compact retry; Stop/checkpoints retain
spent calls, source identity, completed chunks, and the selected retry strategy.
Review uses the existing output ceiling for an empty length-limited reply and
reserves two final decisions in newly started research runs. Legacy serialized
budgets retain their original reserves. Model activity includes the final review.
There remains one scoped prose repair; unresolved findings are disclosed.

Drafting and review explicitly preserve causal direction, affected participants,
and applicability conditions. Citation presence is not proof of causation.
Automated/model review is fallible: a replay caught one causal inference that a
different reviewer accepted. These instructions improve the general check;
they do not introduce subject-specific rules or promise perfect reports.

Both server downloads and offline Markdown retain stored evidence counts,
editorial review status, unresolved findings, and unresearched angles. Metadata
is escaped as prose, citations keep the report's numbered source mapping, and
legacy metadata remains readable. Missing measurements do not become a claim
of a clean review. Existing exports without qualification metadata retain their
content. PDF tests inspect actual generated document text.

## Validation record

Prior final-review recovery validation: 6,832 core/retrieval/app-server tests
passed; GLM returned a usable complete-workload verdict after checkpoint/resume
with the existing 48,000-token ceiling. A cached repeat made zero model calls.
The same saved review workload completed with DeepSeek. No model-specific
runtime selection was used. The earlier complete GLM Quick run took 4,318 seconds
behind a relay that deliberately rejected the optional control; it is not a
measurement of normal-endpoint configured performance.

The 2026-09-23 source change is being qualified with a fresh full GLM run against
the normal endpoint, the Python and frontend suites, architecture gates, actual
export downloads, and subsequent published-image installation. Exact source
identities, timestamps, results, and release digests are recorded externally in
`/var/home/dylan/AI-Work/disco-blackbox-validation-20260922/` and the PR. Pending
checks here must not be read as passed.

## Authority transition

The source commit preserves its parent's public API and test authority bytes.
Derived public API rows name exact additions and old/new member or declaration
signatures; test inventory regeneration proves preservation of collected tests
and records new tests. The landing is a sibling of that retained source commit,
differing only in the authorized derived files. Governance implementation,
protected contracts, collector scopes, and test execution requirements are not
relaxed to accommodate this change.
