# Acknowledgements

Disco is licensed under Apache-2.0. It stands on prior art from the open-source
agent community. This file credits the projects whose designs and (where noted)
code Disco has studied, adapted, or directly incorporated.

> **For maintainers:** when you port *substantial code* (not just an idea) from a
> project below, you MUST retain that project's copyright + license notice — inline
> at the top of the derived file AND in the table here. For a *technique/design*
> reimplemented from scratch, an entry here is the courtesy (and is required by our
> own honesty rules). MIT and BSD code is compatible with our Apache-2.0 license;
> keep their notices intact.

## Prior art

| Project | Author | License | What Disco took |
|---------|--------|---------|-----------------|
| [SmallCode](https://github.com/Doorman11991/smallcode) | Doorman11991 | MIT | Weak-model adaptations harvested into Disco's gated "weak-model assist" tier (see `development/notes/remaining-work-plan.md`, Track F): multi-format tool-call recovery (text / `reasoning_content` fallback, trailing-comma repair), hallucinated-tool-name detection with edit-distance suggestions, read-before-write guard, first-turn project bootstrap, patch-spiral detection, thinking-budget truncation, mid-turn argument truncation, and the Contract / Definition-of-Done guard pattern. *Status: design-harvested 2026-06-13; code ports pending — each will carry the MIT notice below.* |
| [Aider](https://github.com/Aider-AI/aider) | Paul Gauthier & contributors | Apache-2.0 | The `ChatChunks.chat_files` pattern (current file content as a separate, non-condensable chunk rebuilt from disk each turn) — the basis for Disco's live workspace snapshot. |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) (incl. `openhands-aci`) | All-Hands-AI & contributors | MIT | The 4-pattern stuck-detector (ported in `core/loop/stuck.py`); the `str_replace` / content-anchored edit pattern behind `file_edit`'s forgiving replace; `view`-style always-current on-disk reads. |
| [Cline](https://github.com/cline/cline) | Cline Bot Inc. & contributors | Apache-2.0 | Studied for tool-use UX and plan/act mode boundaries. |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent) | Princeton NLP & contributors | MIT | Studied for epochal history masking and the agent-computer-interface design. |

## License notices for incorporated code

*(Add the verbatim upstream notice here when code is ported. Placeholder kept ready.)*

### SmallCode — MIT License

> Copyright (c) 2026 Doorman11991
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of
> this software and associated documentation files (the "Software"), to deal in the
> Software without restriction, including without limitation the rights to use, copy,
> modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
> and to permit persons to whom the Software is furnished to do so, subject to the
> following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
> INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
> PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
> HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
> CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
> OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
