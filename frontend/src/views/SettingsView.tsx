import { AlertTriangle } from "lucide-react";
import type { ReactNode } from "react";
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
    return "Image generation needs a ComfyUI URL.";
  }
  if (imageGen.provider === "openai" && !(imageGen.api_key_env ?? "").trim()) {
    return "Image generation needs a stored key name.";
  }
  if (imageGen.provider === "openrouter") {
    if (!openRouterKey) return null;
    if (!openRouterKey.configured) {
      return "Image generation needs an OpenRouter key.";
    }
    if (openRouterKey.locked) {
      return "Image generation needs the OpenRouter key re-entered.";
    }
    if (!(imageGen.model ?? "").trim()) {
      return "Image generation needs an image model.";
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

  const issues: { title: string; detail: string; href: string; cta: string }[] =
    [];
  const lockedCount =
    (secrets?.locked_names.length ?? 0) + (openRouterKey?.locked ? 1 : 0);
  if (lockedCount > 0) {
    issues.push({
      title: "Provider key needs attention",
      detail: "A stored key cannot be decrypted.",
      href: "#providers",
      cta: "Review providers",
    });
  }
  const modelConfigIssue = modelIssue(models, assignments);
  if (modelConfigIssue) {
    issues.push({
      title: "Driver model not configured",
      detail: modelConfigIssue,
      href: "#catalogue",
      cta: "Configure model",
    });
  }
  const imageIssue = imageGenIssue(imageGen, openRouterKey);
  if (imageIssue) {
    issues.push({
      title: "Image generation unavailable",
      detail: imageIssue,
      href: "#image-generation",
      cta: "Configure image generation",
    });
  }
  if (projects && projects.status !== "ok") {
    issues.push({
      title: "Project storage unavailable",
      detail: "The configured project folder is not ready.",
      href: "#project-storage",
      cta: "Review storage",
    });
  }
  if (issues.length === 0) return null;

  return (
    <div
      role="status"
      className="flex items-start gap-inline rounded-card border border-warn/50 bg-warn/5 p-body"
    >
      <AlertTriangle className="mt-px size-4 shrink-0 text-warn" aria-hidden />
      <div className="flex flex-col gap-inline">
        {issues.map((issue) => (
          <div
            key={issue.title}
            className="flex flex-col gap-hair sm:flex-row sm:items-center"
          >
            <div className="min-w-0">
              <p className="font-ui text-[0.84rem] font-medium text-text">
                {issue.title}
              </p>
              <p className="font-ui text-[0.78rem] text-text-muted">
                {issue.detail}
              </p>
            </div>
            <a
              href={issue.href}
              className="shrink-0 font-ui text-[0.78rem] font-medium text-accent hover:underline"
            >
              {issue.cta}
            </a>
          </div>
        ))}
      </div>
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
    <section id={id} className="scroll-mt-section">
      {aliases.map((alias) => (
        <span
          key={alias}
          id={alias}
          aria-hidden="true"
          className="block h-0 scroll-mt-section"
        />
      ))}
      <header className="mb-section">
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
            aria-label="Settings sections"
            className="sticky top-0 z-10 -mx-body flex gap-hair overflow-x-auto border-y border-hairline bg-bg/95 px-body py-inline backdrop-blur lg:top-section lg:mx-0 lg:flex-col lg:overflow-visible lg:border-y-0 lg:border-r lg:py-hair lg:pr-body"
          >
            {NAV.map((item) => (
              <a
                key={item.id}
                href={`#${item.id}`}
                className="whitespace-nowrap rounded-control px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:bg-surface-1 hover:text-text"
              >
                {item.label}
              </a>
            ))}
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
              <SettingsItem id="catalogue">
                <ModelCatalogue />
              </SettingsItem>
              <SettingsItem id="model-resilience">
                <details className="rounded-control border border-hairline bg-surface-1/30 px-body py-inline">
                  <summary className="cursor-pointer font-ui text-[0.84rem] font-medium text-text">
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
