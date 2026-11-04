# Video shapes for the sweeps: scenarios/shapes.json, one entry per shape with
# the aiomoqt --video profile, its nominal Mbps, and the moqperf object sizes
# and timing that reproduce that bitrate. Sourced; needs python3.
SHAPES_JSON=${SHAPES_JSON:-$(dirname "${BASH_SOURCE[0]}")/../scenarios/shapes.json}

# shape_field <shape> <field>: prints the value, fails on an unknown shape or field.
shape_field() {
  python3 - "$SHAPES_JSON" "$1" "$2" <<'PY'
import json, sys
path, shape, field = sys.argv[1:]
shapes = json.load(open(path))
if shape not in shapes or field not in shapes[shape]:
    sys.exit(f"unknown shape or field: {shape} {field} (shapes: {' '.join(shapes)})")
print(shapes[shape][field])
PY
}
