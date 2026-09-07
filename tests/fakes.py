"""Test doubles — no network, record what they were asked."""

from __future__ import annotations

import threading

from dialpass.telephony.twilio_client import PlacedCall


class FakeTwilioClient:
    """Stands in for `TwilioClient`. Every method records its call so a test can
    assert on it; `place_outbound_call` / `ring_user` hand back deterministic SIDs."""

    def __init__(self, *, ring_user_raises: Exception | None = None) -> None:
        self.outbound: list[dict] = []
        self.user_rings: list[dict] = []
        self.digit_presses: list[dict] = []
        self.hangups: list[str] = []
        self.sms: list[dict] = []
        self._ring_user_raises = ring_user_raises

    def place_outbound_call(
        self, to_number: str, twiml_url: str, conference_name: str
    ) -> PlacedCall:
        self.outbound.append({"to": to_number, "url": twiml_url, "conference": conference_name})
        return PlacedCall(call_sid="CAagent0001", conference_name=conference_name)

    def ring_user(self, user_number: str, twiml_url: str, conference_name: str) -> PlacedCall:
        if self._ring_user_raises is not None:
            raise self._ring_user_raises
        self.user_rings.append({"to": user_number, "url": twiml_url, "conference": conference_name})
        return PlacedCall(call_sid="CAuser0001", conference_name=conference_name)

    def press_digits(self, call_sid: str, digits: str, reconnect_url: str) -> None:
        self.digit_presses.append(
            {"sid": call_sid, "digits": digits, "reconnect_url": reconnect_url}
        )

    def hang_up(self, call_sid: str) -> None:
        self.hangups.append(call_sid)

    def send_sms(self, to_number: str, body: str) -> None:
        self.sms.append({"to": to_number, "body": body})


class FakeSqsClient:
    """Stands in for a boto3 SQS client. Producer side records batches; consumer
    side replays scripted `receive_message` responses."""

    def __init__(self) -> None:
        self.sent_batches: list[list[dict]] = []
        self.deleted: list[dict] = []
        # producer knobs
        self.fail_times = 0  # raise from send_message_batch this many times first
        self.partial_fail_ids: set[str] = set()  # Ids to report Failed once
        self.block_send: threading.Event | None = None  # if set, send blocks on it
        # consumer knobs
        self._inbox: list[list[dict]] = []  # queued receive_message message-lists
        self.stop_when_empty: threading.Event | None = None

    # -- producer ------------------------------------------------------
    def send_message_batch(self, *, QueueUrl: str, Entries: list[dict]) -> dict:
        if self.block_send is not None:
            self.block_send.wait(timeout=5)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("sqs unavailable")
        self.sent_batches.append(list(Entries))
        failed = [
            {"Id": e["Id"], "SenderFault": False, "Code": "x"}
            for e in Entries
            if e["Id"] in self.partial_fail_ids
        ]
        self.partial_fail_ids = set()  # clear so the retry succeeds
        return {"Failed": failed} if failed else {}

    @property
    def sent_bodies(self) -> list[str]:
        return [e["MessageBody"] for batch in self.sent_batches for e in batch]

    # -- consumer -----------------------------------------------------
    def queue_messages(self, bodies: list[str]) -> None:
        self._inbox.append(
            [
                {"MessageId": f"m{i}", "ReceiptHandle": f"r{i}", "Body": b}
                for i, b in enumerate(bodies)
            ]
        )

    def receive_message(self, *, QueueUrl: str, MaxNumberOfMessages: int, WaitTimeSeconds: int):
        if self._inbox:
            return {"Messages": self._inbox.pop(0)}
        if self.stop_when_empty is not None:
            self.stop_when_empty.set()
        return {}

    def delete_message_batch(self, *, QueueUrl: str, Entries: list[dict]) -> dict:
        self.deleted.extend(Entries)
        return {}
