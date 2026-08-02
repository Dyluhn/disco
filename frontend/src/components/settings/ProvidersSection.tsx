import { GenericProviders } from "./providersSectionParts/GenericProviders";
import { OpenRouterSection } from "./OpenRouterSection";
import { ProviderKeysSection } from "./ProviderKeysSection";

export function ProvidersSection() {
  return (
    <section
      id="providers"
      aria-labelledby="providers-heading"
      className="flex flex-col gap-inline"
    >
      <header>
        <h3
          id="providers-heading"
          className="font-ui text-[0.95rem] font-semibold text-text"
        >
          Providers
        </h3>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          Add a provider key, browse the models that key can see, and toggle
          models into the shared catalogue.
        </p>
      </header>

      <GenericProviders />

      <OpenRouterSection />

      <details className="rounded-card border border-hairline bg-surface-1/30 px-body py-inline">
        <summary className="cursor-pointer font-ui text-[0.84rem] font-medium text-text">
          Advanced provider keys
        </summary>
        <div className="mt-inline">
          <ProviderKeysSection />
        </div>
      </details>
    </section>
  );
}
