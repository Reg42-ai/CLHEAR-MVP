"""Ephemeral GPU: launch/terminate/orphan with a fake EC2 client."""
from datetime import datetime, timedelta, timezone

from app.clhear.models import gpu_sessions
from app.clhear.platform import gpu as gpu_mod
import sqlalchemy as sa


class _FakeEC2:
    def __init__(self):
        self.runs = []
        self.terminated = []
        self.instances = []

    def run_instances(self, **kwargs):
        self.runs.append(kwargs)
        inst = {"InstanceId": "i-gpu-test-1", "LaunchTime": datetime.now(timezone.utc)}
        self.instances.append(inst)
        return {"Instances": [inst]}

    def terminate_instances(self, InstanceIds):
        self.terminated.extend(InstanceIds)
        return {"TerminatingInstances": [{"InstanceId": i} for i in InstanceIds]}

    def describe_instances(self, Filters=None):
        return {"Reservations": [{"Instances": list(self.instances)}]}

    def describe_images(self, Owners=None, Filters=None):
        return {"Images": [{"ImageId": "ami-gpu", "CreationDate": "2026-01-01"}]}


def _factory(ec2):
    def factory(service, region):
        assert service == "ec2"
        return ec2
    return factory


def test_dry_run_when_subnet_missing(engine):
    out = gpu_mod.launch_nightly_gpu(engine)
    assert out["status"] == "dry-run"
    assert gpu_mod.is_gpu_open(engine) is False  # dry-run is not a live window
    # terminate still closes the ledger row
    done = gpu_mod.terminate_gpu(engine, out["id"])
    assert done["terminated"] is True


def test_launch_and_terminate_with_fake_ec2(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_GPU_SUBNET_ID", "subnet-1")
    monkeypatch.setenv("CLHEAR_GPU_SECURITY_GROUP_ID", "sg-1")
    monkeypatch.setenv("CLHEAR_GPU_AMI_ID", "ami-gpu")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    ec2 = _FakeEC2()
    out = gpu_mod.launch_nightly_gpu(engine, client_factory=_factory(ec2))
    assert out["status"] == "running"
    assert out["instance_id"] == "i-gpu-test-1"
    assert gpu_mod.is_gpu_open(engine) is True
    assert ec2.runs[0]["InstanceMarketOptions"]["MarketType"] == "spot"
    assert "shutdown -h +240" in ec2.runs[0]["UserData"]
    done = gpu_mod.terminate_gpu(engine, out["id"], client_factory=_factory(ec2))
    assert done["terminated"] is True
    assert "i-gpu-test-1" in ec2.terminated
    assert gpu_mod.is_gpu_open(engine) is False
    get_settings.cache_clear()


def test_orphan_guard_kills_stale_session(engine):
    old = datetime.now(timezone.utc) - timedelta(hours=6)
    with engine.begin() as conn:
        conn.execute(
            gpu_sessions.insert().values(
                id="gpu-old", instance_id="", instance_type="g6.xlarge",
                status="running", started_at=old, detail={},
            )
        )
    result = gpu_mod.orphan_guard(engine, now=datetime.now(timezone.utc))
    assert result["count"] >= 1
    with engine.connect() as conn:
        row = conn.execute(sa.select(gpu_sessions).where(gpu_sessions.c.id == "gpu-old")).one()
    assert row.status == "terminated"


def test_userdata_has_fuse_and_s3_cache():
    script = gpu_mod.userdata_script(cache_uri="s3://bucket/ollama-models", region="us-east-1")
    assert "shutdown -h +240" in script
    assert "aws s3 sync s3://bucket/ollama-models" in script
    assert "ollama pull qwen3.6:27b" in script
