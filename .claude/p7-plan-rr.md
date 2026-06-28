Re-review the 5 required revisions to PR P7-KITS (see "PLAN REVISION 1" in /home/dylan/projects/disclaude/disclaude.md).
1. REAL consumers (not a decorative registry): (a) app_create scaffolds the lead_form starter when sections is
None (byte-equivalent, single source); (b) a NEW host-owned tool scaffold_starter materializes the active
contract's starter files into the workspace (the model invokes it per the pack's "scaffold from the <name>
starter"). Registered + scoped + coherence-tested.
2. StarterKit.scaffold(title) PARAMETERIZED + PATH-SAFE (normalized rel paths; reject '..'/absolute).
3. lead_form default AppSpec lives in starter.py; app_create uses it when sections is None; rendered output
byte-equivalent to today.
4. DECK RECONCILE: the deck contract required_files=("deck.json",) + build_deck pack are wrong — slides tooling
writes the AuthoredDeck sidecar {name}.authored.json (slides.py:485 confirms editable_source = {name}.authored.json).
Fix: align the deck contract + pack to the real AuthoredDeck source; slides_generate IS the deck materializer
(NO deck_stage file-map). Will confirm the exact base-name convention at impl.
5. BRANDKIT = AppKit PROJECTION over the EXISTING disco.core.brand themes (Theme.accent/font_display/ui/reading/
mono/branded + THEMES) → appkit {primary,accent,bg,fg,font}; NOT a parallel token catalog. Apply via app_set_design.
Scope out: full UIKit + app_set_brand (packs won't imply it).
Confirm these resolve your 5 revisions. Two scope questions: (i) is adding a scaffold_starter TOOL the right
real-consumer (vs a runtime bootstrap materializer)? (ii) for the deck reconcile, is aligning the CONTRACT/pack
to the existing AuthoredDeck filename the right direction (vs changing the tooling)? Return APPROVE or REVISE +
one line each.
