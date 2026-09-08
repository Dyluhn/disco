"""Evidence limits are subject matter, not internal process narration."""

import pytest
from disco.retrieval.deep_research._report_rulers import has_process_language
from disco.retrieval.deep_research._writer_findings import process_language_findings


@pytest.mark.parametrize(
    "sentence",
    [
        "The comparative latency magnitudes are not established in the provided evidence.",
        "The supplied evidence covers one installation; "
        "it does not establish long-term reliability.",
        "The evidence does not include an independent measurement of annual output.",
        "That is a limitation of this evidence pool, "
        "not evidence that such performance data does not exist.",
        "The evidence pool contains no quantitative measured data "
        "from cold-climate installations.",
    ],
)
def test_legitimate_evidentiary_limits_do_not_order_prose_removal(sentence):
    assert not has_process_language(sentence, sentence)
    assert process_language_findings(sentence, [("Limitations", sentence)]) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "We searched for current measurements and then reviewed the returned pages.",
        "The search process returned three sources above for this subquestion.",
        "The evidence in front of me arrived after a provider response.",
    ],
)
def test_internal_work_narration_still_receives_a_scoped_finding(sentence):
    assert has_process_language(sentence, sentence)
    findings = process_language_findings(sentence, [("Findings", sentence)])
    assert {finding.where for finding in findings} == {"executive summary", "Findings"}
    assert all(finding.rubric == "R7" for finding in findings)


def test_evidence_limitations_heading_does_not_order_a_rewrite():
    assert process_language_findings(
        "The measured efficiency varies by climate.",
        [("Evidence pool limitations", "These sources do not establish annual performance.")],
    ) == []
