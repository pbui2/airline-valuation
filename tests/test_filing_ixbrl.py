from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from conftest import sec_fixture_files
from airline_valuation.filing_ixbrl import parse_filing, resolve_locator, _parse_xml_fragment


BASE = '''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:us-gaap="urn:gaap" xmlns:dei="urn:dei" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><body><ix:header><ix:resources><xbrli:context id="c1"><xbrli:entity><xbrli:identifier scheme="urn:test">0001</xbrli:identifier><xbrli:segment>{dims}</xbrli:segment></xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context><xbrli:unit id="u1"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit></ix:resources></ix:header>{facts}</body></html>'''

def document(facts, *, dims="", period='<xbrli:instant>2025-12-31</xbrli:instant>'):
    return BASE.format(facts=facts, dims=dims, period=period).encode()


def test_ixbrl_fact_context_unit_locator_roundtrip():
    raw = document('<ix:nonFraction id="f1" name="us-gaap:Revenue" contextRef="c1" unitRef="u1" decimals="0" scale="3">12</ix:nonFraction>')
    result = parse_filing(raw, source_id="synthetic")
    fact = result["facts"][0]
    assert fact["concept"] == "us-gaap:Revenue"
    assert fact["concept_namespace"] == "urn:gaap"
    assert fact["context"]["entity"] == "0001"
    assert fact["context"]["entity_scheme"] == "urn:test"
    assert fact["context"]["period"] == {"type": "instant", "date": "2025-12-31"}
    assert fact["unit"]["measures"][0]["lexical"] == "iso4217:USD"
    assert fact["value"] == "12000"
    assert fact["locator"]["format"] == "html_dom"
    assert fact["locator"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert resolve_locator(raw, fact["locator"])
    assert fact["locator"]["path"] and fact["locator"]["content_sha256"]
    assert fact["locator"]["heading_sha256"] and fact["heading"] == fact["locator"]["heading_text"]
    headed = document('<h2>Revenue for fiscal year</h2><ix:nonFraction id="hh" name="us-gaap:Revenue" contextRef="c1" unitRef="u1">12</ix:nonFraction>')
    headed_fact = parse_filing(headed)["facts"][0]
    assert headed_fact["heading"] == "Revenue for fiscal year"
    assert resolve_locator(headed, headed_fact["locator"])
    tampered_heading = dict(headed_fact["locator"]); tampered_heading["heading_text"] = "altered"
    assert not resolve_locator(headed, tampered_heading)
    altered = dict(fact["locator"]); altered["path"] = [999]
    assert not resolve_locator(raw, altered)
    assert not resolve_locator(raw + b" ", fact["locator"])
    unknown_format = dict(fact["locator"], format="unknown")
    assert not resolve_locator(raw, unknown_format)
    legacy_unformatted = dict(fact["locator"])
    legacy_unformatted.pop("format")
    assert not resolve_locator(raw, legacy_unformatted)


def test_ixbrl_transform_and_scale_matrix():
    facts = ''.join([
        '<ix:nonFraction id="a" name="us-gaap:A" contextRef="c1" unitRef="u1" format="ixt:num-dot-decimal" scale="2">1,234.50</ix:nonFraction>',
        '<ix:nonFraction id="b" name="us-gaap:B" contextRef="c1" unitRef="u1" sign="-" scale="0">9</ix:nonFraction>',
        '<ix:nonFraction id="c" name="us-gaap:C" contextRef="c1" unitRef="u1" format="ixt:zerodash">—</ix:nonFraction>',
        '<ix:nonFraction id="d" name="us-gaap:D" contextRef="c1" unitRef="u1" format="custom:unknown">12</ix:nonFraction>',
    ])
    fs = parse_filing(document(facts))["facts"]
    assert [f["value"] for f in fs] == ["123450.00", "-9", "0", None]
    assert fs[3]["unresolved"]
    assert any(d["code"] == "unsupported-transform" for d in parse_filing(document(facts))["diagnostics"])


def test_ixbrl_ambiguity_and_dimension_matrix():
    dimensions = '<xbrldi:explicitMember dimension="us-gaap:ProductAxis">us-gaap:WidgetMember</xbrldi:explicitMember><xbrldi:typedMember dimension="us-gaap:TypedAxis"><xbrli:identifier>opaque</xbrli:identifier></xbrldi:typedMember>'
    raw = document('<ix:nonFraction id="f" name="us-gaap:Amount" contextRef="c1" unitRef="missing">1</ix:nonFraction>', dims=dimensions,
                   period='<xbrli:startDate>2025-01-01</xbrli:startDate>')
    result = parse_filing(raw)
    fact = result["facts"][0]
    assert fact["unresolved"] and fact["value"] is None
    assert any(d["code"] == "missing-unit" for d in result["diagnostics"])
    assert "typed-dimension" in fact["context"]["unsupported"]
    assert fact["context"]["period"]["type"] == "invalid-duration"
    assert fact["context"]["dimensions"][0]["member_namespace"] == "urn:gaap"
    assert "typed-dimension" in fact["context"]["unsupported"]
    assert {d["code"] for d in result["diagnostics"]} >= {"missing-unit"}
    valid_duration = document('<ix:nonFraction id="d" name="us-gaap:Duration" contextRef="c1" unitRef="u1">1</ix:nonFraction>', period='<xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate>')
    assert parse_filing(valid_duration)["facts"][0]["context"]["period"]["type"] == "duration"
    no_unit = document('<ix:nonFraction id="nu" name="us-gaap:NoUnit" contextRef="c1">1</ix:nonFraction>')
    no_unit_result = parse_filing(no_unit)
    assert no_unit_result["facts"][0]["unresolved"] and no_unit_result["facts"][0]["value"] is None
    assert any(d["code"] == "missing-numeric-unit" for d in no_unit_result["diagnostics"])
    duplicate_resources = document('<ix:nonFraction id="z" name="us-gaap:Z" contextRef="c1" unitRef="u1">4</ix:nonFraction>')
    duplicate_context = b'<xbrli:context id="c1"><xbrli:entity><xbrli:identifier scheme="urn:a">A</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>'
    duplicate_unit = b'<xbrli:unit id="u1"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
    duplicate_resources = duplicate_resources.replace(b'</ix:resources>', duplicate_context + duplicate_unit + b'</ix:resources>')
    duplicate_resources = duplicate_resources.replace(b'</ix:resources>', duplicate_context + duplicate_unit + b'</ix:resources>')
    duplicate_result = parse_filing(duplicate_resources)
    assert any(d["code"] == "duplicate-context-id" for d in duplicate_result["diagnostics"])
    assert duplicate_result["facts"][0]["context"] is None
    assert duplicate_result["facts"][0]["unresolved"]
    assert {d["code"] for d in duplicate_result["diagnostics"]} >= {"duplicate-unit-id"}
    conflict = document('<ix:nonFraction id="x1" name="us-gaap:X" contextRef="c1" unitRef="u1">1</ix:nonFraction><ix:nonFraction id="x2" name="us-gaap:X" contextRef="c1" unitRef="u1">2</ix:nonFraction>')
    conflict_result = parse_filing(conflict)
    assert all(f["unresolved"] and f["value"] is None for f in conflict_result["facts"])
    assert any(d["code"] == "conflicting-duplicate-facts" for d in conflict_result["diagnostics"])
    duplicate = document('<ix:nonFraction id="same" name="us-gaap:A" contextRef="c1" unitRef="u1">1</ix:nonFraction><ix:nonFraction id="same" name="us-gaap:B" contextRef="c1" unitRef="u1">2</ix:nonFraction>')
    dup_result = parse_filing(duplicate)
    assert sum(d["code"] == "duplicate-id" for d in dup_result["diagnostics"]) == 1
    assert all(f["unresolved"] for f in dup_result["facts"])
    fragment = _parse_xml_fragment("<p:item xmlns:p='urn:x'>ok</p:item>")
    assert fragment[0]["text"] == "ok"
    with pytest.raises(ValueError):
        _parse_xml_fragment("<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><x>&e;</x>")


def test_ixbrl_malformed_and_resource_limits():
    raw = document('<ix:nonFraction id="f" name="us-gaap:A" contextRef="c1" unitRef="u1">1</ix:nonFraction>')
    with pytest.raises(ValueError, match="byte limit"):
        parse_filing(raw, max_bytes=20)
    with pytest.raises(ValueError, match="token limit"):
        parse_filing(raw, max_tokens=3)
    with pytest.raises(ValueError, match="depth limit"):
        parse_filing(b'<a><b><c>deep</c></b></a>', max_depth=2)
    with pytest.raises(ValueError, match="fact limit"):
        parse_filing(raw, max_facts=0)
    with pytest.raises(ValueError):
        _parse_xml_fragment("<broken>")
    with pytest.raises(ValueError, match="doctype"):
        parse_filing(b'<!DOCTYPE html><html></html>')
    with pytest.raises(ValueError, match="entity"):
        parse_filing(b'<!DOCTYPE x [<!ENTITY e "x">]><html>&e;</html>')


def test_ixbrl_namespace_rebinding_and_shadowing():
    raw = b'''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:p="urn:outer"><ix:resources><xbrli:context id="c"><xbrli:entity><xbrli:identifier>e</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2025</xbrli:instant></xbrli:period></xbrli:context></ix:resources><ix:nonNumeric id="a" name="p:Outer" contextRef="c">a</ix:nonNumeric><section xmlns:p="urn:inner"><ix:nonNumeric id="b" name="p:Inner" contextRef="c">b</ix:nonNumeric></section><ix:nonNumeric id="c1" name="p:Outer" contextRef="c">c</ix:nonNumeric></html>'''
    facts = parse_filing(raw)["facts"]
    assert [(f["concept"], f["concept_namespace"]) for f in facts] == [
        ("p:Outer", "urn:outer"), ("p:Inner", "urn:inner"), ("p:Outer", "urn:outer")]


def test_preflight_counts_canonical_ix_tags_across_prefixes_and_rebinding():
    raw = b'''<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL"><i:nonNumeric id="a" name="d:Text" contextRef="c">a</i:nonNumeric><section xmlns:i="urn:foreign"><i:nonNumeric id="fake" name="d:Fake" contextRef="c">x</i:nonNumeric></section><section xmlns:z="http://www.xbrl.org/2013/inlineXBRL"><z:nonNumeric id="b" name="d:Text" contextRef="c">b</z:nonNumeric></section></html>'''
    with pytest.raises(ValueError, match="fact limit"):
        parse_filing(raw, max_facts=0)
    parsed = parse_filing(raw)
    assert [fact["id"] for fact in parsed["facts"]] == ["a", "b"]


def test_preflight_aggregates_continuation_chars_across_prefixes():
    raw = b'''<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL"><i:continuation id="a">abc</i:continuation><section xmlns:z="http://www.xbrl.org/2013/inlineXBRL"><z:continuation id="b">def</z:continuation></section></html>'''
    with pytest.raises(ValueError, match="continuation length limit"):
        parse_filing(raw, max_continuation_chars=5)


def test_unclosed_inline_fact_and_resource_markup_is_named_error():
    with pytest.raises(ValueError, match="unclosed Inline XBRL fact/resource"):
        parse_filing(b'<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL"><i:nonNumeric id="f">text</html>')
    with pytest.raises(ValueError, match="unclosed Inline XBRL fact/resource"):
        parse_filing(b'<html xmlns:x="http://www.xbrl.org/2003/instance"><x:unit id="u"></html>')


def test_preflight_rejects_mismatched_lexical_xbrl_closing_names():
    fact = b'''<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL" xmlns:z="http://www.xbrl.org/2013/inlineXBRL"><i:nonNumeric id="f" name="d:Text" contextRef="c">text</z:nonNumeric></html>'''
    with pytest.raises(ValueError, match="mismatched Inline XBRL closing tag"):
        parse_filing(fact)

    continuation = b'''<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL" xmlns:z="http://www.xbrl.org/2013/inlineXBRL"><i:continuation id="c">text</z:continuation></html>'''
    with pytest.raises(ValueError, match="mismatched Inline XBRL closing tag"):
        parse_filing(continuation)


def test_preflight_rejects_implicit_and_unclosed_xbrl_resource_descendants():
    valid = document('<ix:nonFraction id="f" name="us-gaap:A" contextRef="c1" unitRef="u1">1</ix:nonFraction>')
    malformed_measure = valid.replace(
        b'<xbrli:measure>iso4217:USD</xbrli:measure>',
        b'<xbrli:measure>iso4217:USD</xbrli:unit>')
    with pytest.raises(ValueError, match="unclosed Inline XBRL fact/resource"):
        parse_filing(malformed_measure)

    unclosed_measure = valid.replace(
        b'<xbrli:measure>iso4217:USD</xbrli:measure>',
        b'<xbrli:measure>iso4217:USD')
    with pytest.raises(ValueError, match="unclosed Inline XBRL fact/resource"):
        parse_filing(unclosed_measure)

    unclosed_continuation_child = b'''<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL" xmlns:x="http://www.xbrl.org/2003/instance"><i:continuation id="c"><x:measure>USD</i:continuation></html>'''
    with pytest.raises(ValueError, match="unclosed Inline XBRL fact/resource"):
        parse_filing(unclosed_continuation_child)


def test_preflight_keeps_ordinary_html_omitted_tag_repair():
    parsed = parse_filing(b'<html><body><p>one<p>two<li>first<li>second</body></html>')
    assert parsed["facts"] == []


def test_foreign_measure_does_not_resolve_xbrli_unit():
    valid = document('<ix:nonFraction id="f" name="us-gaap:A" contextRef="c1" unitRef="u1">1</ix:nonFraction>')
    foreign = valid.replace(b'<xbrli:measure>iso4217:USD</xbrli:measure>',
                            b'<fake:measure xmlns:fake="urn:foreign">iso4217:USD</fake:measure>')
    parsed = parse_filing(foreign)
    assert parsed["units"]["u1"]["measures"] == []
    assert parsed["facts"][0]["unresolved"] and parsed["facts"][0]["value"] is None


def test_numeric_unit_measure_qnames_and_structures_fail_closed():
    fact = '<ix:nonFraction id="f" name="us-gaap:A" contextRef="c1" unitRef="u1">1</ix:nonFraction>'
    base = document(fact)
    unbound = base.replace(b'<xbrli:measure>iso4217:USD</xbrli:measure>',
                           b'<xbrli:measure>missing:USD</xbrli:measure>')
    parsed = parse_filing(unbound)
    assert parsed["facts"][0]["unresolved"] and parsed["facts"][0]["value"] is None
    assert "unresolved-unit-measure-qname" in {item["code"] for item in parsed["diagnostics"]}

    rebound_concept = base.replace(b'xmlns:us-gaap="urn:gaap"', b'xmlns:us-gaap="urn:foreign"')
    fact_result = parse_filing(rebound_concept)["facts"][0]
    assert fact_result["concept_namespace"] == "urn:foreign"
    assert fact_result["unresolved"] is False  # Generic extraction remains supported; policy is namespace-pinned.

    rebound_measure = base.replace(b'xmlns:iso4217="http://www.xbrl.org/2003/iso4217"',
                                   b'xmlns:iso4217="urn:foreign"')
    rebound_unit = parse_filing(rebound_measure)["facts"][0]
    assert rebound_unit["unit"]["measures"][0]["namespace"] == "urn:foreign"
    assert not rebound_unit["unresolved"]  # The parser resolves expanded names; issuer policy pins the allowed namespace.

    multiple = base.replace(b'</xbrli:unit>', b'<xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>')
    divide = base.replace(b'<xbrli:measure>iso4217:USD</xbrli:measure>',
                          b'<xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitDenominator></xbrli:divide>')
    for raw in (multiple, divide):
        result = parse_filing(raw)
        assert result["facts"][0]["unresolved"] and result["facts"][0]["value"] is None
        assert result["units"]["u1"]["unsupported"]


def test_resource_serialization_namespace_amplification_is_bounded():
    from airline_valuation.filing_ixbrl import _Node, MAX_XML_FRAGMENT_CHARS, _xml_resource_fragment
    namespaces = {f"p{i}": "urn:" + ("x" * 80) for i in range(15000)}
    node = _Node("xbrli:unit", {}, namespaces)
    with pytest.raises(ValueError, match="XML resource serialization size limit"):
        _xml_resource_fragment(node)
    assert MAX_XML_FRAGMENT_CHARS == 1_000_000


def test_parse_aggregate_resource_serialization_limit(monkeypatch):
    import airline_valuation.filing_ixbrl as ixbrl

    monkeypatch.setattr(ixbrl, "MAX_XML_RESOURCE_TOTAL_CHARS", 500)
    raw = b'''<html xmlns:xbrli="http://www.xbrl.org/2003/instance">
      <xbrli:context id="c1"><xbrli:entity><xbrli:identifier>one</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:context id="c2"><xbrli:entity><xbrli:identifier>two</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
    </html>'''
    with pytest.raises(ValueError, match="XML resource aggregate serialization size limit exceeded"):
        ixbrl.parse_filing(raw)


def test_namespace_validation_alternate_prefixes_and_foreign_context_fields():
    alternate = b'''<html xmlns:i="http://www.xbrl.org/2013/inlineXBRL" xmlns:x="http://www.xbrl.org/2003/instance" xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:g="urn:gaap" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:t="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"><i:resources><x:context id="c"><x:entity><x:identifier>0001</x:identifier></x:entity><x:period><x:instant>2025-12-31</x:instant></x:period><x:scenario><fake:instant xmlns:fake="urn:foreign">1900</fake:instant></x:scenario></x:context><x:unit id="u"><x:measure>iso4217:USD</x:measure></x:unit></i:resources><i:nonFraction id="f" name="g:Revenue" contextRef="c" unitRef="u" format="t:num-dot-decimal">1,200</i:nonFraction></html>'''
    transform_only = alternate.replace(b'<x:scenario><fake:instant xmlns:fake="urn:foreign">1900</fake:instant></x:scenario>', b'')
    parsed = parse_filing(transform_only)["facts"][0]
    assert parsed["value"] == "1200"
    assert not parsed["unresolved"]
    foreign = alternate.replace(b'<fake:instant xmlns:fake="urn:foreign">1900</fake:instant>', b'<fake:custom xmlns:fake="urn:foreign">1900</fake:custom>')
    foreign_fact = parse_filing(foreign)["facts"][0]
    assert foreign_fact["context"]["unsupported"] == ["foreign-or-unsupported-context-child"]
    assert foreign_fact["unresolved"] and foreign_fact["value"] is None

    fake_ix = document('<ix:nonFraction id="fake" name="us-gaap:A" contextRef="c1" unitRef="u1">9</ix:nonFraction>').replace(
        b'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"', b'xmlns:ix="urn:fake"')
    assert parse_filing(fake_ix)["facts"] == []
    fake_xbrli = document('<ix:nonFraction id="fake" name="us-gaap:A" contextRef="c1" unitRef="u1">9</ix:nonFraction>').replace(
        b'xmlns:xbrli="http://www.xbrl.org/2003/instance"', b'xmlns:xbrli="urn:fake"')
    assert parse_filing(fake_xbrli)["facts"][0]["unresolved"]


def test_resource_xml_validation_fails_closed_and_preserves_valid_resources():
    valid = document('<ix:nonFraction id="f" name="us-gaap:A" contextRef="c1" unitRef="u1">1</ix:nonFraction>')
    parsed = parse_filing(valid)
    assert parsed["facts"][0]["context"]["entity"] == "0001"
    assert parsed["facts"][0]["unit"]["measures"][0]["lexical"] == "iso4217:USD"

    foreign_namespace = valid.replace(
        b'xmlns:xbrli="http://www.xbrl.org/2003/instance"', b'xmlns:xbrli="urn:foreign"')
    foreign_result = parse_filing(foreign_namespace)
    assert foreign_result["facts"][0]["unresolved"]
    assert any(item["code"] == "missing-context" for item in foreign_result["diagnostics"])

    malicious = valid.replace(
        b'<xbrli:instant>2025-12-31</xbrli:instant>',
        b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><xbrli:instant>&e;</xbrli:instant>')
    # Document-level declaration screening rejects the malicious resource before token parsing.
    with pytest.raises(ValueError, match="doctype|entity"):
        parse_filing(malicious)


def test_xml_fragment_size_and_transform_namespace_validation():
    with pytest.raises(ValueError, match="size limit"):
        _parse_xml_fragment("x" * 1_000_001)
    fake_transform = document('<ix:nonFraction id="f" name="us-gaap:A" contextRef="c1" unitRef="u1" format="ixt:num-dot-decimal">1,200</ix:nonFraction>').replace(
        b'xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"', b'xmlns:ixt="urn:fake"')
    fact = parse_filing(fake_transform)["facts"][0]
    assert fact["unresolved"] and fact["value"] is None


def test_ixbrl_continuation_cycles_and_numeric_bounds():
    raw = document('<ix:nonNumeric id="f" name="dei:Text" contextRef="c1" continuedAt="c1">start</ix:nonNumeric><ix:continuation id="c1" continuedAt="c1">tail</ix:continuation>')
    result = parse_filing(raw, max_continuation_chars=20)
    assert any(d["code"] == "continuation-cycle" for d in result["diagnostics"])
    assert result["facts"][0]["raw_text"] == "" and result["facts"][0]["unresolved"]
    missing = document('<ix:nonNumeric id="m" name="dei:Text" contextRef="c1" continuedAt="absent">partial</ix:nonNumeric>')
    missing_result = parse_filing(missing)
    assert any(d["code"] == "missing-continuation" for d in missing_result["diagnostics"])
    assert missing_result["facts"][0]["raw_text"] == "" and missing_result["facts"][0]["unresolved"]
    nil_doc = document('<ix:nonFraction id="n" name="us-gaap:N" contextRef="c1" unitRef="u1" xsi:nil="true">9</ix:nonFraction>')
    nil_fact = parse_filing(nil_doc)["facts"][0]
    assert nil_fact["unresolved"] and nil_fact["value"] is None
    assert any(d["code"] == "invalid-nil-content" for d in parse_filing(nil_doc)["diagnostics"])
    metadata_doc = document('<ix:hidden><ix:nonFraction id="h" name="us-gaap:H" contextRef="c1" unitRef="u1" precision="INF" continuedAt="c2">3</ix:nonFraction></ix:hidden><ix:continuation id="c2">4</ix:continuation>')
    metadata_fact = parse_filing(metadata_doc)["facts"][0]
    assert metadata_fact["hidden"] and metadata_fact["continued_at"] == "c2" and metadata_fact["precision"] == "INF"
    with pytest.raises(ValueError, match="continuation length limit"):
        parse_filing(raw, max_continuation_chars=2)
    huge = document('<ix:nonFraction id="f" name="us-gaap:N" contextRef="c1" unitRef="u1" scale="999999999999">1</ix:nonFraction>')
    bounded = parse_filing(huge)
    assert bounded["facts"][0]["value"] is None
    assert bounded["facts"][0]["unresolved"]


@pytest.mark.parametrize("issuer", ["southwest", "delta"])
def test_private_originals_optional(issuer):
    from airline_valuation.filing_registry import FILINGS
    artifact = FILINGS[issuer]["artifacts"]["primary"]
    path = sec_fixture_files(artifact["filename"])[artifact["filename"]]
    raw = path.read_bytes()
    assert len(raw) == artifact["length"]
    assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
    result = parse_filing(raw, source_id=issuer)
    assert len(result["facts"]) >= 1500
    assert result["contexts"] and result["units"]
    assert any(fact["context"] for fact in result["facts"])
    assert any(fact["unit"] for fact in result["facts"])
    assert not any(d["code"] == "unresolved-concept-qname" for d in result["diagnostics"])
