import re
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / 'static'
BLOCK = re.compile(r'(:root(?:\[data-theme="dark"\])?)\s*\{([^}]*)\}')
DECLARATION = re.compile(r'(--[\w-]+)\s*:\s*([^;]+)')
REFERENCE = re.compile(r'^var\((--[\w-]+)\)$')
HEX = re.compile(r'^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')

TEXT_MIN = 4.5
NON_TEXT_MIN = 3.0

TEXT = ['--text', '--text-2', '--text-3', '--text-faint', '--accent', '--accent-ink', '--pos', '--neg', '--warn', '--info']
BACKGROUNDS = ['--bg-0', '--surface', '--surface-2', '--surface-3', '--surface-raised']
SEMANTIC_TEXT = [
    ('--pos', '--pos-soft'), ('--neg', '--neg-soft'), ('--warn', '--warn-soft'), ('--info', '--info-soft'),
    ('--accent', '--accent-soft'), ('--accent-ink', '--accent-soft'), ('--neg-strong', '--neg-soft'),
    ('--exit-active-text', '--exit-active-bg'), ('--exit-stop-text', '--exit-stop-bg'),
    ('--exit-pending-text', '--exit-pending-bg'), ('--pressed-text', '--pressed-bg'),
    ('--danger-text', '--surface'), ('--danger-text', '--surface-raised'), ('--danger-text', '--danger-hover-bg'),
    ('--text', '--row-selected'), ('--text-2', '--row-selected'), ('--surface', '--text'),
    ('--chart-text', '--chart-bg'), ('--on-fill', '--booked-from'), ('--on-fill', '--booked-to'),
    ('--accent-ink', '--accent-stripe'), ('--neg', '--neg-stripe'),
]
NON_TEXT = [('--accent', background) for background in BACKGROUNDS] + [
    ('--control-border', '--surface'), ('--control-border', '--surface-raised'),
    ('--danger-border', '--surface'), ('--danger-border', '--surface-raised'),
] + [(line, '--chart-bg') for line in [
    '--chart-up', '--chart-down', '--chart-entry', '--chart-pending', '--chart-level',
    '--chart-equity', '--chart-drawdown', '--chart-legacy',
]]
def blocks(path):
    css = re.sub(r'/\*.*?\*/', '', path.read_text(), flags=re.S)
    found = {}
    for selector, body in BLOCK.findall(css):
        assert selector not in found, f'{path.name} has more than one {selector} block'
        found[selector] = dict((name, value.strip()) for name, value in DECLARATION.findall(body))
    return found


def themes():
    base = blocks(STATIC / 'styles.css')[':root']
    workspace = blocks(STATIC / 'workspace.css')
    light = {**base, **workspace[':root']}
    return {'light': light, 'dark': {**light, **workspace[':root[data-theme="dark"]']}}


THEMES = themes()


def resolve(tokens, name):
    value = tokens[name]
    while reference := REFERENCE.match(value):
        value = tokens[reference.group(1)]
    match = HEX.match(value)
    assert match, f'{name} must be a solid hex color, got {value!r}'
    digits = match.group(1)
    if len(digits) == 3:
        digits = ''.join(c * 2 for c in digits)
    return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))


def luminance(rgb):
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(tokens, foreground, background):
    first, second = luminance(resolve(tokens, foreground)), luminance(resolve(tokens, background))
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def cases():
    text = [(fg, bg) for fg in TEXT for bg in BACKGROUNDS] + SEMANTIC_TEXT
    for theme in THEMES:
        for pair, minimum in [*((p, TEXT_MIN) for p in text), *((p, NON_TEXT_MIN) for p in NON_TEXT)]:
            yield pytest.param(theme, *pair, minimum, id=f'{theme}:{pair[0]}-on-{pair[1]}')


@pytest.mark.parametrize('theme,foreground,background,minimum', list(cases()))
def test_token_pair_meets_wcag_contrast(theme, foreground, background, minimum):
    ratio = contrast(THEMES[theme], foreground, background)
    assert ratio >= minimum, f'{theme} {foreground} on {background}: {ratio} < {minimum}'


def test_luminance_uses_wcag_reference_values():
    assert luminance((1, 1, 1)) == pytest.approx(1)
    assert luminance((0, 0, 0)) == 0
    assert contrast({'--a': '#000', '--b': '#fff'}, '--a', '--b') == pytest.approx(21)
    assert contrast({'--a': '#767676', '--b': '#ffffff'}, '--a', '--b') == pytest.approx(4.54, abs=0.01)


def test_dark_theme_overrides_every_color_token_it_inherits():
    light, dark = THEMES['light'], THEMES['dark']
    colors = [name for name, value in light.items() if HEX.match(value) or value.startswith('rgb(')]
    assert [name for name in colors if dark[name] == light[name]] == []
