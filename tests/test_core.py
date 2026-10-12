import json
from unittest.mock import patch

from app.common.text import emails_in, extract_email, parse_abbreviated_number, phones_in, valid_email
from app.enrichment.lead import LeadEnricher
from app.scouting import discovery
import enrich_cli
from app.enrichment.erp import contact_id, to_lead_contact
from app.enrichment.spoc import SpocFinder
from app.enrichment.people import local_matches_person, name_parts, json_ld
from app.enrichment.sources import site as site_src

E = LeadEnricher()

SITE = """<title>Acme Digital | Agency</title>
We ship worldwide. Pricing: $2,000 per month retainer, billed monthly. Pay via Stripe. Prices in USD and GBP.
<a href="mailto:accounts@acme.in">accounts@acme.in</a> <p>jane.diaz@acme.in</p> <img src="logo@2x.png">
Call +91 98765 43210"""


def test_email_regex_rejects_pipe_and_extracts():
    assert extract_email("reach me: a.b+c@sub.example.co.uk!") == "a.b+c@sub.example.co.uk"
    assert extract_email("x@y.c|m") == ""


def test_blacklist_matches_domains_not_substrings():
    assert valid_email("me@mytest.com")
    assert not valid_email("a@test.com")
    assert not valid_email("a@sub.sentry.io")
    assert not valid_email("logo@2x.png")


def test_phone_needs_real_shape():
    assert phones_in("followers 1234567890123") == []
    assert phones_in("call +91 98765 43210 or 2024") == ["+919876543210"]
    assert phones_in("(415) 555-2671") == ["4155552671"]
    assert phones_in("id 1111111111") == []
    assert phones_in("bio 9876543210 and 02240001234") == ["9876543210", "02240001234"]


def test_abbrev_numbers():
    assert parse_abbreviated_number("1.2K") == 1200
    assert parse_abbreviated_number("2M") == 2_000_000


def test_role_inbox_is_not_a_name_pattern():
    assert E._detect_pattern("info") is None
    assert E._detect_pattern("jane.doe") == "first.last"
    assert E._apply_pattern("f.last", "jane", "doe", "a.com") == "j.doe@a.com"
    assert E._apply_pattern("nope", "jane", "doe", "a.com") is None








def test_analyze_site_signals():
    with patch.object(E, "_fetch_page", side_effect=lambda u: SITE if u.rstrip("/") == "https://acme.in" else None):
        lead = discovery.analyze_site("https://acme.in", E)
    assert lead["cross_border"] and lead["recurring"]
    assert {"ships_worldwide", "multi_currency", "subscription", "billing_provider"} <= set(lead["signals"].split(","))
    assert lead["own_emails"][0] == "accounts@acme.in"      # role inbox ranked first
    assert lead["phone"] == "+919876543210"


PAGE = """<h2>Our Team</h2><div>Priya Nair, Founder &amp; CEO<br><a href="mailto:priya@acme.in">email</a> +91 98765 43210</div>
<div>Rohit Das, CTO rohit.das@acme.in</div><p>General: info@acme.in</p>"""
OK = {"mx": True, "smtp": "accepted", "catch_all": False, "api": None}
CATCH_ALL = {"mx": True, "smtp": "accepted", "catch_all": True, "api": None}


def finder(verify=OK, pages=None):
    f = SpocFinder(enricher=LeadEnricher(), smtp=True)
    f._pages["acme.in"] = pages if pages is not None else {"/team": PAGE}
    f.verify = lambda email: dict(verify)
    return f


def test_name_matching():
    assert name_parts("Dr. Priya Nair") == ("priya", "nair")
    assert local_matches_person("priya.nair", "priya", "nair") and local_matches_person("pnair", "priya", "nair")
    assert not local_matches_person("info", "priya", "nair") and not local_matches_person("rohit.das", "priya", "nair")


def test_phones_in_filters_noise():
    assert phones_in("call +91 98765 43210 or 2024") == ["+919876543210"]
    assert phones_in("id 1111111111") == []
    assert phones_in("bio 9876543210 and 02240001234") == ["9876543210", "02240001234"]


def test_spoc_found_on_site_and_server_verified():
    r = finder().find({"lead_id": 1, "name": "Priya Nair", "domain": "acme.in", "company": "Acme"})
    assert (r["email"], r["email_status"], r["email_confidence"]) == ("priya@acme.in", "VERIFIED", 95)
    assert r["phone"] == "+919876543210" and r["phone_scope"] == "person"
    assert r["email_sources"] == ["site"] and "info@acme.in" in r.get("company_inboxes", [])


def test_catch_all_site_listed_email_is_likely_not_verified():
    r = finder(verify=CATCH_ALL).find({"name": "Priya Nair", "domain": "acme.in"})
    assert r["email"] == "priya@acme.in" and r["email_status"] == "LIKELY"


def test_rejected_candidate_is_never_returned():
    f = finder(verify={"mx": True, "smtp": "rejected", "catch_all": False, "api": None})
    r = f.find({"name": "Priya Nair", "domain": "acme.in"})
    assert r["email"] == "" and r["email_status"] == "NOT_FOUND"


