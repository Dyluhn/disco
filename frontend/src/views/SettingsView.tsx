import { AudioSection } from "@/components/settings/AudioSection";
import { DataSourcesSection } from "@/components/settings/DataSourcesSection";
import { EncoderSection } from "@/components/settings/EncoderSection";
import { McpSection } from "@/components/settings/McpSection";
import { ModelMatrix } from "@/components/settings/ModelMatrix";
import { ProjectStorageSection } from "@/components/settings/ProjectStorageSection";
import { ProviderKeysSection } from "@/components/settings/ProviderKeysSection";
import { SandboxSection } from "@/components/settings/SandboxSection";
import { SkillsSection } from "@/components/settings/SkillsSection";

/**
 * Settings (Prompt 4): the model-assignment matrix (absolute, manual model story)
 * plus the skills and MCP scaffolds. Dense by nature — kept organized and quiet,
 * not decorated. One readable column; hairline-separated sections.
 */
export function SettingsView() {
  return (
    <div className="mx-auto w-full max-w-doc px-body py-section">
      <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-major">
        <header>
          <h1 className="font-display text-[2rem] tracking-tight text-text">Settings</h1>
          <p className="font-ui text-[0.88rem] text-text-muted">
            How this instance thinks, what it can do, and what it connects to.
          </p>
        </header>

        <ModelMatrix />
        <ProviderKeysSection />
        <EncoderSection />
        <AudioSection />
        <DataSourcesSection />
        <SandboxSection />
        <ProjectStorageSection />
        <SkillsSection />
        <McpSection />
      </div>
    </div>
  );
}
