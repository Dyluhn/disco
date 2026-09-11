import { AlertTriangle, Info } from "lucide-react";
import { useRef } from "react";
import type { ReactNode } from "react";
import { ScrollFadeEdges } from "@/components/ScrollFade";
import { cn } from "@/lib/cn";
import { useScrollFade } from "@/hooks/useScrollFade";
import { AudioSection } from "@/components/settings/AudioSection";
import { ChatVerbositySection } from "@/components/settings/ChatVerbositySection";
import { DataSourcesSection } from "@/components/settings/DataSourcesSection";
import { EncoderSection } from "@/components/settings/EncoderSection";
import { ImageGenSection } from "@/components/settings/ImageGenSection";
import { McpSection } from "@/components/settings/McpSection";
import { ModelCatalogue } from "@/components/settings/ModelCatalogue";
import { ModelMatrix } from "@/components/settings/ModelMatrix";
import { ProjectStorageSection } from "@/components/settings/ProjectStorageSection";
import { ProvidersSection } from "@/components/settings/ProvidersSection";
import { RoleFallbackSection } from "@/components/settings/RoleFallbackSection";
import { SandboxRuntimeSection } from "@/components/settings/SandboxRuntimeSection";
import { ReferencePacksSection } from "@/components/settings/ReferencePacksSection";
import { SkillsSection } from "@/components/settings/SkillsSection";
import {
  useAssignments,
  useImageGenConfig,
  useModels,
  useOpenRouterKey,
} from "@/hooks/useModels";
import { useProjectsConfig } from "@/hooks/useProjectsConfig";
import { useSecrets } from "@/hooks/useSecrets";
import type {
  ImageGenConfig,
  ModelAssignments,
  ModelInfo,
  OpenRouterKeyStatus,
} from "@/types/models";

const NAV = [
  { id: "general", label: "General" },
  { id: "models", label: "Models" },
  { id: "research-media", label: "Research & Media" },
  { id: "runtime", label: "Runtime" },
  { id: "extensions-storage", label: "Extensions & Storage" },
] as const;

function imageGenIssue(
  imageGen: ImageGenConfig | undefined,
  openRouterKey: OpenRouterKeyStatus | undefined,
): string | null {
  if (!imageGen) return null;
  if (imageGen.provider === "comfyui" && !(imageGen.base_url ?? "").trim()) {
    return "Add a ComfyUI URL to turn it on.";
  }
  if (imageGen.provider === "openai" && !(imageGen.api_key_env ?? "").trim()) {
    return "Name the stored key to turn it on.";
  }
  if (imageGen.provider === "openrouter") {
    if (!openRouterKey) return null;
    if (!openRouterKey.configured) {
      return "Store an OpenRouter key to turn it on.";
    }
    if (openRouterKey.locked) {
      return "Re-enter the OpenRouter key to turn it on.";
    }
    if (!(imageGen.model ?? "").trim()) {
      return "Pick an image model to turn it on.";
    }
  }
  return null;
}

function modelIssue(
  models: ModelInfo[] | undefined,
  assignments: ModelAssignments | undefined,
): string | null {
  if (!models || !assignments) return null;
  const driver = models.find((m) => m.id === assignments.default_model);
  if (!driver) {
    return "The default primary model is missing from the catalogue.";
  }
  if (!(driver.base_url ?? "").trim()) {
    return "Add an OpenAI-compatible base URL, then set that model as the Default primary.";
  }
  return null;
}

