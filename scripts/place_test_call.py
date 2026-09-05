"""M3 dev only: place a call that points at an arbitrary TwiML path.

The /calls endpoint always uses /twiml/voice (the conference path). For M3 data
capture we sometimes want a different TwiML (e.g. /twiml/holdmusic-test). Remove
after M3.

    uv run python scripts/place_test_call.py +15144972287 /twiml/holdmusic-test
"""

from __future__ import annotations

import sys

from dialpass.config import get_settings
from dialpass.telephony.twilio_client import TwilioClient


def main(argv: list[str]) -> None:
    to_number, twiml_path = argv[1], argv[2]
    s = get_settings()
    client = TwilioClient(s.twilio_account_sid, s.twilio_auth_token, s.twilio_from_number)
    call = client._client.calls.create(
        to=to_number,
        from_=s.twilio_from_number,
        url=f"{s.public_base_url}{twiml_path}",
    )
    print(f"placed {call.sid} -> {to_number} ({twiml_path})")


if __name__ == "__main__":
    main(sys.argv)
