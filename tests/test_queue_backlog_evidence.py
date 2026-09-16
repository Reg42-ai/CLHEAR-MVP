"""Queue backlog evidence classifies samples and never names the DLQ for purge."""
import json

from scripts.queue_backlog_evidence import QUEUES, _classify_body, collect, purge_fleet_queues


def test_classify_uses_the_routing_table():
    body = json.dumps({"kind": "SourceChanged", "layer": "l1", "event_id": "1",
                       "subject_ref": "x", "producer": "t", "ts": "2026-09-16T00:00:00Z"})
    assert _classify_body(body)["category"] == "audit"
    held = json.dumps({"kind": "clhear.l2.changed", "layer": "l2", "event_id": "2",
                       "subject_ref": "x", "producer": "t", "ts": "2026-09-16T00:00:00Z"})
    assert _classify_body(held) == {"kind": "clhear.l2.changed", "category": "layer_event", "owner": None}
    assert _classify_body("not-json")["category"] == "malformed"


class FakeSqs:
    def __init__(self, queues, counts):
        self.queues = {url: list(messages) for url, messages in queues.items()}
        self.counts = counts
        self.purged = []

    def get_queue_url(self, QueueName):
        return {"QueueUrl": "https://sqs.us-east-1.amazonaws.com/730649732189/" + QueueName}

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        n = self.counts.get(QueueUrl, 0)
        return {"Attributes": {name: str(n if name == "ApproximateNumberOfMessages" else 0) for name in AttributeNames}}

    def receive_message(self, QueueUrl, **kwargs):
        batch = self.queues.get(QueueUrl, [])[:10]
        return {"Messages": batch} if batch else {}

    def purge_queue(self, QueueUrl):
        self.purged.append(QueueUrl.rsplit("/", 1)[-1])


def test_collect_samples_and_purge_skips_dlq():
    events = QUEUES["l1"]
    body = json.dumps({"kind": "SourceChanged", "layer": "l1", "event_id": "1",
                       "subject_ref": "x", "producer": "t", "ts": "2026-09-16T00:00:00Z"})
    dlq = "https://sqs.us-east-1.amazonaws.com/730649732189/clhear-events-dlq"
    sqs = FakeSqs(
        {events: [{"MessageId": "m1", "Body": body, "Attributes": {"ApproximateReceiveCount": "9"}}],
         dlq: [{"MessageId": "d1", "Body": body, "Attributes": {}}]},
        {events: 12, dlq: 99},
    )
    report = collect(sqs, sample_size=5)
    assert report["queues"]["l1"]["ApproximateNumberOfMessages"] == 12
    assert report["queues"]["l1"]["sample_kinds"] == {"SourceChanged:audit": 1}
    assert report["queues"]["dlq"]["ApproximateNumberOfMessages"] == 99
    purge_fleet_queues(sqs, report)
    assert "clhear-events" in report["purged"]
    assert "clhear-events-dlq" not in report["purged"]
    assert report["dlq_purged"] is False
    assert "clhear-events-dlq" not in sqs.purged