function SettingsAttentionBanner() {
  const { data: secrets } = useSecrets();
  const { data: openRouterKey } = useOpenRouterKey();
  const { data: imageGen } = useImageGenConfig();
  const { data: projects } = useProjectsConfig();
  const { data: models } = useModels();
  const { data: assignments } = useAssignments();

  // "attention": something the user set up is broken or missing and work will
  // fail. "optional": a feature nobody has turned on yet — a stock install has
  // several, and painting them red made the page read as a wall of errors the
  // user had caused (UI-2).
  const issues: {
    title: string;
    detail: string;
    href: string;
    cta: string;
    tone: "attention" | "optional";
  }[] = [];
  const lockedCount =
    (secrets?.locked_names.length ?? 0) + (openRouterKey?.locked ? 1 : 0);
  if (lockedCount > 0) {
    issues.push({
      title: "Provider key needs attention",
      detail: "A stored key cannot be decrypted.",
      href: "#providers",
      cta: "Review providers",
      tone: "attention",
    });
  }
  const modelConfigIssue = modelIssue(models, assignments);
  if (modelConfigIssue) {
    issues.push({
      title: "Driver model not configured",
      detail: modelConfigIssue,
      href: "#model-library",
      cta: "Configure model",
      tone: "attention",
    });
  }
  const imageIssue = imageGenIssue(imageGen, openRouterKey);
  if (imageIssue) {
    issues.push({
      title: "Image generation — optional, not set up",
      detail: imageIssue,
      href: "#image-generation",
      cta: "Set up image generation",
      tone: "optional",
    });
  }
  if (projects && projects.status !== "ok") {
    issues.push({
      title: "Project storage unavailable",
      detail: "The configured project folder is not ready.",
      href: "#project-storage",
      cta: "Review storage",
      tone: "attention",
    });
  }
  if (issues.length === 0) return null;

  const anyAttention = issues.some((issue) => issue.tone === "attention");
  return (
    <div
      role="status"
      className={cn(
        "overflow-hidden rounded-card border",
        anyAttention ? "border-warn/40 bg-warn/[0.04]" : "border-hairline bg-surface-1/30",
      )}
    >
      {issues.map((issue) => {
        const attention = issue.tone === "attention";
        const Icon = attention ? AlertTriangle : Info;
        return (
          <div
            key={issue.title}
            data-attention-tone={issue.tone}
            className={cn(
              "grid grid-cols-[auto_minmax(0,1fr)] items-center gap-inline border-b px-body py-inline last:border-b-0 sm:grid-cols-[auto_minmax(0,1fr)_auto]",
              attention ? "border-warn/15" : "border-hairline",
            )}
          >
            <Icon
              className={cn(
                "size-4 shrink-0",
                attention ? "text-warn" : "text-text-faint",
              )}
              aria-hidden
            />
            <div className="min-w-0">
              <p className="font-ui text-[0.84rem] font-medium text-text">
                {issue.title}
              </p>
              <p className="font-ui text-[0.78rem] leading-snug text-text-muted">
                {issue.detail}
              </p>
            </div>
            <a
              href={issue.href}
              onClick={() => {
                const target = document.querySelector(issue.href);
                const disclosure = target?.querySelector("details");
                if (disclosure instanceof HTMLDetailsElement) disclosure.open = true;
              }}
              className={cn(
                "col-start-2 w-fit shrink-0 rounded-control border px-inline py-hair font-ui text-[0.76rem] font-medium text-accent sm:col-start-auto",
                attention
                  ? "border-warn/25 hover:border-warn/50"
                  : "border-hairline hover:border-hairline-strong",
              )}
            >
              {issue.cta}
            </a>
          </div>
        );
      })}
    </div>
  );
}

function SettingsGroup({
  id,
  aliases = [],
  title,
  summary,
  children,
}: {
  id: string;
  aliases?: readonly string[];
  title: string;
  summary: string;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      className="scroll-mt-section rounded-card border border-hairline bg-surface-1/20 p-body sm:p-section"
    >
      {aliases.map((alias) => (
        <span
          key={alias}
          id={alias}
          aria-hidden="true"
          className="block h-0 scroll-mt-section"
        />
      ))}
      <header className="mb-section border-b border-hairline pb-body">
        <h2 className="font-display text-[1.35rem] tracking-tight text-text">
          {title}
        </h2>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          {summary}
        </p>
      </header>
      <div className="flex flex-col gap-section">{children}</div>
    </section>
  );
}

function SettingsItem({ id, children }: { id?: string; children: ReactNode }) {
  return (
    <div
      id={id}
      className="scroll-mt-section border-t border-hairline pt-section first:border-t-0 first:pt-0"
    >
      {children}
    </div>
  );
}

/**
 * Settings: grouped controls for the interface, models, research/media, runtime,
 * extensions, and storage. The section components remain the existing wired surfaces;
 * this view owns information architecture and navigation.
 */
