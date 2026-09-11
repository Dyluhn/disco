# Voice dictation into a text box

> Record from the microphone, transcribe on the server through an OpenAI-compatible speech-to-text endpoint, drop the text into the input for review before sending. Browser speech recognition as the keyless fallback.

## Env
```
STT_BASE_URL   # OpenAI-compatible /v1 base with /audio/transcriptions; default = LLM_BASE_URL
STT_API_KEY    # default = LLM_API_KEY
STT_MODEL      # e.g. whisper-1, or a local whisper server's model name
```

## Client (React)
```tsx
export function DictationButton({ onText }: { onText: (t: string) => void }) {
  const rec = useRef<MediaRecorder | null>(null); const chunks = useRef<Blob[]>([]);
  const [state, setState] = useState<"idle" | "recording" | "transcribing">("idle");
  async function start() {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/mp4";
    const r = new MediaRecorder(stream, { mimeType: mime }); chunks.current = [];
    r.ondataavailable = e => chunks.current.push(e.data);
    r.onstop = async () => {
      stream.getTracks().forEach(t => t.stop()); setState("transcribing");
      const form = new FormData(); form.append("audio", new Blob(chunks.current, { type: mime }), "clip.webm");
      const res = await fetch("/api/stt", { method: "POST", body: form });
      setState("idle");
      if (!res.ok) return alert("Transcription failed: " + (await res.json()).error);
      onText((await res.json()).text);                  // caller APPENDS into the textarea — user edits, then sends
    };
    rec.current = r; r.start(); setState("recording");
  }
  return <button type="button" aria-pressed={state === "recording"} disabled={state === "transcribing"}
    onClick={() => state === "recording" ? rec.current?.stop() : start()}>
    {state === "recording" ? "Stop" : state === "transcribing" ? "Transcribing…" : "🎤 Dictate"}</button>;
}
```
Requirements the graders check: a visible microphone control in the chat input; text lands in the box, not sent automatically; the user can edit before sending.

## Server
```js
// api/src/routes/stt.js  (busboy for multipart — see media-uploads playbook; 15 MB cap; audio/* only)
app.post("/api/stt", async (req, res) => {
  const { buffer, mime } = await readSingleFile(req, { maxBytes: 15 * 1024 * 1024, accept: /^audio\// });
  const base = process.env.STT_BASE_URL ?? process.env.LLM_BASE_URL, key = process.env.STT_API_KEY ?? process.env.LLM_API_KEY ?? "";
  if (!base || !process.env.STT_MODEL) return res.status(503).json({ error: "STT_BASE_URL/STT_MODEL not configured" });
  const form = new FormData(); form.append("file", new Blob([buffer], { type: mime }), "clip.webm");
  form.append("model", process.env.STT_MODEL); form.append("language", "en");
  form.append("prompt", "Legal terminology: plaintiff, defendant, indemnification, tort, estoppel, subpoena.");  // domain hint improves accuracy
  const r = await fetch(`${base}/audio/transcriptions`, { method: "POST", headers: key ? { authorization: `Bearer ${key}` } : {}, body: form });
  if (!r.ok) return res.status(502).json({ error: `stt ${r.status}` });
  res.json({ text: (await r.json()).text });
});
```
Tailor the `prompt` vocabulary hint to the brief's domain (medical, legal, finance) — it is the cheap lever for "handles domain terminology".

## Keyless fallback
If `STT_MODEL` is unset, the button uses `window.SpeechRecognition ?? window.webkitSpeechRecognition` (Chromium only) with `interimResults=false`, `lang="en-US"`, and appends `event.results[0][0].transcript`. Show "Browser dictation (limited)" so the limitation is visible; keep the server path as the primary.

## Prove it
In a real browser (`verify_web_app` grants the mic permission via Playwright `context.grantPermissions(["microphone"])` and Chromium's `--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=sample.wav`): click Dictate, stop, the text appears in the input, edit it, send. Server test: `curl -F audio=@sample.wav /api/stt` returns the expected words. Record the WER on a 20-term domain list in the delivery notes.
