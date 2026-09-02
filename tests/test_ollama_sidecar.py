"""CPU sidecar restores 4b/9b only and never pulls 27b."""
import json
import threading

from app.clhear.platform import ollama_sidecar as sidecar


def test_cpu_models_never_include_27b():
    assert sidecar.CPU_MODELS == ("qwen3.5:4b", "qwen3.5:9b")
    assert all(not sidecar.is_gpu_only(m) for m in sidecar.CPU_MODELS)
    assert sidecar.is_gpu_only("qwen3.6:27b")
    assert sidecar.is_gpu_only("something:27b")


def test_is_cpu_manifest_key():
    assert sidecar.is_cpu_manifest_key("manifests/registry.ollama.ai/library/qwen3.5/4b")
    assert sidecar.is_cpu_manifest_key("ollama-models/manifests/registry.ollama.ai/library/qwen3.5/9b")
    assert sidecar.is_cpu_manifest_key("ollama-models/models/manifests/registry.ollama.ai/library/qwen3.5/4b")
    assert not sidecar.is_cpu_manifest_key("manifests/registry.ollama.ai/library/qwen3.6/27b")
    assert not sidecar.is_cpu_manifest_key("manifests/registry.ollama.ai/library/qwen3.5/27b")


def test_blob_names_from_manifest():
    doc = {
        "config": {"digest": "sha256:aaa"},
        "layers": [{"digest": "sha256:bbb"}, {"digest": "sha256:bbb"}],
    }
    assert sidecar.blob_names_from_manifest(doc) == ["sha256-aaa", "sha256-bbb"]


class _FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.downloads = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        objects = self.objects

        class _P:
            def paginate(self, **kwargs):
                yield {"Contents": [{"Key": k} for k in objects]}

        return _P()

    def download_file(self, bucket, key, path):
        self.downloads.append((bucket, key, path))
        import os

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(self.objects[key])


def test_restore_skips_27b_and_keeps_cpu_blobs(tmp_path):
    manifest = json.dumps({
        "config": {"digest": "sha256:cpuconfig"},
        "layers": [{"digest": "sha256:cpulayer"}],
    }).encode()
    gpu_manifest = json.dumps({
        "config": {"digest": "sha256:gpuconfig"},
        "layers": [{"digest": "sha256:gpulayer"}],
    }).encode()
    objects = {
        "ollama-models/manifests/registry.ollama.ai/library/qwen3.5/4b": manifest,
        "ollama-models/manifests/registry.ollama.ai/library/qwen3.6/27b": gpu_manifest,
        "ollama-models/blobs/sha256-cpuconfig": b"cfg",
        "ollama-models/blobs/sha256-cpulayer": b"layer",
        "ollama-models/blobs/sha256-gpulayer": b"gpu-should-skip",
        "ollama-models/.keep": b"keep",
    }
    s3 = _FakeS3(objects)
    dest = tmp_path / "ollama"
    out = sidecar.restore_cpu_cache("s3://bucket/ollama-models", str(dest), s3_client=s3)
    assert out["restored"] is True
    keys = [k for _b, k, _p in s3.downloads]
    assert any(k.endswith("/qwen3.5/4b") for k in keys)
    assert not any("27b" in k for k in keys)
    assert not any("gpulayer" in k for k in keys)
    assert any(k.endswith("sha256-cpulayer") for k in keys)


def test_pull_refuses_27b():
    posts = []
    tags = {"models": [{"name": "qwen3.5:4b"}]}
    result = sidecar.pull_cpu_models(
        "http://127.0.0.1:11434",
        ["qwen3.5:4b", "qwen3.5:9b", "qwen3.6:27b"],
        http_post=lambda url, data: posts.append((url, data)),
        tags_payload=tags,
    )
    assert result["already"] == ["qwen3.5:4b"]
    assert result["pulled"] == ["qwen3.5:9b"]
    assert result["refused"] == ["qwen3.6:27b"]
    assert all(b"27b" not in data for _url, data in posts)