export function SettingsView() {
  const navScrollRef = useRef<HTMLElement>(null);
  const { showLeft, showRight } = useScrollFade(navScrollRef);

  return (
    <div className="mx-auto w-full max-w-[76rem] px-body py-section">
      <div className="flex flex-col gap-major">
        <header>
          <h1 className="font-display text-[2rem] tracking-tight text-text">
            Settings
          </h1>
          <p className="font-ui text-[0.88rem] text-text-muted">
            How this instance thinks, what it can do, and what it connects to.
          </p>
        </header>

        <SettingsAttentionBanner />

        <div className="grid gap-section lg:grid-cols-[13rem_minmax(0,1fr)] lg:items-start">
          <nav
            ref={navScrollRef}
            aria-label="Settings sections"
            className="sticky top-0 z-10 -mx-body flex gap-hair overflow-x-auto border-y border-hairline bg-bg/95 px-body py-inline backdrop-blur lg:top-section lg:mx-0 lg:flex-col lg:overflow-visible lg:border-y-0 lg:border-r lg:py-hair lg:pr-body"
          >
            {NAV.map((item) => (
              <a
                key={item.id}
                href={`#${item.id}`}
                className="flex min-h-11 shrink-0 items-center whitespace-nowrap rounded-control px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:bg-surface-1 hover:text-text lg:min-h-0"
              >
                {item.label}
              </a>
            ))}
            {/* Mobile-only scroll affordance — the strip overflows below `lg`
                (five section labels never fit 375–430px), and a plain
                overflow-x-auto gives no visual hint that "Extensions &
                Storage" is one swipe away. lg:hidden because desktop switches
                to the non-scrolling vertical sidebar layout above. */}
            <ScrollFadeEdges showLeft={showLeft} showRight={showRight} className="lg:hidden" />
          </nav>

          <div className="flex min-w-0 flex-col gap-major">
            <SettingsGroup
              id="general"
              title="General"
              summary="Choose how the application presents itself while you work."
            >
              <SettingsItem id="agent-chat">
                <ChatVerbositySection />
              </SettingsItem>
            </SettingsGroup>

            <SettingsGroup
              id="models"
              aliases={["models-providers"]}
              title="Models"
              summary="Choose the models this instance uses and connect their providers."
            >
              <SettingsItem id="role-assignments">
                <ModelMatrix />
              </SettingsItem>
              <SettingsItem>
                <ProvidersSection />
              </SettingsItem>
              <SettingsItem id="model-library">
                <span id="catalogue" aria-hidden className="block h-0 scroll-mt-section" />
                <ModelCatalogue />
              </SettingsItem>
              <SettingsItem id="model-resilience">
                <details className="rounded-control border border-hairline bg-surface-1/30 px-body py-inline">
                  <summary className="cursor-pointer py-3 font-ui text-[0.84rem] font-medium text-text lg:py-0">
                    Advanced model resilience
                  </summary>
                  <div className="mt-inline">
                    <RoleFallbackSection />
                  </div>
                </details>
              </SettingsItem>
            </SettingsGroup>

            <SettingsGroup
              id="research-media"
              aliases={["intelligence"]}
              title="Research & Media"
              summary="Configure research retrieval, supporting encoders, and generated media."
            >
              <SettingsItem id="encoders">
                <EncoderSection />
              </SettingsItem>
              <SettingsItem id="data-sources">
                <DataSourcesSection />
              </SettingsItem>
              <SettingsItem id="image-generation">
                <ImageGenSection />
              </SettingsItem>
              <SettingsItem id="audio-overview">
                <AudioSection />
              </SettingsItem>
            </SettingsGroup>

            <SettingsGroup
              id="runtime"
              aliases={["agent-sandbox"]}
              title="Runtime"
              summary="Choose where agent tools run and expose dependent runtime features."
            >
              <SettingsItem id="sandbox">
                <SandboxRuntimeSection />
              </SettingsItem>
            </SettingsGroup>

            <SettingsGroup
              id="extensions-storage"
              aliases={["workspace"]}
              title="Extensions & Storage"
              summary="Choose where projects persist and manage reusable skills plus external tool connections."
            >
              <SettingsItem id="project-storage">
                <ProjectStorageSection />
              </SettingsItem>
              <SettingsItem id="reference-packs">
                <ReferencePacksSection />
              </SettingsItem>
              <SettingsItem id="skills">
                <SkillsSection />
              </SettingsItem>
              <SettingsItem id="connections">
                <McpSection />
              </SettingsItem>
            </SettingsGroup>
          </div>
        </div>
      </div>
    </div>
  );
}
