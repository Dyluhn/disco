import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    },
  },
  {
    files: ["**/*.test.{ts,tsx}", "src/test/**"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },

  // ---- api/client funnel (Amendment A3) --------------------------------------
  // `src/api/client.ts` states its own invariant in its header: it is "the ONE
  // place that talks to the backend; components never import this — only the api
  // modules + hooks do". Nothing enforced it, so 36 files under components/ and
  // views/ now import it directly, spreading transport concerns through the view
  // layer and making the data-access seam unfindable.
  //
  // The rule restores the invariant. The list below started as the drift
  // measured at Epic 12-A open (compiler-derived from the pinned tsc, not grep)
  // and is a RATCHET: it may only shrink. Each PKG-12 sub-epic migrates the
  // files it touches onto `api/`/`hooks/` and deletes those entries; A3
  // requires it to reach zero by Epic 12 close. Do not add an entry — if new
  // code needs the backend, it needs an api module.
  //
  // 36 at Epic 12-A open; 33 after Epic 12-B migrated the three files it
  // decomposed (`BuildSurface.tsx`, `build/AgentCanvas.tsx`,
  // `build/DeliverablePanel.tsx`) behind `api/agent.ts`, `api/canvas.ts` and
  // `api/deliverables.ts`; 28 after Epic 12-C migrated the five it touched
  // (`canvas/PreviewPane.tsx` + its versions spec, `research/NeedMoreCard.tsx`,
  // and the `ResearchSurface.draft` / `DeepResearchSurface.attach` specs) behind
  // `api/preview.ts` and `api/deepResearch.ts`. The two specs kept their exact
  // `agentLive` interception via `vi.mock`, which needs no static import.
  // `src/test/apiClientBoundary.test.ts` holds both
  // prongs: the ceiling, and that every remaining entry still imports the
  // client — so a migration is only complete once its entry is gone.
  {
    files: ["src/components/**/*.{ts,tsx}", "src/views/**/*.{ts,tsx}"],
    ignores: [
      "src/components/DemoDataBadge.tsx",
      "src/components/PairingGate.tsx",
      "src/components/PreviewLaunchFrame.tsx",
      "src/components/build/ActivityFeed.tsx",
      "src/components/build/AgentCanvas.live.test.tsx",
      "src/components/build/DeckEditorPane.test.tsx",
      "src/components/build/DeckEditorPane.tsx",
      "src/components/build/DeckExportBar.tsx",
      "src/components/build/ExecutionCanvas.preview.test.tsx",
      "src/components/build/ImportProjectDialog.tsx",
      "src/components/settings/AudioSection.tsx",
      "src/components/settings/DataSourcesSection.test.tsx",
      "src/components/settings/DataSourcesSection.tsx",
      "src/components/settings/ImageGenSection.test.tsx",
      "src/components/settings/ImageGenSection.tsx",
      "src/components/settings/McpSection.approval.test.tsx",
      "src/components/settings/McpSection.live.test.tsx",
      "src/components/settings/McpSection.tsx",
      "src/components/settings/ModelCatalogue.tsx",
      "src/components/settings/OpenRouterSection.tsx",
      "src/components/settings/ProjectStorageSection.test.tsx",
      "src/components/settings/ProviderKeysSection.test.tsx",
      "src/components/settings/ProviderKeysSection.tsx",
      "src/components/settings/ProvidersSection.test.tsx",
      "src/components/settings/ProvidersSection.tsx",
      "src/components/settings/ScheduleSection.timezone.test.tsx",
      "src/components/settings/ScheduleSection.tsx",
      "src/views/history.test.tsx",
    ],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "@/api/client",
              message:
                "components/ and views/ must not talk to the backend directly. " +
                "Call an api module (src/api/*.ts) or a hook (src/hooks/*.ts); " +
                "client.ts is the transport seam those own.",
            },
          ],
        },
      ],
    },
  },
);
