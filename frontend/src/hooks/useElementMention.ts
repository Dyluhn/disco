import { useCallback, useEffect, useRef, useState } from "react";
import {
  makeElementMentionArmCommand,
  makeElementMentionDisarmCommand,
  makeElementMentionNonce,
  parseElementMentionMessage,
  sendElementMentionCommand,
} from "@/lib/elementMention";
import type { ElementMentionPayload } from "@/lib/elementMention";

export interface ElementMentionState {
  armed: boolean;
  arm: () => void;
  disarm: () => void;
}

export function useElementMention(
  iframeRef: React.RefObject<HTMLIFrameElement | null>,
  allowedOrigin: string | null,
  onMention: (payload: ElementMentionPayload) => void,
  enabled = true,
): ElementMentionState {
  const [armed, setArmed] = useState(false);
  const nonceRef = useRef("");
  const onMentionRef = useRef(onMention);
  onMentionRef.current = onMention;

  const disarm = useCallback(() => {
    const nonce = nonceRef.current;
    const iframe = iframeRef.current;
    if (iframe && nonce) {
      sendElementMentionCommand(iframe, makeElementMentionDisarmCommand(nonce));
    }
    nonceRef.current = "";
    setArmed(false);
  }, [iframeRef]);

  const arm = useCallback(() => {
    if (!enabled || !allowedOrigin) return;
    const nonce = makeElementMentionNonce();
    nonceRef.current = nonce;
    setArmed(true);
    const iframe = iframeRef.current;
    if (iframe) {
      sendElementMentionCommand(iframe, makeElementMentionArmCommand(nonce));
    }
  }, [allowedOrigin, enabled, iframeRef]);

  useEffect(() => {
    if (!enabled) {
      disarm();
    }
  }, [disarm, enabled]);

  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const nonce = nonceRef.current;
      if (!nonce || !allowedOrigin) return;
      const payload = parseElementMentionMessage(event, allowedOrigin, nonce);
      if (!payload) return;
      nonceRef.current = "";
      setArmed(false);
      onMentionRef.current(payload);
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [allowedOrigin]);

  useEffect(() => {
    return () => {
      const nonce = nonceRef.current;
      const iframe = iframeRef.current;
      if (iframe && nonce) {
        sendElementMentionCommand(iframe, makeElementMentionDisarmCommand(nonce));
      }
    };
  }, [iframeRef]);

  return { armed, arm, disarm };
}
