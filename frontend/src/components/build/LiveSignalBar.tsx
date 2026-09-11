/**
 * The live activity narrator — what the agent is doing RIGHT NOW between
 * persisted events. Without token streaming, the static "Working" badge feels
 * frozen during slow local-model turns. This bar replaces it with a precise,
 * always-changing description ("Reading your message…", "Running `ls /tmp`…",
 * "Composing next step…") so the user can tell something is happening even
 * when the model takes 20 seconds to emit the next event.
 *
 * Pure derivation: lives.kind drives the icon + label. No animations beyond
 * the spinner — quiet in appearance, rich in function.
 */

import { Loader2, MessageSquare, Sparkles, Terminal, User } from "lucide-react";
import type { LiveSignal } from "@/lib/buildTrace";

const TOOL_PROGRESS: Record<string, string> = {
  file_write: "Writing file",
  file_edit: "Editing file",
  file_read: "Reading file",
  file_list: "Listing files",
  file_append: "Extending file",
  file_insert_lines: "Editing file",
  file_replace_lines: "Editing file",
  file_str_replace: "Editing file",
  exact_replace: "Editing file",
  safe_write_file: "Writing file",
  preview_start: "Starting the preview server",
  preview_stop: "Stopping the preview server",
  preview_status: "Checking the preview server",
  preview_logs: "Reading the preview logs",
  server_status: "Checking the app server",
  shell: "Running command",
  shell_exec: "Running command",
  shell_view: "Watching a running command",
  shell_wait: "Waiting on a command",
  shell_kill_process: "Stopping a process",
  shell_write_to_process: "Sending input to a process",
  run_project_script: "Running a project script",
  code_exec: "Running code",
  search: "Searching the web",
  extract: "Reading a web page",
  slides_generate: "Generating slides",
  deck_patch: "Editing slides",
  sheet_generate: "Generating a spreadsheet",
  doc_set_section: "Drafting a document section",
  doc_export: "Exporting the document",
  audio_overview: "Generating an audio overview",
  image_generate: "Generating an image",
  scaffold_starter: "Setting up the project starter",
  build_playbook: "Reading a playbook",
  create_reference_pack: "Saving a reference pack",
  reference_inspect: "Inspecting a reference file",
  app_create: "Creating the app scaffold",
  app_add_section: "Adding an app section",
  app_update_content: "Updating app content",
  app_set_design: "Applying the design",
  app_snapshot_version: "Saving a version snapshot",
  design_lint: "Checking the design",
  verify_web_app: "Verifying the app in a browser",
  verify_appkit_app: "Verifying the app",
  plan_step: "Updating the plan",
  update_plan_progress: "Checking off plan progress",
  submit_plan: "Proposing a plan",
  think: "Thinking it through",
  skip: "Skipping a step",
  context_memory: "Saving working notes",
  delegate_explore: "Exploring the codebase",
  draft_workflow: "Drafting a workflow",
  enter_workflow: "Starting a workflow",
  list_workflows: "Listing workflows",
  read_workflow_card: "Reading a workflow card",
  request_custom_build: "Requesting a custom build",
  needs_input: "Asking for input",
};

export function LiveSignalBar({ signal }: { signal: LiveSignal }) {
  if (signal.kind === "idle") return null;

  const { Icon, label, detail } = describe(signal);
  // When the agent is WAITING ON THE USER it isn't working — no spinner (a
  // spinner there would falsely imply progress). Otherwise the spinner conveys
  // active work between events.
  const working = signal.kind !== "waiting_for_you";
  return (
    <div className="flex items-center gap-hair rounded-control border border-dashed border-hairline bg-surface-1/60 px-inline py-hair">
      {working && <Loader2 className="size-3.5 shrink-0 animate-spin text-accent" aria-hidden />}
      <Icon className="size-3.5 shrink-0 text-text-faint" aria-hidden />
      <div className="flex min-w-0 flex-1 items-baseline gap-hair">
        <span className="font-ui text-[0.82rem] text-text">{label}</span>
        {detail && (
          <span className="min-w-0 truncate font-mono text-[0.74rem] text-text-faint">
            {detail}
          </span>
        )}
      </div>
    </div>
  );
}

function describe(signal: LiveSignal): {
  Icon: typeof Loader2;
  label: string;
  detail?: string;
} {
  switch (signal.kind) {
    case "starting":
      return { Icon: Sparkles, label: "Reading the goal…" };
    case "thinking_about_user_message":
      return {
        Icon: User,
        label: "Reading your message…",
        detail: signal.preview,
      };
    case "tool_executing": {
      const verb = TOOL_PROGRESS[signal.tool_name];
      return {
        Icon: Terminal,
        label: verb ? `${verb}…` : `Working (${signal.tool_name})`,
        detail: signal.detail,
      };
    }
    case "composing_next_step":
      return { Icon: MessageSquare, label: "Composing the next step…" };
    case "waiting_for_you":
      return { Icon: User, label: signal.label };
    default:
      // unreachable — exhaustive switch
      return { Icon: Loader2, label: "Working…" };
  }
}