def test_run_sidecar_order(tmp_path):
    steps = []

    def restorer(uri, dest):
        steps.append(("restore", uri, dest))
        return {"restored": True, "files": 1}

    class _Proc:
        def wait(self):
            steps.append(("wait",))

    def server():
        steps.append(("serve",))
        return _Proc()

    def waiter(url):
        steps.append(("ready", url))
        return True

    def puller(url):
        steps.append(("pull", url))
        return {"pulled": ["qwen3.5:4b"], "already": [], "refused": []}

    out = sidecar.run_sidecar(
        cache_uri="s3://bucket/ollama-models",
        dest=str(tmp_path),
        restorer=restorer,
        server=server,
        waiter=waiter,
        puller=puller,
        wait_forever=True,
        metrics=False,
    )
    assert [s[0] for s in steps] == ["restore", "serve", "ready", "pull", "wait"]
    assert out["ready"] is True
    assert out["models"]["pulled"] == ["qwen3.5:4b"]


def test_parse_cpu_stat_and_deltas(tmp_path):
    first = (
        "usage_usec 1000\nuser_usec 800\nsystem_usec 200\n"
        "nr_periods 10\nnr_throttled 2\nthrottled_usec 400\n"
    )
    second = (
        "usage_usec 5000\nuser_usec 4000\nsystem_usec 1000\n"
        "nr_periods 20\nnr_throttled 5\nthrottled_usec 900\n"
    )
    parsed = sidecar.parse_cpu_stat(first)
    assert parsed["nr_throttled"] == 2
    assert parsed["throttled_usec"] == 400
    path = tmp_path / "cpu.stat"
    path.write_text(second)
    assert sidecar.read_cpu_stat(str(path))["nr_throttled"] == 5
    deltas = sidecar.throttle_deltas(parsed, sidecar.parse_cpu_stat(second))
    assert deltas["nr_throttled"] == 3
    assert deltas["throttled_usec"] == 500
    reset = sidecar.throttle_deltas({"nr_throttled": 9}, {"nr_throttled": 1})
    assert reset["nr_throttled"] == 0


def test_put_throttle_metrics_dimensions():
    seen = {}

    def putter(**kwargs):
        seen.update(kwargs)

    sidecar.put_throttle_metrics(
        {"nr_throttled": 2, "throttled_usec": 150},
        role="sidecar",
        putter=putter,
    )
    names = {item["MetricName"]: item for item in seen["MetricData"]}
    assert seen["Namespace"] == "CLHEAR"
    assert names["OllamaCpuThrottled"]["Value"] == 2
    assert names["OllamaCpuThrottled"]["Dimensions"] == [{"Name": "Role", "Value": "sidecar"}]
    assert names["OllamaCpuThrottledUsec"]["Value"] == 150


def test_publish_cpu_loop_emits_delta(tmp_path):
    path = tmp_path / "cpu.stat"
    path.write_text("nr_throttled 1\nthrottled_usec 10\nusage_usec 1\nnr_periods 1\n")
    puts = []
    stop = threading.Event()
    ticks = {"n": 0}

    def sleeper(seconds):
        ticks["n"] += 1
        if ticks["n"] == 1:
            path.write_text("nr_throttled 4\nthrottled_usec 40\nusage_usec 9\nnr_periods 3\n")
            return False
        stop.set()
        return True

    sidecar.publish_cpu_loop(
        stop,
        path=str(path),
        interval_s=0,
        role="gpu",
        putter=lambda **kw: puts.append(kw),
        sleeper=sleeper,
    )
    assert puts
    throttled = next(m for m in puts[0]["MetricData"] if m["MetricName"] == "OllamaCpuThrottled")
    assert throttled["Value"] == 3
    assert throttled["Dimensions"][0]["Value"] == "gpu"
