"""Pure helpers for the small floating overlay orb.

The overlay is the *mini mode* of the existing floating HUD window in
app.py: a compact, frameless, always-on-top orb that can be dragged to
any position on the desktop and floats above other applications. This
module holds only the geometry / interaction math for it — window
sizes, on-screen clamping, click-vs-drag discrimination, command
palette placement and command normalisation — so every rule is
unit-testable headless. No tkinter import lives here.

Scope honesty: the overlay floats over other *desktop* applications
(that is what an always-on-top desktop window does). It does not embed
inside other applications or websites; web/app control happens because
commands submitted from its palette are routed through the desktop
app's existing command pipeline (``OrvixDesktop._send_text``), with the
app's existing tools and approval gates.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

# Overlay (mini) geometry. The orb canvas renders the same
# FloatingCoreWidget as the hero core, just smaller; 88 px keeps the
# rings/particles legible while staying a compact desktop bubble.
OVERLAY_ORB_SIZE = 88
OVERLAY_WIDTH = 104
OVERLAY_HEIGHT = 128            # orb + EXPAND affordance + resize grip
PALETTE_HEIGHT = 42             # extra height while the palette is open
MIN_OVERLAY_WIDTH = OVERLAY_WIDTH
MIN_OVERLAY_HEIGHT = OVERLAY_HEIGHT

#: Press/release travel (px) at or below this counts as a click, not a drag.
CLICK_TRAVEL_PX = 6

#: Delay before a single click toggles the palette, so a double-click
#: (open main window) can cancel the pending toggle first.
CLICK_TOGGLE_DELAY_MS = 240


def clamp_overlay_position(x: int, y: int, width: int, height: int,
                           screen_width: int, screen_height: int) -> tuple[int, int]:
    """Clamp a window origin so the whole overlay stays on screen."""
    max_x = max(0, int(screen_width) - int(width))
    max_y = max(0, int(screen_height) - int(height))
    return (min(max(int(x), 0), max_x), min(max(int(y), 0), max_y))


def overlay_window_height(palette_open: bool) -> int:
    """Window height for the overlay with the palette open or closed."""
    return OVERLAY_HEIGHT + (PALETTE_HEIGHT if palette_open else 0)


def palette_open_geometry(x: int, y: int, *, palette_open: bool,
                          screen_width: int, screen_height: int) -> tuple[int, int, int]:
    """(x, y, height) for the overlay window in the given palette state.

    The palette grows the window downward from the orb; if that would
    push the window past the bottom of the screen, the window shifts
    up so the palette stays fully visible.
    """
    height = overlay_window_height(palette_open)
    nx, ny = clamp_overlay_position(x, y, OVERLAY_WIDTH, height,
                                    screen_width, screen_height)
    return (nx, ny, height)


def is_click(press: Sequence[int], release: Sequence[int],
             threshold: int = CLICK_TRAVEL_PX) -> bool:
    """True when a press/release pair is a click rather than a drag."""
    if len(press) < 2 or len(release) < 2:
        return False
    return (abs(int(release[0]) - int(press[0])) <= threshold
            and abs(int(release[1]) - int(press[1])) <= threshold)


def normalize_overlay_command(text: object) -> Optional[str]:
    """Normalise palette text into a command, or None when empty."""
    if text is None:
        return None
    command = " ".join(str(text).split())
    return command or None


def submit_overlay_command(text: object, send: Callable[[str], None]) -> bool:
    """Route palette text through the app's existing command pipeline.

    ``send`` is the very same entry point the main window's composer
    uses (``OrvixDesktop._send_text``), so overlay commands execute
    with the app's existing tools, queueing and approval gates.
    Returns True when a command was actually routed.
    """
    command = normalize_overlay_command(text)
    if command is None:
        return False
    send(command)
    return True
