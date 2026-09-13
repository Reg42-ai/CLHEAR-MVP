"""Restricted-license sources (Class D): ISO 27001, SOC 2 TSC, PCI DSS, IFRS.

Same verbatim pipeline. When a BYOL file is present under
`s3://…/restricted/{source_key}/` it is parsed (PDF/HTML/text) and stored with
`public_ok=false`. When the prefix is empty we persist a placeholder version
— structure and hashes only, no invented publisher text — so the source is a
locked corpus row, not a missing adapter.
"""
from datetime import date

from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.settings import get_settings


def _list_restricted_objects(source_key: str) -> list[tuple[str, bytes, str]]:
    settings = get_settings()
    bucket = settings.clhear_datalake_bucket
    prefix = f"restricted/{source_key}/"
    try:
        import boto3

        client = boto3.client("s3", region_name=settings.aws_region)
        resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        out = []
        for obj in resp.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            name = key.rsplit("/", 1)[-1]
            ctype = "application/pdf" if body[:5] == b"%PDF-" else "application/octet-stream"
            out.append((name, body, ctype))
        return out
    except Exception:
        return []


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
            license_ref="BYOL — public APIs omit raw_text until a licensed file is in restricted/",
            canonical_url=self._url,
            adapter=self.key,
            short_name=self._title,
            version_policy="edition",
        )

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        files = _list_restricted_objects(self._source_key)
        today = date.today()
        if not files:
            placeholder = (
                f"{self._title}\n"
                "No BYOL file in restricted/. Structure and hashes only."
            ).encode()
            tree = [
                DocNode(
                    node_type="title",
                    ref=self._source_key,
                    heading=self._title,
                    children=[
                        DocNode(
                            node_type="note",
                            ref=f"{self._source_key}/locked",
                            heading="License-restricted — BYOL pending",
                            raw_text="No BYOL file in restricted/. Structure and hashes only.",
                        )
                    ],
                )
            ]
            return FetchResult(
                version_label=f"edition:byol-pending",
                artifacts=[Artifact(name="placeholder.txt", content=placeholder, content_type="text/plain")],
                tree=tree,
                version_kind="edition",
                as_of_date=today,
            )
        name, body, ctype = files[0]
        if body[:5] == b"%PDF-":
            from app.clhear.l1.adapters.pdf_docling import pages_to_tree, extract_pdf_pages

            tree = pages_to_tree(extract_pdf_pages(body), self._source_key, self._title)
        else:
            text = body.decode("utf-8", errors="replace")
            tree = [
                DocNode(
                    node_type="title",
                    ref=self._source_key,
                    heading=self._title,
                    children=[DocNode(node_type="paragraph", raw_text=text)],
                )
            ]
        return FetchResult(
            version_label=f"edition:{today.isoformat()}",
            artifacts=[Artifact(name=name, content=body, content_type=ctype)],
            tree=tree,
            version_kind="edition",
            as_of_date=today,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        spans: list[str] = []
        for artifact in artifacts:
            if artifact.content[:5] == b"%PDF-":
                from app.clhear.l1.adapters.pdf_docling import extract_pdf_pages

                for page in extract_pdf_pages(artifact.content):
                    spans.extend(p.strip() for p in page.splitlines() if p.strip())
            else:
                text = artifact.content.decode("utf-8", errors="replace")
                spans.extend(line.strip() for line in text.splitlines() if line.strip())
        return spans
