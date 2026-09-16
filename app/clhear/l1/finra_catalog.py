"""FINRA publication and exposed archive metadata decoder, called by workers."""
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from app.clhear.l1.publisher_catalogs import form_continuations, language_metadata


def decoder(classify):
    def decode(body, page):
        if body.startswith(b"%PDF-"):
            return {"entries": [], "links": [], "findings": []}
        soup = BeautifulSoup(body, "html.parser")
        area = soup.find("main") or soup.find("article") or soup.body or soup
        if re.search(r"access denied|solve this CAPTCHA", area.get_text(" ", strip=True), re.I):
            raise ValueError("FINRA catalog access unavailable")
        links, entries, findings = {}, {}, []
        if page["role"] == "document":
            own = classify(page["url"], page)
            if own and own.get("entry"):
                entry = own["entry"]
                title = soup.find("h1")
                if title:
                    entry["name"] = entry["short_name"] = title.get_text(" ", strip=True)
                evidence = language_metadata(body, publisher_id="finra", document_key=entry["key"], url=page["url"])
                if evidence:
                    entry["language_evidence"] = evidence
                entries[entry["key"]] = entry
        for a in area.find_all("a", href=True):
            if a.find_parent(["header", "footer", "aside"]):
                continue
            raw = urljoin(page["url"], a["href"])
            found = classify(raw, page)
            if not found:
                if "next" in (a.get("rel") or []):
                    findings.append({"code": "unsupported_pagination", "detail": "An exposed next-page link requires an expanded FINRA catalog contract.", "link_url": raw})
                elif re.search(r"\.pdf(?:\?|$)", raw, re.I):
                    findings.append({"code": "finra_attachment_host_unverified", "detail": "A linked original is outside reviewed FINRA distribution hosts.", "link_url": raw})
                continue
            if found["url"] == page["url"]:
                continue
            links[found["url"]] = {k: found[k] for k in ("url", "source_key", "category", "role")}
            if found.get("entry"):
                entry = found["entry"]
                if a.get_text(strip=True):
                    entry["name"] = a.get_text(" ", strip=True)
                entry["catalog_evidence"] = {"url": page["url"], "method": "official_link"}
                if page["role"] == "document":
                    entry["related_document_key"] = page["source_key"]
                entries[entry["key"]] = entry
        form_links, form_findings = form_continuations(area, page, classify)
        links.update({link["url"]: link for link in form_links})
        findings.extend(form_findings)
        if area.select("[data-drupal-views-infinite-scroll-content-wrapper], [data-total-pages]"):
            findings.append({"code": "dynamic_enumeration_unverified", "detail": "FINRA dynamic continuation requires a verified endpoint contract."})
        if page["role"] == "collection" and not links:
            findings.append({"code": "empty_discovery_index", "detail": "Collection returned no supported records or continuation."})
        return {"entries": list(entries.values()), "links": list(links.values()), "findings": findings,
                "catalog_metadata": {"method": "official_links_and_observed_get_facets", "observed_links": len(links)}}
    return decode
