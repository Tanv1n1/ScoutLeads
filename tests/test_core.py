import json
import re
from unittest.mock import patch

from app.common.text import EMAIL_RE
from app.enrichment.lead import LeadEnricher
import enrich_cli
from app.enrichment.spoc import SpocFinder
from app.enrichment.people import local_matches_person, name_parts, phones_in, json_ld
from app.enrichment.sources import site as site_src

E = LeadEnricher()

def test_email_regex_rejects_pipe_and_extracts():
    assert re.findall(EMAIL_RE, "reach me: a.b+c@sub.example.co.uk!") == ["a.b+c@sub.example.co.uk"]
    assert re.findall(EMAIL_RE, "x@y.c|m") == []


def test_blacklist_matches_domains_not_substrings():
    assert E._is_valid_email("me@mytest.com")
    assert not E._is_valid_email("a@test.com")
    assert not E._is_valid_email("a@sub.sentry.io")
    assert not E._is_valid_email("logo@2x.png")


def test_role_inbox_is_not_a_name_pattern():
    assert E._detect_pattern("info") is None
    assert E._detect_pattern("jane.doe") == "first.last"
    assert E._apply_pattern("f.last", "jane", "doe", "a.com") == "j.doe@a.com"
    assert E._apply_pattern("nope", "jane", "doe", "a.com") is None


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
    assert judge({"smtp": "unknown", "catch_all": None, "api": {"hunter": "valid"}}) == "valid"
    assert judge({"smtp": "accepted", "catch_all": True, "api": {"hunter": "invalid"}}) == "invalid"
    assert judge({"smtp": "accepted", "catch_all": True, "api": {}}) == "undecided"

