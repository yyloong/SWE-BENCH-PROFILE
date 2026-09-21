import json,sys
from pathlib import Path
import pull_images
p=Path(__file__).resolve().parent
rows=json.loads((p/'selected.json').read_text())
d=next(d for d in rows if d['instance_id']==sys.argv[1])
r=pull_images.pull(d)
raise SystemExit(0 if r['returncode']==0 else 1)
