# Deep Research model timeline

- 65: routed `rag_answerer` to `prov-opencode-go-muse-spark-1-2-contributor/muse-spark-1.2-contributor`
- 67: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (6115 ms)
  - Decision: First turn mapping all major angles. Targeting authoritative primary sources for each: independent productivity studies (METR, Uplevel, academic RCTs), DORA/Google reports, security research on AI-generated code vulnerabilities, and vendor claims from GitHub/Cursor/Devin to later contrast.
  - Queries: GitHub Copilot productivity claims official study; METR AI coding agent productivity randomized controlled trial; Uplevel Labs GitHub Copilot productivity study DORA
- 70: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (7323 ms)
  - Decision: Productivity angle has initial conflicting evidence. Now need to map remaining 6 angles with authoritative sources: security vulnerabilities (primary empirical studies), reliability/bug rates, DORA/Google production adoption data, and vendor claims for contrast. Will chase citations upstream next turn.
  - Queries: AI generated code security vulnerabilities empirical study Stanford NYU; DORA 2024 report Google AI impact developer productivity delivery; GitHub Copilot Cursor Devin official productivity claims 2024 2025
- 72: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (9076 ms)
  - Decision: Productivity contradiction mapped (GitHub 55% vs METR -19% vs Uplevel/DORA no gain). Security and DORA angles have placeholder sources but need empirical content. Next need to fill reliability/bug rates, security primary study (Perry et al), production adoption (Stack Overflow), economics/ROI, and vendor claims vs independent benchmarks.
  - Queries: AI generated code correctness bug rate empirical study SWE-bench; Perry et al Do Users Write More Insecure Code with AI Assistants Stanford study; Stack Overflow Developer Survey 2024 2025 AI coding tools adoption statistics
- 73: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (5973 ms)
  - Decision: Need to fill 6 open angles: reliability/bug rates, security empirical content, production adoption, economics/ROI, vendor vs independent benchmarks, criticisms. Prior evidence has placeholders for security and reliability but lacks quantitative defect rates and ROI data. Next searches target primary empirical sources for each gap.
  - Queries: SWE-bench verified AI coding agent solve rate correctness independent study; Veracode Snyk AI generated code security vulnerability rate report 2024 2025; Cognition Devin AI agent SWE-bench claims independent evaluation results
- 75: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (10584 ms)
  - Decision: Fill 5 open angles with primary sources: economics/ROI enterprise data, SWT-Bench/SWE-bench reliability defect rates, Veracode large-scale security rates, Devin vendor vs independent evaluation gap, and critical/limitation studies. Cross-validate load-bearing numbers and chase citations upstream.
  - Queries: Uplevel DORA enterprise ROI AI coding assistant cost benefit analysis original report; SWT-Bench ETH Zurich testing validating real-world bug fixes code agents original paper; Veracode 2025 GenAI Code Security Report 45 percent vulnerabilities site:veracode.com
- 77: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (10653 ms)
  - Decision: Fill 4 remaining open angles with primary sources: (1) economics/ROI enterprise data, (2) reliability defect rates beyond solve-rate (SWE-bench correctness study), (3) systematic vendor vs independent benchmark comparison, (4) critical/limitation studies on technical debt/hallucination. Chase citations upstream and cross-validate load-bearing numbers.
  - Queries: DORA ROI AI-assisted software development report economics cost benefit; Are Solved Issues in SWE-bench Really Solved Correctly empirical study arXiv; Veracode 2025 GenAI Code Security Report methodology 45 percent vulnerabilities
- 79: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (6808 ms)
  - Decision: Fill 4 remaining open angles with primary sources: (1) economics/ROI enterprise data, (2) reliability defect rates beyond solve-rate (SWE-bench correctness study), (3) systematic vendor vs independent benchmark comparison, (4) critical/limitation studies on technical debt/hallucination. Chase citations upstream and cross-validate load-bearing numbers.
  - Queries: DORA ROI AI-assisted software development report 2026 original site:dora.dev economics framework; Are Solved Issues in SWE-bench Really Solved Correctly false positive rate arXiv 2503.15223 site:arxiv.org; Cursor Copilot Devin SWE-bench Verified independent replication vs vendor claims site:arxiv.org OR site:github.com
- 81: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (7786 ms)
  - Decision: Fill 4 open angles with targeted primary-source searches: DORA ROI economics framework, SWE-bench correctness false-positive study, Epoch AI independent benchmark audit exposing vendor gap, and technical debt/hallucination criticism. Chase upstream citations and cross-validate numbers.
  - Queries: DORA ROI AI-assisted software development report 2026 original site:dora.dev economics framework; Are Solved Issues in SWE-bench Really Solved Correctly false positive rate arXiv 2503.15223 site:arxiv.org; Epoch AI independent SWE-bench Verified third-party benchmark auditing vendor gap
- 83: **research_turn** attempt 1 on `muse-spark-1.2-contributor` (7138 ms)
  - Decision: Pivoting from rejected repeat queries to rephrased primary-source searches covering the 4 open angles: DORA ROI economics without site filter, SWE-bench false-positive/overfitting analysis, technical debt/hallucination criticism, and enterprise pricing/ROI overhead. This fills economics, reliability, vendor-gap, and criticism gaps within remaining 4 source slots.
  - Queries: DORA ROI AI-assisted software development economics framework 2026; SWE-bench Verified false positive overfitting test suite empirical analysis; AI coding agents technical debt hallucination maintenance burden study
- 87: **report_draft** attempt 1 on `muse-spark-1.2-contributor` (54391 ms)
- 90: **report_review** attempt 1 on `muse-spark-1.2-contributor` (13728 ms)
- 92: **report_rework** attempt 2 on `muse-spark-1.2-contributor` (53664 ms)
- 94: **report_review** attempt 2 on `muse-spark-1.2-contributor` (13196 ms)

Full bounded, redacted inputs and outputs: `model_io.jsonl`.
