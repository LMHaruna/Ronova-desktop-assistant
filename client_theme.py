"""Shared colors for the desktop client's widgets and custom painters."""

import json
from pathlib import Path
from string import Template


COLORS = json.loads((Path(__file__).resolve().parent / 'design/tokens.json').read_text(encoding='utf-8'))['colors']


def themed(style):
    return Template(style).substitute(COLORS)