def test_guess_needs_server_confirmation():
    f = finder(verify={"mx": True, "smtp": "unknown", "catch_all": None, "api": None}, pages={"/": "nothing here"})
    r = f.find({"name": "Priya Nair", "domain": "acme.in"})
    assert r["email"] == "" and r["other_candidates"] and "email_unverified" in r["gaps"]
    r = finder(pages={"/": "nothing here"}).find({"name": "Priya Nair", "domain": "acme.in"})
    assert r["email_status"] == "VERIFIED" and r["email_sources"] == ["pattern"]


def test_provided_values_are_kept_and_checked():
    r = finder().find({"name": "Priya Nair", "domain": "acme.in", "email": "priya@acme.in", "phone": "+911111222233"})
    assert r["phone"] == "+911111222233" and r["phone_scope"] == "provided"
    assert set(r["email_sources"]) == {"provided", "site"}


def test_missing_domain_is_a_gap_not_a_guess():
    f = SpocFinder(enricher=LeadEnricher(), smtp=False)
    r = f.find({"name": "A B", "company": "Acme"})
    assert r["gaps"] == ["domain_unresolved"] and r["email"] == ""


def test_cli_batches_and_resumes(tmp_path):
    inp, out = tmp_path / "in.json", tmp_path / "out.json"
    inp.write_text(json.dumps([{"lead_id": i, "name": "A B", "domain": "x.in"} for i in range(3)]))
    with patch.object(SpocFinder, "find", side_effect=lambda s: {"lead_id": s["lead_id"], "email": "a@x.in"}):
        enrich_cli.main([str(inp), str(out), "--batch", "2", "--no-smtp"])
        assert len(json.loads(out.read_text())) == 2
        enrich_cli.main([str(inp), str(out), "--batch", "2", "--no-smtp"])
    assert len(json.loads(out.read_text())) == 3


LD = """<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Organization","name":"Acme","email":"mailto:hello@acme.in","telephone":"+91 22 4000 1234"},
 {"@type":"Person","name":"Dr. Priya Nair","jobTitle":"CEO","email":"p.nair@acme.in","telephone":"+91 98765 43210"}]}</script>"""


def test_json_ld_people_and_org():
    d = json_ld(LD)
    assert d["people"][0]["email"] == "p.nair@acme.in" and d["org_emails"] == ["hello@acme.in"]


def test_schema_person_email_beats_page_text():
    r = finder(pages={"/about": LD}).find({"name": "Priya Nair", "domain": "acme.in"})
    assert r["email"] == "p.nair@acme.in" and r["phone"] == "+919876543210" and r["phone_scope"] == "person"
    assert r["company_inboxes"] == ["hello@acme.in"]


def test_site_crawl_follows_homepage_links_and_sitemap():
    pages = {
        "https://a.in": '<a href="/meet-the-founders">x</a><a href="/blog/post">y</a><a href="https://other.com/team">z</a>',
        "https://a.in/meet-the-founders": "founders",
        "https://a.in/sitemap.xml": "<urlset><loc>https://a.in/leadership-board</loc><loc>https://a.in/shop</loc></urlset>",
        "https://a.in/leadership-board": "board",
    }
    got = site_src.crawl(lambda u: pages.get(u.rstrip("/") if u.count("/") > 2 else u), "a.in")
    assert "/meet-the-founders" in got and "/leadership-board" in got and "/blog/post" not in got


def test_api_verifier_can_decide_when_smtp_cannot():
    from app.enrichment.verify import judge
    assert judge({"smtp": "unknown", "catch_all": None, "api": {"zerobounce": "valid"}}) == "valid"
    assert judge({"smtp": "accepted", "catch_all": True, "api": {"zerobounce": "invalid"}}) == "invalid"
    assert judge({"smtp": "accepted", "catch_all": True, "api": {}}) == "undecided"



class FakeEnr:
    def __init__(self, **r): self.r = {"exists": False, "accept_all": False, "mx": True, "checked": True, "score": 0, **r}
    def _verify_email_smtp(self, email): return self.r


def _zb_verifier(monkeypatch, enr, zb):
    from app.enrichment import verify as ver
    monkeypatch.setenv("SCOUT_CACHE", "0"); monkeypatch.setenv("ZEROBOUNCE_API_KEY", "k"); monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(ver.Verifier, "_zerobounce", staticmethod(lambda e: (calls.append(e), zb)[1]))
    return ver.Verifier(enr, True), calls


def test_zerobounce_not_called_when_smtp_already_rejected(monkeypatch):
    v, calls = _zb_verifier(monkeypatch, FakeEnr(exists=False), {"status": "valid", "raw": {}})
    assert v.verify("x@a.com")["smtp"] == "rejected" and calls == []


def test_zerobounce_decides_catch_all(monkeypatch):
    from app.enrichment.verify import judge
    v, calls = _zb_verifier(monkeypatch, FakeEnr(exists=True, accept_all=True), {"status": "valid", "raw": {"status": "valid", "sub_status": ""}})
    res = v.verify("x@a.com")
    assert calls == ["x@a.com"] and judge(res) == "valid" and res["api_detail"]["zerobounce"]["status"] == "valid"


