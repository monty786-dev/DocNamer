"""Tiny i18n layer: all UI strings go through t(). Add more languages by extending STRINGS."""
from __future__ import annotations

STRINGS = {
    "en": {
        "app": "DocNamer",
        "step_folder": "1 Folder", "step_orient": "2 Orientation", "step_zones": "3 Zones / Mode",
        "step_pages": "4 Multi-page", "step_naming": "5 Naming", "step_preview": "6 Preview", "step_run": "7 Run",
        "browse": "Browse...", "next": "Next >", "back": "< Back", "start": "Start", "pause": "Pause",
        "resume": "Resume", "cancel": "Cancel", "include_sub": "Include subfolders",
        "input_folder": "Input folder", "output_folder": "Output folder",
        "orientation_auto": "Auto-fix page orientation (recommended)", "mode_zones": "Zones (draw areas)",
        "mode_anchors": "Anchors (keyword)", "mode_full": "Full read (letter by letter)",
        "pattern": "Name pattern", "fallback": "Fallback pattern", "dry_run": "Preview (dry run)",
        "commit": "Create renamed copies", "review_title": "Can't read this - please help",
        "accept": "Accept", "edit": "Use my text", "skip_field": "Skip field", "use_fallback": "Use fallback name",
        "needs_manual": "Needs manual", "apply_similar": "Apply to all similar",
        "undo": "Undo last run", "done": "Done", "error": "Error",
        "save_library": "Save to library (teach handwriting)",
        "teach_title": "Teach handwriting", "teach_btn": "Teach handwriting...",
        "teach_ask": "What does this contain?  Type it (Hindi or English)",
        "teach_word": "word / \u0936\u092c\u094d\u0926", "teach_letter": "letter / \u0905\u0915\u094d\u0937\u0930",
        "teach_save": "Save & next", "teach_skip": "Skip", "teach_letters": "Teach this word's letters",
        "teach_also": "Also save its letters automatically (when the count matches)",
        "teach_finish": "Finish", "teach_profile": "Writer / font name", "teach_kind": "Type",
        "teach_pick_zone": "Select (or draw) a zone on the sample page first - the handwriting inside it will be taught.",
    }
}
_LANG = "en"


def set_language(code: str) -> None:
    global _LANG
    _LANG = code if code in STRINGS else "en"


def t(key: str, **kw) -> str:
    s = STRINGS.get(_LANG, STRINGS["en"]).get(key) or STRINGS["en"].get(key, key)
    return s.format(**kw) if kw else s
