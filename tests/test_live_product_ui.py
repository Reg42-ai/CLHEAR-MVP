"""Live product chrome: L1–L8 peers, public stand-ins for licensed slots."""
from app.clhear.l1.fleet import adapter_for
from app.clhear.l1.source_registry import S, source_role


STAND_INS = {
    "iso/27001-2022": "nvlpubs.nist.gov",
    "iso/27001-2022-amd1-2024": "nvlpubs.nist.gov",
    "aicpa/soc2-tsc": "nvlpubs.nist.gov",
    "pci/dss-v4": "eur-lex.europa.eu",
    "ifrs/standards": "eur-lex.europa.eu",
}


def test_licensed_slots_have_public_fetch_urls():
    by_key = {entry["key"]: entry for entry in S}
    for key, host in STAND_INS.items():
        entry = by_key[key]
        assert source_role(key) == "document"
        url = (entry.get("fetch") or {}).get("url") or ""
        assert host in url, (key, url)
        adapter = adapter_for(entry)
        assert adapter.meta().source_key == key
        assert getattr(adapter, "declaration_gap", None) is None


def test_layer_pages_share_peer_nav(client):
    for path in ("/", "/l1", "/l2", "/l3", "/l4", "/l5", "/l6", "/l7", "/l8", "/explore"):
        page = client.get(path)
        assert page.status_code == 200, path
        body = page.text
        for href in ("/l1", "/l2", "/l3", "/l4", "/l5", "/l6", "/l7", "/l8"):
            assert f'href="{href}"' in body, (path, href)
        assert "L1 Sources" not in body
        assert "Sources Explorer" not in body
