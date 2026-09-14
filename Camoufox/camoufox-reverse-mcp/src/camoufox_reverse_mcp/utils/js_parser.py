"""ES2017 parser with an optional local, bundled Acorn fallback for modern JS.

No CDN access and no execution of input source. Acorn uses UTF-16 offsets; these
are converted to Python code-point offsets before source slicing/site reporting.
"""
from __future__ import annotations

import bisect
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

MAX_SOURCE_BYTES = 4 * 1024 * 1024


class JSNode(SimpleNamespace):
    # Match esprima's missing optional properties (e.g. a synthetic operator).
    def __getattr__(self, name):
        return None


def _convert_tree(value, offsets, line_starts):
    if isinstance(value, list):
        return [_convert_tree(v, offsets, line_starts) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: _convert_tree(v, offsets, line_starts) for k, v in value.items() if k not in ('start', 'end', 'range', 'loc')}
    if 'type' not in value:
        return JSNode(**result)
    start = offsets[value['start']]
    end = offsets[value['end']]
    result['range'] = [start, end]
    if 'loc' in value:
        def location(index):
            line = bisect.bisect_right(line_starts, index) - 1
            return JSNode(line=line + 1, column=index - line_starts[line])
        result['loc'] = JSNode(start=location(start), end=location(end))
    return JSNode(**result)


def parse_source(source: str, *, locations: bool = False):
    import esprima
    first_error = None
    try:
        return esprima.parseScript(source, options={'range': True, 'loc': locations, 'tolerant': False}), 'esprima'
    except Exception as exc:
        first_error = str(exc)
    node = shutil.which('node')
    if not node:
        raise ValueError(f'ES2017 parse failed ({first_error}); modern syntax needs Node.js for bundled Acorn')
    payload = json.dumps({'source': source, 'locations': locations}).encode()
    if len(payload) > MAX_SOURCE_BYTES:
        raise ValueError('modern JS parser input exceeds 4 MiB')
    bridge = Path(__file__).parents[1] / 'vendor' / 'parse.cjs'
    try:
        result = subprocess.run([node, '--max-old-space-size=256', str(bridge)], input=payload,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('modern JS parser exceeded 10 seconds') from exc
    if len(result.stdout) > 32 * 1024 * 1024:
        raise ValueError('modern JS parser output exceeds 32 MiB')
    if not result.stdout:
        raise ValueError('modern JS parser failed: ' + result.stderr.decode(errors='replace')[:300])
    output = json.loads(result.stdout)
    if result.returncode or output.get('error'):
        raise ValueError(f"ES2017 parse failed ({first_error}); {output.get('error', 'Acorn failed')}")
    offsets = []
    line_starts = [0]
    for index, char in enumerate(source):
        offsets.append(index)
        if ord(char) > 0xFFFF:
            offsets.append(index)
        if char in ('\n', '\u2028', '\u2029') or (char == '\r' and (index + 1 == len(source) or source[index + 1] != '\n')):
            line_starts.append(index + 1)
    offsets.append(len(source))
    return _convert_tree(output['tree'], offsets, line_starts), output['parser']
