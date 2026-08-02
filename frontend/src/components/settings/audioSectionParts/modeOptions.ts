import { Cloud, Cpu, Server, VolumeX } from "lucide-react";

export type Provider = "bundled" | "speaches" | "openai";
export type Mode = "off" | Provider;

export const OPTIONS: { mode: Mode; Icon: typeof Cpu; label: string; help: string }[] =
  [
    {
      mode: "off",
      Icon: VolumeX,
      label: "Off",
      help: "Audio overviews are disabled, and the agent-server unloads the voice model to free RAM. The tool fails soft with a clear message if invoked.",
    },
    {
      mode: "bundled",
      Icon: Cpu,
      label: "Bundled (in-process)",
      help: "Kokoro ONNX/CPU, self-contained — no TTS server, no key. Lazy-loaded, so enabled-but-unused costs nothing; loads ~0.5 GB on first use. The default.",
    },
    {
      mode: "speaches",
      Icon: Server,
      label: "Self-hosted endpoint",
      help: "An OpenAI-compatible /v1/audio/speech endpoint you run (Speaches / Kokoro-FastAPI). Keyless. Offloads the model from this host; leave the URL blank to use the server default.",
    },
    {
      mode: "openai",
      Icon: Cloud,
      label: "Paid API (OpenAI-compatible)",
      help: "A vendor like OpenAI tts-1. Set the base URL, choose the stored key name, and set the model id. Use vendor voice names (e.g. alloy).",
    },
  ];

export function modeOf(enabled: boolean, provider: Provider): Mode {
  return enabled ? provider : "off";
}
