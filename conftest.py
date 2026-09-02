import pathlib
import sys

# Make `scripts/` importable from tests (the offline harness has an acceptance test).
sys.path.insert(0, str(pathlib.Path(__file__).parent))
