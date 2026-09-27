"""Resolve archived run locations without editing historical configs or logs."""
import json
from pathlib import Path


def resolve_saved_path(path):
    path = Path(path).expanduser().resolve()
    if path.exists():
        return path
    roots = [*path.parents, Path(__file__).resolve().parents[2]/'manual-runs']
    for root in dict.fromkeys(roots):
        index = root/'details/path-map.json'
        if not index.is_file():
            continue
        data = json.loads(index.read_text())
        origins = [root, Path(data.get('original_root', str(root)))]
        moves = sorted(data['moves'], key=lambda move: len(Path(move['from']).parts), reverse=True)
        for move in moves:
            for origin in origins:
                try:
                    suffix = path.relative_to(origin/move['from'])
                except ValueError:
                    continue
                return (root/move['to']/suffix).resolve()
    return path
