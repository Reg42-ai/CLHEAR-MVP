"""Restricted-license sources (Class D): ISO 27001, SOC 2 TSC, PCI DSS, IFRS.

The pipeline must approve acquire/store/parse permissions before calling this
adapter. An uploaded file alone is not permission. A missing file raises an
actionable error; no placeholder artifact or invented version is created.
"""
import hashlib

from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.settings import get_settings


def _plain_text(body: bytes) -> str:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Unsupported authorized artifact format: provide PDF or valid UTF-8 plain text") from exc
    if any(ord(char) < 32 and char not in "\t\r\n\f" for char in text):
        raise ValueError("Unsupported authorized artifact format: binary content is not plain text")
    return text


def _list_restricted_objects(source_key: str) -> list[tuple[str, bytes, str]]:
    settings = get_settings()
    bucket = settings.clhear_datalake_bucket
    prefix = f"restricted/{source_key}/"
    try:
        import boto3

        client = boto3.client("s3", region_name=settings.aws_region)
        out = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if not key.startswith(prefix):
                    continue
                name = key[len(prefix):]
                # Only direct uploads are inputs. Pipeline originals/diffs live
                # in version subdirectories and must never become new inputs.
                if not name or "/" in name:
                    continue
                out.append((name, key))
        if len(out) > 1:
            raise ValueError(f"Ambiguous artifacts for {source_key}: provide exactly one authorized file directly under {prefix}")
        if not out:
            return []
        name, key = out[0]
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        ctype = "application/pdf" if body[:5] == b"%PDF-" else "application/octet-stream"
        return [(name, body, ctype)]
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Cannot read authorized artifacts for {source_key}: object-store access failed") from exc


class RestrictedFileAdapter:
    key = "restricted_file"

    def __init__(self, source_key: str, title: str, url: str = "", meta: SourceMeta | None = None):
        self._source_key = source_key
        self._title = title
        self._url = url
        self._meta = meta

    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            family_key="standards",
            family_name="Standards & frameworks",
            source_key=self._source_key,
            name=self._title,
            kind="standard",
            issuer="",
            jurisdiction="INTL",
            license="restricted",
            license_ref="Explicit permission evidence required; uploaded files do not grant processing or display rights",
            canonical_url=self._url,
            adapter=self.key,
            short_name=self._title,
            version_policy="edition",
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        files = _list_restricted_objects(self._source_key)
        if not files:
            raise FileNotFoundError(
                f"Awaiting authorized source artifact for {self._source_key}; provide the actual licensed file "
                "after recording explicit acquire/store/parse permissions. No placeholder version was ingested."
            )
        if len(files) != 1:
            raise ValueError(f"Ambiguous artifacts for {self._source_key}: select exactly one authorized source file")
        name, body, ctype = files[0]
        if not body:
            raise ValueError(f"Authorized source artifact for {self._source_key} is empty")
        if body[:5] == b"%PDF-":
            from app.clhear.l1.adapters.pdf_docling import pages_to_tree, extract_pdf_pages

            tree = pages_to_tree(extract_pdf_pages(body), self._source_key, self._title)
        else:
            text = _plain_text(body)
            tree = [
                DocNode(
                    node_type="title",
                    ref=self._source_key,
                    heading=self._title,
                    children=[DocNode(node_type="paragraph", raw_text=text)],
                )
            ]
        return FetchResult(
            # Identifies acquired bytes, not an invented publisher edition/date.
            version_label=f"edition:acquired-sha256-{hashlib.sha256(body).hexdigest()}",
            artifacts=[Artifact(name=name, content=body, content_type=ctype)],
            tree=tree,
            version_kind="edition",
            as_of_date=None,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        spans: list[str] = []
        for artifact in artifacts:
            if artifact.content[:5] == b"%PDF-":
                from app.clhear.l1.originals import pdf_original
                spans.append(pdf_original(artifact.content)[0])
            else:
                text = _plain_text(artifact.content)
                spans.extend(line.strip() for line in text.splitlines() if line.strip())
        return spans
