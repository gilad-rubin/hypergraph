"""Node styling definitions for visualization.

Each node type has explicit, readable styling. Non-frontend developers
can easily modify colors by changing Tailwind CSS classes.

Tailwind color reference: https://tailwindcss.com/docs/customizing-colors
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeStyle:
    """Style configuration for a node type.

    All colors use Tailwind CSS classes for consistency.
    """

    # Border colors
    border_dark: str
    border_light: str

    # Background colors
    bg_dark: str
    bg_light: str

    # Icon (shown in node header)
    icon: str
    icon_color_dark: str
    icon_color_light: str

    # Text colors
    text_dark: str
    text_light: str

    # Dimensions
    min_width: int = 120
    header_height: int = 36
    output_row_height: int = 24


# ============================================================
# NODE TYPE DEFINITIONS
# ============================================================

FUNCTION = NodeStyle(
    border_dark="border-indigo-500/50",
    border_light="border-indigo-400",
    bg_dark="bg-slate-800",
    bg_light="bg-white",
    icon="fn",
    icon_color_dark="text-indigo-400",
    icon_color_light="text-indigo-600",
    text_dark="text-slate-100",
    text_light="text-slate-900",
)

PIPELINE = NodeStyle(
    border_dark="border-amber-500/50",
    border_light="border-amber-400",
    bg_dark="bg-slate-800",
    bg_light="bg-white",
    icon="{}",
    icon_color_dark="text-amber-400",
    icon_color_light="text-amber-600",
    text_dark="text-slate-100",
    text_light="text-slate-900",
)

ROUTE = NodeStyle(
    border_dark="border-purple-500/50",
    border_light="border-purple-400",
    bg_dark="bg-slate-800",
    bg_light="bg-white",
    icon="?",
    icon_color_dark="text-purple-400",
    icon_color_light="text-purple-600",
    text_dark="text-slate-100",
    text_light="text-slate-900",
)

DATA = NodeStyle(
    border_dark="border-emerald-500/50",
    border_light="border-emerald-400",
    bg_dark="bg-slate-800",
    bg_light="bg-white",
    icon="o",
    icon_color_dark="text-emerald-400",
    icon_color_light="text-emerald-600",
    text_dark="text-slate-100",
    text_light="text-slate-900",
    min_width=100,
    header_height=32,
)

DATA_BOUND = NodeStyle(
    border_dark="border-slate-500/30",
    border_light="border-slate-400/50",
    bg_dark="bg-slate-800/50",
    bg_light="bg-slate-100",
    icon="o",
    icon_color_dark="text-slate-500",
    icon_color_light="text-slate-400",
    text_dark="text-slate-400",
    text_light="text-slate-500",
    min_width=100,
    header_height=32,
)

# Registry for lookup
STYLES: dict[str, NodeStyle] = {
    "FUNCTION": FUNCTION,
    "PIPELINE": PIPELINE,
    "ROUTE": ROUTE,
    "DATA": DATA,
    "DATA_BOUND": DATA_BOUND,
}


def get_style(node_type: str) -> NodeStyle:
    """Get style for a node type, with fallback to FUNCTION."""
    return STYLES.get(node_type, FUNCTION)


def get_tailwind_classes(node_type: str, theme: str) -> dict[str, str]:
    """Get Tailwind classes for a node type and theme.

    Args:
        node_type: One of FUNCTION, PIPELINE, ROUTE, DATA, DATA_BOUND
        theme: Either "dark" or "light"

    Returns:
        Dict with keys: border, bg, icon_color, text
    """
    style = get_style(node_type)
    is_dark = theme == "dark"

    return {
        "border": style.border_dark if is_dark else style.border_light,
        "bg": style.bg_dark if is_dark else style.bg_light,
        "icon_color": style.icon_color_dark if is_dark else style.icon_color_light,
        "text": style.text_dark if is_dark else style.text_light,
    }
