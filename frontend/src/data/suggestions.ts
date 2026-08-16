export type SuggestionSurface = "search" | "deep_research" | "build" | "agent";

export interface Suggestion {
  id: string;
  text: string;
}

export const SUGGESTION_SAMPLE_MIN = 4;
export const SUGGESTION_SAMPLE_MAX = 6;

const RESEARCH_PROMPTS = [
  "What changed in US nuclear power permitting since 2020?",
  "Compare sodium-ion and LFP batteries for grid storage",
  "Why did the Roman concrete recipe survive seawater so well?",
  "Map the strongest evidence for and against GLP-1 use in adolescents",
  "Trace how NASA's Artemis schedule has shifted and why",
  "What are the best explanations for the Fermi paradox?",
  "Summarize current evidence on microplastics in human blood and fertility",
  "Compare open-source vector databases for a small RAG system",
  "How do ranked-choice voting tabulation methods differ in practice?",
  "What caused the 1970s stagflation, and what parallels exist now?",
  "Find the most cited critiques of carbon offsets and how registries responded",
  "Explain the economics of desalination for drought-prone cities",
  "Which countries are expanding geothermal energy fastest, and what policies helped?",
  "What do recent studies say about four-day work weeks and productivity?",
  "Build a timeline of CRISPR therapy approvals and safety concerns",
  "Compare the EU AI Act with US state AI laws for model developers",
  "What evidence links sleep timing, not duration, to metabolic risk?",
  "How has the definition of ultra-processed food changed in nutrition research?",
  "What are the failure modes of school voucher studies?",
  "What is known about PFAS removal in municipal water treatment?",
  "Explain why transformer inference gets memory-bound",
  "Compare WebGPU, WebGL, and WASM for browser ML apps",
  "What is the current state of fusion pilot plants and credible timelines?",
  "Which wildfire mitigation programs have measurable results?",
  "How do housing supply reforms in Auckland and Minneapolis compare?",
  "What are the strongest arguments for and against congestion pricing?",
  "Summarize the research on bilingual education outcomes",
  "What happened to local news economics after Facebook referral traffic dropped?",
  "How do scientists estimate dinosaur body mass from fossils?",
  "Compare Medicare Advantage denial rates across studies and regulators",
  "What is the evidence that urban trees reduce heat-related mortality?",
  "Explain the Taiwan semiconductor supply chain chokepoints",
  "Which climate attribution methods are most trusted for extreme weather?",
  "What can ancient DNA tell us about Indo-European language spread?",
  "Compare the safety records of autonomous driving deployments",
  "What are the known tradeoffs of retrieval augmented generation vs long context?",
  "Trace the history of Section 230 reform proposals and their likely effects",
  "What makes some vaccines require boosters while others last decades?",
  "How are stablecoins regulated across the US, EU, and Singapore?",
  "Which public transit fare policies increased ridership after 2020?",
  "Explain how randomized controlled trials can mislead in education policy",
  "What did the replication crisis change in psychology methods?",
  "Compare different explanations for the Bronze Age collapse",
  "What is the current evidence on creatine for cognition?",
  "How do antitrust cases define consumer harm in zero-price markets?",
  "What are the economics of vertical farming after recent bankruptcies?",
  "How do ocean alkalinity enhancement proposals measure permanence and risk?",
  "Review evidence on phonics mandates and reading outcomes by state",
  "What makes a source credible in fast-moving public health research?",
  "Compare LLM benchmark contamination detection methods",
  "How did pandemic-era remote work affect downtown tax bases?",
  "What are the leading theories for why endometriosis is underdiagnosed?",
] as const;

const DEEP_RESEARCH_PROMPTS = [
  "Produce a market map of solid-state battery commercialization with source tiers",
  "Build an evidence review of GLP-1 side effects and long-term adherence",
  "Trace how US data-center power demand forecasts changed from 2020 to 2026",
  "Compare national AI safety institutes by mandate, budget, and enforcement power",
  "Investigate why offshore wind projects were cancelled or renegotiated after 2022",
  "Assess the evidence for universal basic income pilots across countries",
  "Map the scientific dispute over ocean iron fertilization risks and governance",
  "Write a sourced briefing on water scarcity in the Colorado River basin",
  "Compare the chip export-control strategies of the US, Netherlands, Japan, and China",
  "Review recent evidence on tutoring, attendance, and learning recovery after COVID",
  "Analyze how insurance markets are responding to wildfire and flood risk",
  "Trace the evolution of mRNA cancer vaccine trials and open questions",
  "Compare urban heat mitigation policies across Phoenix, Singapore, and Paris",
  "Review what is known about antimicrobial resistance in wastewater surveillance",
  "Build a timeline of stablecoin regulation and major enforcement actions",
  "Assess whether direct air capture cost curves look credible",
  "Compare open-source LLM licensing terms and business risks",
  "Review the evidence that social media bans affect teen mental health",
  "Trace the causes and policy response to pharmacy deserts in the US",
  "Analyze post-2020 transit ridership recovery across large metro systems",
  "Compare food labeling systems and their effect on consumer behavior",
  "Investigate the current limits of organoid research and clinical translation",
  "Review the evidence for heat pumps in cold climates",
  "Map the main theories and evidence around the Bronze Age collapse",
] as const;

