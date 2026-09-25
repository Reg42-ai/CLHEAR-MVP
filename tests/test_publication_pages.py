"""FTC guidance pages read as verified blocks on the US government lane."""
import pytest

from app.clhear.l1.adapters.base import Artifact
from app.clhear.l1.adapters.ftc_pages import FtcPageAdapter, is_ftc_page
from app.clhear.l1.adapters.publication_pages import original_text, parse, publication_title

FTC = """<html><head><title>Disclosures 101 | FTC</title></head><body>
<header><nav><ul><li>Business Guidance</li></ul></nav></header>
<main id="main-content"><h1 class="page-title">Disclosures 101 for Social Media Influencers</h1>
<article><div class="node__content"><div class="field field--name-body"><div class="field__items"><div class="field__item">
<p>If you endorse a product through social media, your endorsement message should make it obvious when you have a
relationship (&ldquo;material connection&rdquo;) with the brand.</p>
<h2>How to disclose</h2>
<ul><li>Place it so it&rsquo;s hard to miss.</li><li>Use simple and clear language.</li></ul>
<p>Don&rsquo;t assume that followers know all your brand relationships.</p>
</div></div></div></div></article></main>
<footer><ul><li>Contact</li></ul></footer></body></html>""".encode()
KEY = "ftc/guidance/disclosures-101"


def test_ftc_page_blocks_group_under_headings_and_exclude_chrome():
    assert is_ftc_page(FTC)
    tree = parse(FTC, KEY, "FTC Disclosures 101")
    root = tree[0]
    assert publication_title(root) == "Disclosures 101 for Social Media Influencers"
    [group] = [n for n in root.children if n.node_type == "group" and n.source_locator["block"] == "heading"]
    assert group.heading == "How to disclose"
    assert [n.raw_text for n in group.children] == [
        "Place it so it’s hard to miss.", "Use simple and clear language.",
        "Don’t assume that followers know all your brand relationships."]
    text = original_text(FTC)
    assert "Business Guidance" not in text and "Contact" not in text


def test_ftc_page_verifies_through_original_projection():
    from app.clhear.l1.originals import attach_source_locations, verify_original_projection

    tree = parse(FTC, KEY, "FTC Disclosures 101")
    artifacts = [Artifact(name="publication.html", content=FTC, content_type="text/html")]
    assert attach_source_locations(KEY, "govinfo_us", artifacts, tree)
    report = verify_original_projection(KEY, "govinfo_us", artifacts, tree)
    assert report["verified"], report["findings"]
    tree[0].children[-1].children[0].raw_text = "Hide it."
    assert not verify_original_projection(KEY, "govinfo_us", artifacts, tree)["verified"]


def test_registry_routes_the_ftc_page_to_its_reader():
    from app.clhear.l1.fleet import adapter_for
    from app.clhear.l1.source_registry import S

    entry = next(s for s in S if s["key"] == KEY)
    adapter = adapter_for(entry)
    assert isinstance(adapter, FtcPageAdapter) and adapter.key == "govinfo_us"
    assert entry["tier"] == "guidance"


def test_landing_page_cannot_stand_in(monkeypatch):
    from app.clhear.l1 import http

    monkeypatch.setattr(http, "get", lambda url, headers=None: b"<html><body><h1>Search</h1></body></html>")
    with pytest.raises(ValueError, match="FTC guidance page"):
        FtcPageAdapter(KEY, "FTC", "https://www.ftc.gov/x", meta=None).fetch()
