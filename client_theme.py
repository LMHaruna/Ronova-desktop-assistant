"""Shared colors for the desktop client's widgets and custom painters."""

import json
import re
from pathlib import Path
from string import Template


LIGHT_COLORS = json.loads((Path(__file__).resolve().parent / 'design/tokens.json').read_text(encoding='utf-8'))['colors']
LIGHT_COLORS['link'] = '#2453A6'
DARK_COLORS = {**LIGHT_COLORS, **{
    'background': '#211F1F', 'surface': '#211F1F', 'elevated': '#302C2D',
    'text': '#F6F2F3', 'muted': '#C5B7BA', 'gray': '#A99A9D',
    'border': '#504B4D', 'selection': '#403A3D', 'disabled': '#95868A',
    'primary': '#B6323B', 'primaryHover': '#C8434C', 'error': '#B6323B',
    'alertBackground': '#341A1D', 'alertBorder': '#70434A', 'link': '#A9C7FF',
}}
COLORS = LIGHT_COLORS.copy()


def set_theme(name):
    palette = {'light': LIGHT_COLORS, 'dark': DARK_COLORS}[name]
    COLORS.clear()
    COLORS.update(palette)


def recolor_style(style, source, target):
    """Translate theme colors in a generated Qt stylesheet in one pass."""
    mapping = {source[key].lower(): target[key] for key in source if key in target}
    return re.sub(r'#[0-9a-fA-F]{6}\b', lambda match: mapping.get(match.group().lower(), match.group()), style)


def themed(style):
    return Template(style).substitute(COLORS)