const BUILD_PROMPTS = [
  "a kanban board with drag and drop",
  "a landing page for a coffee roastery",
  "a 2048 clone",
  "an invoice tracker with searchable customers and payment status",
  "a habit tracker with weekly streak charts",
  "a markdown notes app with local search",
  "a personal finance dashboard from CSV uploads",
  "a recipe scaler with unit conversions",
  "a browser-based pomodoro timer with sessions",
  "a portfolio site for a documentary photographer",
  "a pricing page with monthly and annual toggles",
  "a support ticket triage dashboard",
  "a SQLite-backed bookmarks manager",
  "a workout planner with printable routines",
  "a color palette generator with exportable CSS variables",
  "a tiny CRM for freelance leads",
  "a meeting agenda app with timers and reusable templates",
  "a real-time typing speed test",
  "a weather dashboard using a public API",
  "a launch checklist app with reusable templates",
  "a changelog page for a SaaS product",
  "a classroom quiz game with score tracking",
  "a responsive restaurant menu with filters",
  "a file renamer preview tool",
  "a status page with incident history",
  "a charting dashboard for sales CSVs",
  "a chess opening trainer",
  "a password strength explainer",
  "a docs site with search and dark mode",
  "a drag-and-drop image gallery organizer",
  "a simple inventory scanner UI",
  "a certificate designer with live preview and printable layouts",
] as const;

const AGENT_PROMPTS = [
  "summarize the files I upload into a brief",
  "fill this form from my notes",
  "browse example.com and extract pricing",
  "turn my meeting transcript into decisions and next steps",
  "compare these two contracts for risky clauses",
  "organize this folder into a cleaner structure",
  "draft a reply using the facts in this thread",
  "find broken links on this site and list fixes",
  "convert these screenshots into a QA bug report",
  "research vendors and make a shortlist table",
  "pull action items out of these project docs",
  "check whether this CSV has duplicate customer rows",
  "rewrite this policy in plain language",
  "prepare a travel options brief from my constraints",
  "extract deadlines from these PDFs",
  "browse the docs and tell me the setup steps",
  "turn these notes into a slide outline",
  "find accessibility issues on this page",
  "collect contact emails from these pages",
  "validate these claims against source links",
  "make a release checklist from this changelog",
  "separate invoices from receipts in uploaded files",
  "compare the uploaded resumes against this role",
  "turn my rough spec into implementation tickets",
  "audit this repo for unused files",
  "browse competitor sites and summarize positioning",
  "merge these bullet notes into one clean memo",
  "check a spreadsheet for missing values",
  "collect public API limits into a table",
  "write a customer support macro from these examples",
  "prepare a week plan from my todos",
  "extract product names and prices from a catalog PDF",
] as const;

function suggestions(prefix: SuggestionSurface, prompts: readonly string[]): Suggestion[] {
  return prompts.map((text, index) => ({ id: `${prefix}-${index + 1}`, text }));
}

export const SUGGESTION_POOLS = {
  search: suggestions("search", RESEARCH_PROMPTS),
  deep_research: suggestions("deep_research", DEEP_RESEARCH_PROMPTS),
  build: suggestions("build", BUILD_PROMPTS),
  agent: suggestions("agent", AGENT_PROMPTS),
} satisfies Record<SuggestionSurface, readonly Suggestion[]>;

type RandomSource = () => number;

export function sampleSuggestions(
  pool: readonly Suggestion[],
  count: number,
  random: RandomSource = Math.random,
): Suggestion[] {
  const shuffled = [...pool];
  for (let i = shuffled.length - 1; i > 0; i -= 1) {
    const j = Math.floor(random() * (i + 1));
    [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
  }
  return shuffled.slice(0, Math.min(Math.max(count, 0), shuffled.length));
}

export function getSuggestionPool(surface: SuggestionSurface): readonly Suggestion[] {
  return SUGGESTION_POOLS[surface];
}

function randomSampleCount(random: RandomSource): number {
  const range = SUGGESTION_SAMPLE_MAX - SUGGESTION_SAMPLE_MIN + 1;
  return SUGGESTION_SAMPLE_MIN + Math.floor(random() * range);
}

// Curated fallback provider; the splash swaps to generated suggestions when available.
export function getSuggestions(surface: SuggestionSurface): Suggestion[] {
  return sampleSuggestions(getSuggestionPool(surface), randomSampleCount(Math.random));
}
