import os
import pathlib
import sys

# Make `scripts/` importable from tests (the offline harness has an acceptance test).
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# Tests must never pick up real credentials from a developer's .env — that would
# make test_api.py place an actual Twilio call / hit a real OpenAI key. Force the
# "unconfigured" path regardless of what's in .env.
for _key in (
    "DIALPASS_TWILIO_ACCOUNT_SID",
    "DIALPASS_TWILIO_AUTH_TOKEN",
    "DIALPASS_TWILIO_FROM_NUMBER",
    "DIALPASS_PUBLIC_BASE_URL",
    "DIALPASS_OPENAI_API_KEY",
):
    os.environ[_key] = ""
