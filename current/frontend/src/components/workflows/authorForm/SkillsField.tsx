import type { WorkflowAuthoringContext } from "@/types/workflow";
import { toggleValue } from "./draftMapping";

interface SkillsFieldProps {
  skills: WorkflowAuthoringContext["skills"];
  selectedSkills: string[];
  onChange: (skills: string[]) => void;
}

export function SkillsField({ skills, selectedSkills, onChange }: SkillsFieldProps) {
  if (skills.length === 0) return null;

  return (
    <section className="flex flex-col gap-inline">
      <h4 className="font-ui text-[0.86rem] font-semibold text-text">Skills</h4>
      <div className="flex flex-wrap gap-hair">
        {skills.map((skill) => (
          <label
            key={skill}
            className="flex min-h-11 items-center gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text-muted lg:min-h-0"
          >
            <input
              type="checkbox"
              checked={selectedSkills.includes(skill)}
              onChange={(event) =>
                onChange(toggleValue(selectedSkills, skill, event.target.checked))
              }
            />
            {skill}
          </label>
        ))}
      </div>
    </section>
  );
}
