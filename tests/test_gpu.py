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
        inst = {
            "InstanceId": "i-gpu-test-1",
            "LaunchTime": datetime.now(timezone.utc),
            "PrivateIpAddress": "10.0.1.20",
            "State": {"Name": "running"},
        }
        self.instances.append(inst)
        return {"Instances": [inst]}

    def terminate_instances(self, InstanceIds):
        self.terminated.extend(InstanceIds)
        return {"TerminatingInstances": [{"InstanceId": i} for i in InstanceIds]}

    def describe_instances(self, Filters=None, InstanceIds=None):
        insts = list(self.instances)
        if InstanceIds:
            insts = [i for i in insts if i["InstanceId"] in InstanceIds]
        return {"Reservations": [{"Instances": insts}]}

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
    out = gpu_mod.launch_nightly_gpu(engine, client_factory=_factory(ec2), sleeper=lambda _s: None)
    assert out["status"] == "launching"
    assert out["instance_id"] == "i-gpu-test-1"
    assert out["detail"]["private_ip"] == "10.0.1.20"
    assert out["detail"]["ollama_url"] == "http://10.0.1.20:11434"
    assert gpu_mod.is_gpu_open(engine) is True
    assert ec2.runs[0]["InstanceMarketOptions"]["MarketType"] == "spot"
    assert ec2.runs[0]["NetworkInterfaces"][0]["AssociatePublicIpAddress"] is True
    assert "SubnetId" not in ec2.runs[0]
    assert "shutdown -h +240" in ec2.runs[0]["UserData"]
    ready = gpu_mod.wait_for_ollama(
        engine, out["id"],
        http_get=lambda url: (200, b'{"models":[{"name":"qwen3.6:27b"}]}'),
        sleeper=lambda _s: None,
    )
    assert ready["ready"] is True
    assert ready["url"] == "http://10.0.1.20:11434"
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


def test_default_ami_picks_newest():
    class _EC2:
        def describe_images(self, Owners=None, Filters=None):
            names = [f["Values"][0] for f in (Filters or []) if f["Name"] == "name"]
            assert names
            assert any("x86_64-ebs" in n or n.endswith("-gpu-*") for n in names)
            return {"Images": [
                {"ImageId": "ami-old", "CreationDate": "2026-01-01T00:00:00.000Z"},
                {"ImageId": "ami-new", "CreationDate": "2026-08-20T19:35:21.000Z"},
            ]}

    assert gpu_mod._default_ami(_EC2()) == "ami-new"


def test_userdata_has_fuse_and_s3_cache():
    script = gpu_mod.userdata_script(cache_uri="s3://bucket/ollama-models", region="us-east-1")
    assert "shutdown -h +240" in script
    assert "aws s3 sync s3://bucket/ollama-models" in script
    assert "ollama pull qwen3.5:9b" in script
    assert "ollama pull qwen3.6:27b" in script
    assert "qwen3.5:14b" not in script
