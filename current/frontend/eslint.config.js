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

  // ---- Tailwind v4 gradient utilities ----------------------------------------
  // Tailwind v4 renamed the gradient utilities: `bg-gradient-to-*` (v3) became
  // `bg-linear-to-*`. The old names still pass typecheck, build, and every
  // test — they simply emit NO CSS under v4, so the element silently loses its
  // gradient. Until now the only guard was a comment in
  // `src/components/ScrollFade.tsx`; a comment is not an invariant, so this
  // rule holds it mechanically. AST-based (string literals + template chunks),
  // so prose in comments — like ScrollFade's — doesn't trip it.
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "Literal[value=/bg-gradient-to-/]",
          message:
            "Tailwind v4 renamed `bg-gradient-to-*` to `bg-linear-to-*`; the " +
            "v3 name compiles but emits no CSS, so the gradient silently " +
            "disappears. Use `bg-linear-to-*`.",
        },
        {
          selector: "TemplateElement[value.raw=/bg-gradient-to-/]",
          message:
            "Tailwind v4 renamed `bg-gradient-to-*` to `bg-linear-to-*`; the " +
            "v3 name compiles but emits no CSS, so the gradient silently " +
            "disappears. Use `bg-linear-to-*`.",
        },
        {
          selector: "JSXText[value=/bg-gradient-to-/]",
          message:
            "Tailwind v4 renamed `bg-gradient-to-*` to `bg-linear-to-*`; the " +
            "v3 name compiles but emits no CSS, so the gradient silently " +
            "disappears. Use `bg-linear-to-*`.",
        },
      ],
    },
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
    // EMPTY — A3's ratchet reached zero at the Epic 12-D seal. The invariant
    // `client.ts` states about itself is now enforced without exception: no
    // file under components/ or views/ imports the transport module. Adding an
    // entry here is not a migration step, it is a regression;
    // `src/test/apiClientBoundary.test.ts` fails if this list is ever non-empty.
    ignores: [],
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
