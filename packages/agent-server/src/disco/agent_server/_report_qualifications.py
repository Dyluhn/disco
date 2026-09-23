"""Evidence and editorial qualifications carried by a stored report.

These are measurements and unresolved findings, not a second verdict. Keep
them in downloads even when a report was produced before this renderer.
"""

from disco.core import ReportEvent


def report_qualifications(report: ReportEvent) -> list[str]:
    meta = report.meta
    notes: list[str] = []
    outcome = meta.get("review_outcome")
    if outcome == "unavailable":
        notes.append("The model's editorial review was unavailable for this run.")
    elif outcome == "incomplete":
        notes.append("Review found unresolved issues in this report. See the findings below.")
    counts = meta.get("grounding_counts")
    keys = ("supported", "contradicted", "unresolved", "unavailable")
    if isinstance(counts, dict) and all(
        type(counts.get(key)) is int and counts[key] >= 0 for key in keys
    ):
        notes.append(
            f"Automated evidence check: {counts['supported']} supported, "
            f"{counts['contradicted']} possible contradictions, "
            f"{counts['unresolved']} unresolved, {counts['unavailable']} not checked."
        )
        notes.append(
            "Unresolved means the checker found no source excerpt to match the sentence "
            "against — it is not a contradiction."
        )
    unverified = meta.get("unverified_sentences")
    if unverified is None:
        unverified = meta.get("residual_deficiencies")
    groups = (
        (meta.get("review_notes"), ""),
        (unverified, "Unverified: "),
        (meta.get("untested_angles"), "Not researched: "),
    )
    for values, prefix in groups:
        if isinstance(values, list):
            notes.extend(prefix + item for item in values if isinstance(item, str) and item.strip())
    return notes