def test_zerobounce_invalid_maps_to_rejected(monkeypatch):
    from app.enrichment.verify import judge
    v, _ = _zb_verifier(monkeypatch, FakeEnr(exists=True), {"status": "invalid", "raw": {"status": "spamtrap", "sub_status": ""}})
    assert judge(v.verify("x@a.com")) == "invalid"


def test_zerobounce_failure_is_not_cached(monkeypatch, tmp_path):
    from app.common import cache
    v, _ = _zb_verifier(monkeypatch, FakeEnr(exists=True), None)
    monkeypatch.setenv("SCOUT_CACHE", "1"); monkeypatch.setenv("SCOUT_CACHE_DIR", str(tmp_path))
    v.verify("x@a.com")
    assert cache.get("verify:x@a.com") is None


def test_search_prefers_free_backends_and_falls_back(monkeypatch):
    from app.enrichment.sources import search as s
    monkeypatch.setenv("SCOUT_CACHE", "0")
    for k in ("SERPER_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.setenv(k, "x")
    assert s.backends() == ["serper", "tavily"]
    calls = []
    def serper(q, n): calls.append("serper"); raise s.httpx.HTTPError("down")
    def tavily(q, n): calls.append("tavily"); return [{"url": "https://u", "text": "a &amp; b"}]
    monkeypatch.setattr(s, "_serper", serper); monkeypatch.setattr(s, "_tavily", tavily)
    assert s.search("q") == [{"url": "https://u", "text": "a & b"}] and calls == ["serper", "tavily"]


def test_search_unavailable_without_keys(monkeypatch):
    from app.enrichment.sources import search as s
    for k in ("SERPER_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert not s.available() and s.search("q") == []


def test_emails_in_dedupes_filters_and_keeps_order():
    text = "b@acme.in a@acme.in B@ACME.IN me@example.com logo@2x.png x@acme.in"
    assert emails_in(text) == ["b@acme.in", "a@acme.in", "B@ACME.IN", "x@acme.in"]


RES = {"email": "priya@acme.in", "email_status": "VERIFIED", "phone": "+919876543210", "phone_scope": "person"}
SPOC = {"lead_id": "lm-1", "name": "Priya Nair", "role": "CEO", "din": "01234567", "data_provenance": "mca_statutory_filing"}


def test_erp_row_maps_fields_and_omits_empties():
    row = to_lead_contact(SPOC, RES)
    assert row == {"contactId": "lm-1__priya-nair", "name": "Priya Nair", "fullName": "Priya Nair", "designation": "CEO",
                   "din": "01234567", "workEmail": "priya@acme.in", "phoneNumber": "+919876543210",
                   "dataProvenance": "mca_statutory_filing", "consentBasis": "legitimate-interest-b2b",
                   "isPrimary": True, "leadId": "lm-1"}
    assert "linkedinUrl" not in row


def test_erp_email_below_threshold_is_not_written():
    low = {**RES, "email_status": "LIKELY"}
    assert "workEmail" not in to_lead_contact(SPOC, low)
    assert to_lead_contact(SPOC, low, "LIKELY")["workEmail"] == "priya@acme.in"


def test_erp_company_phone_is_not_a_persons_number():
    assert "phoneNumber" not in to_lead_contact(SPOC, {**RES, "phone_scope": "company"})
    assert to_lead_contact(SPOC, {**RES, "phone_scope": "provided"})["phoneNumber"] == "+919876543210"


def test_erp_overrides_and_default_provenance():
    row = to_lead_contact({"lead_id": 7, "name": "A B", "contact_id": "C-9", "is_primary": False, "consent_basis": "x"}, {})
    assert row["contactId"] == "C-9" and row["isPrimary"] is False and row["consentBasis"] == "x"
    assert row["dataProvenance"] == "public_web_sources" and "workEmail" not in row
    assert contact_id("L", "Dr. Ana  O'Neil") == "L__dr-ana-o-neil" and contact_id("L", "") == "L"


def test_cli_writes_erp_rows_and_supports_two_contacts_per_lead(tmp_path):
    inp, out, erp = tmp_path / "in.json", tmp_path / "out.json", tmp_path / "rows.json"
    inp.write_text(json.dumps([{"lead_id": "L1", "name": "A One", "domain": "x.in"}, {"lead_id": "L1", "name": "B Two", "domain": "x.in", "contact_id": "L1-b"}]))
    fake = lambda s: {"lead_id": s["lead_id"], "email": "a@x.in", "email_status": "VERIFIED", "phone": "", "phone_scope": ""}
    with patch.object(SpocFinder, "find", side_effect=fake):
        enrich_cli.main([str(inp), str(out), "--erp", str(erp), "--no-smtp"])
    rows = json.loads(erp.read_text())
    assert sorted(r["contactId"] for r in rows) == ["L1-b", "L1__a-one"] and all(r["workEmail"] == "a@x.in" for r in rows)
