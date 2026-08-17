import { FIELD_CLASS } from "./styles";

export function VoiceFieldsRow({
  voiceA,
  setVoiceA,
  voiceB,
  setVoiceB,
}: {
  voiceA: string;
  setVoiceA: (value: string) => void;
  voiceB: string;
  setVoiceB: (value: string) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-inline">
      <label className="flex flex-col gap-hair">
        <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
          Host A voice
          <span className="font-ui text-[0.72rem] text-text-faint">
            · single-speaker default
          </span>
        </span>
        <input
          spellCheck={false}
          value={voiceA}
          onChange={(e) => setVoiceA(e.target.value)}
          placeholder="af_heart"
          className={FIELD_CLASS}
        />
      </label>
      <label className="flex flex-col gap-hair">
        <span className="font-ui text-[0.8rem] text-text">Host B voice</span>
        <input
          spellCheck={false}
          value={voiceB}
          onChange={(e) => setVoiceB(e.target.value)}
          placeholder="af_bella"
          className={FIELD_CLASS}
        />
      </label>
    </div>
  );
}
