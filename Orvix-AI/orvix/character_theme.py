"""Per-character ambient accent colours (Command Center Phase 8).

Pure data only: this module maps each assistant character to ONE
accent colour used for the AMBIENT / secondary visual layer — the
web home's aurora and panel-edge tint, the character-card accents,
and the Tk home's selected-character card tint.

The accent is deliberately NOT the core state signal. Core state
colours (``orvix/core_states.py`` STATE_PARAMS accents, the web
reactor's per-state hues and the FX canvas palette) are keyed to
the core *state* and stay untouched, so the live state remains the
primary, readable signal no matter which character is selected.

Keys are the character keys the app persists in
``selected_character`` (``"luna"`` / ``"ruby"`` / ``"maya"``);
lookups are case-insensitive and any unknown character falls back
to :data:`DEFAULT_CHARACTER_ACCENT`, the page's base cyan.
"""
from __future__ import annotations

#: The base accent used when no character theme applies — the web
#: home's long-standing cyan, so an unknown character changes
#: nothing visually.
DEFAULT_CHARACTER_ACCENT = "#00dfff"

#: Character key -> ambient accent hex. The three values are drawn
#: from the app's existing palette family: LUNA takes the sky /
#: electric blue of the core palette, RUBY the magenta glow its
#: character-card art already uses, and MAYA the warm amber of the
#: core palette. The web stylesheet mirrors these exact values in
#: its ``[data-character="..."]`` rules; a unit test asserts the
#: two never drift apart.
CHARACTER_ACCENTS = {
    "luna": "#38bdf8",
    "ruby": "#e77cff",
    "maya": "#ffb36b",
}


def character_accent(character) -> str:
    """Ambient accent hex for ``character`` (any case / None-safe).

    Unknown or empty values return :data:`DEFAULT_CHARACTER_ACCENT`
    rather than raising: the accent is decoration, never a reason
    to break a state push or a card refresh.
    """
    key = str(character or "").strip().lower()
    return CHARACTER_ACCENTS.get(key, DEFAULT_CHARACTER_ACCENT)
