"""CPU sidecar restores 4b/9b only and never pulls 27b."""
import json

from app.clhear.platform import ollama_sidecar as sidecar


def test_cpu_models_never_include_27b():
    assert sidecar.CPU_MODELS == ("qwen3.5:4b", "qwen3.5:9b")
    assert all(not sidecar.is_gpu_only(m) for m in sidecar.CPU_MODELS)
    assert sidecar.is_gpu_only("qwen3.6:27b")
    assert sidecar.is_gpu_only("something:27b")


def test_is_cpu_manifest_key():
    assert sidecar.is_cpu_manifest_key("manifests/registry.ollama.ai/library/qwen3.5/4b")
    assert sidecar.is_cpu_manifest_key("ollama-models/manifests/registry.ollama.ai/library/qwen3.5/9b")
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
    )
    assert [s[0] for s in steps] == ["restore", "serve", "ready", "pull", "wait"]
    assert out["ready"] is True
    assert out["models"]["pulled"] == ["qwen3.5:4b"]
