import { agentGet, agentSend } from "./client";
import type { AppKitOwnerControls, TweakValue } from "@/types/appkitOwner";

type Response = { controls: AppKitOwnerControls | null };
const endpoint = (cid: string) => `/conversations/${encodeURIComponent(cid)}/appkit/controls`;
export const getAppKitControls = (cid: string) => agentGet<Response>(endpoint(cid));
export const saveAppKitControls = (cid: string, digest: string, values: Record<string, TweakValue>) =>
  agentSend<Response>("PATCH", endpoint(cid), { expected_spec_digest: digest, values });
