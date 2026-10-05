"""Bounded offline Inline XBRL extraction for review, not valuation promotion.

html5lib (MIT) is used for HTML5 tokenization/tree repair; exact pins also include
its runtime dependencies six (MIT) and webencodings (BSD). html5lib 1.1 is an
older release; callers should retain these pins and review maintenance/security
status before upgrades. XML fragments are never resolved against external data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from typing import Any

from html5lib._tokenizer import HTMLTokenizer

MAX_BYTES = 12 * 1024 * 1024
MAX_TOKENS = 200_000
MAX_DEPTH = 128
MAX_FACTS = 20_000
MAX_CONTINUATION_CHARS = 1_000_000
MAX_XML_FRAGMENT_CHARS = 1_000_000
MAX_XML_RESOURCE_TOTAL_CHARS = 8 * 1024 * 1024
XML_RESOURCE_TOTAL_LIMIT_ERROR = "XML resource aggregate serialization size limit exceeded"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
FACT_TAGS = {"ix:nonfraction", "ix:nonnumeric", "ix:fraction", "ix:qualifiedforex"}
XBRLI = "http://www.xbrl.org/2003/instance"
IX = "http://www.xbrl.org/2013/inlineXBRL"
IXT_URIS = {"http://www.xbrl.org/inlineXBRL/transformation/2020-02-12", "http://www.xbrl.org/inlineXBRL/transformation/2015-02-26"}


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    ns: dict[str, str]
    children: list[Any] = field(default_factory=list)
    path: tuple[int, ...] = ()


def _local(tag: str) -> str:
    return tag.rsplit(":", 1)[-1].lower()


def _canonical_tag(tag: str, namespaces: dict[str, str]) -> str:
    prefix, sep, local = tag.partition(":")
    uri = namespaces.get(prefix, "") if sep else namespaces.get("", "")
    local = local.lower() if sep else tag.lower()
    known = {IX: "ix", XBRLI: "xbrli", "http://xbrl.org/2006/xbrldi": "xbrldi"}
    if uri in known:
        return f"{known[uri]}:{local}"
    if (sep and prefix in {"ix", "xbrli", "xbrldi"}) or (not sep and uri in {IX, XBRLI}):
        return f"foreign:{local}"
    return tag.lower()


def _attr(attrs: dict[str, str], key: str, default: str | None = None) -> str | None:
    return attrs.get(key.lower(), default)


def _lexical_qname(value: str | None, namespaces: dict[str, str]) -> tuple[str, str, str] | None:
    if not value or not re.fullmatch(r"[A-Za-z_][\w.-]*:[A-Za-z_][\w.-]*", value):
        return None
    prefix, local = value.split(":", 1)
    uri = namespaces.get(prefix)
    return (value, uri, local) if uri else None


def _walk(node: _Node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _ancestors(root: _Node, target: _Node):
    path = target.path
    result = []
    current = root
    result.append(current)
    for index in path:
        children = [child for child in current.children if isinstance(child, _Node)]
        if index < 0 or index >= len(children):
            return result
        current = children[index]
        result.append(current)
    return result


def _find_path(root: _Node, path: list[int]) -> _Node | None:
    current = root
    for index in path:
        children = [child for child in current.children if isinstance(child, _Node)]
        if index < 0 or index >= len(children): return None
        current = children[index]
    return current


def _plain_text(node: _Node) -> str:
    parts: list[str] = []
    def collect(n: _Node):
        for child in n.children:
            if isinstance(child, str): parts.append(child)
            elif child.tag not in {"ix:exclude", "ix:continuation"}: collect(child)
    collect(node)
    return "".join(parts)


def _node_digest(node: _Node) -> str:
    def encode(n: _Node):
        return [n.tag, sorted(n.attrs.items()), [("text", c) if isinstance(c, str) else ("node", encode(c)) for c in n.children]]
    return hashlib.sha256(json.dumps(encode(node), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _text(node: _Node, continued: dict[str, _Node], diagnostics: list[dict], limit: int) -> tuple[str, bool]:
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    exceeded = False
    def gather(n: _Node):
        nonlocal total, exceeded
        for child in n.children:
            if isinstance(child, str):
                total += len(child)
                if total > limit:
                    exceeded = True
                    return
                out.append(child)
            elif child.tag in {"ix:exclude", "ix:continuation"}:
                continue
            else:
                gather(child)
                if exceeded: return
    gather(node)
    link = _attr(node.attrs, "continuedat")
    while link and not exceeded:
        if link in seen:
            diagnostics.append({"code": "continuation-cycle", "id": link}); return "", True
        seen.add(link)
        nxt = continued.get(link)
        if nxt is None:
            diagnostics.append({"code": "missing-continuation", "id": link}); return "", True
        gather(nxt)
        link = _attr(nxt.attrs, "continuedat")
    if exceeded:
        diagnostics.append({"code": "continuation-limit", "limit": limit})
        return "", True
    return "".join(out), False


def _parse_xml_fragment(raw: str) -> list[dict]:
    """Parse bounded XML fragments without DTD/entity expansion or external access."""
    from xml.etree import ElementTree as ET
    if len(raw) > MAX_XML_FRAGMENT_CHARS:
        raise ValueError("XML fragment size limit exceeded")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
        raise ValueError("XML doctype/entity declarations are forbidden")
    wrapped = "<fragment>" + raw + "</fragment>"
    try:
        root = ET.fromstring(wrapped)
    except (ET.ParseError, ValueError) as exc:
        raise ValueError(f"unsafe or malformed XML fragment: {exc}") from None
    result = []
    for el in root.iter():
        if el is root: continue
        if list(el):
            result.append({"kind": "unsupported-nested-dimension", "tag": el.tag})
        else:
            result.append({"kind": "xml-element", "tag": el.tag, "text": el.text or ""})
    return result


def _xml_resource_fragment(node: _Node, aggregate_chars: list[int] | None = None) -> str:
    """Serialize a resource within per-fragment and optional parse-wide caps."""
    parts: list[str] = []
    total = 0

    def append(value: str) -> None:
        nonlocal total
        size = len(value)
        if total + size > MAX_XML_FRAGMENT_CHARS:
            raise ValueError("XML resource serialization size limit exceeded")
        if (aggregate_chars is not None
                and aggregate_chars[0] + size > MAX_XML_RESOURCE_TOTAL_CHARS):
            raise ValueError(XML_RESOURCE_TOTAL_LIMIT_ERROR)
        total += size
        if aggregate_chars is not None:
            aggregate_chars[0] += size
        parts.append(value)

    def escaped_text(value: str) -> None:
        for char in value:
            append({"&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(char, char))

    def escaped_attribute(value: str) -> None:
        quote = "'" if '"' in value and "'" not in value else '"'
        append(quote)
        replacements = {"&": "&amp;", "<": "&lt;", "\r": "&#13;", "\n": "&#10;", "\t": "&#9;",
                        quote: "&apos;" if quote == "'" else "&quot;"}
        for char in value:
            append(replacements.get(char, char))
        append(quote)

    def encode(current: _Node) -> None:
        append(f"<{current.tag}")
        attrs = [(key, value) for key, value in current.attrs.items() if not key.startswith("xmlns")]
        bindings = dict(current.ns)
        bindings.setdefault("xbrli", XBRLI)
        bindings.setdefault("xbrldi", "http://xbrl.org/2006/xbrldi")
        bindings.setdefault("foreign", "urn:airline-valuation:foreign-prefix")
        attrs.extend(("xmlns" if not prefix else f"xmlns:{prefix}", uri)
                     for prefix, uri in bindings.items())
        for key, value in attrs:
            append(f" {key}=")
            escaped_attribute(value)
        append(">")
        for child in current.children:
            if isinstance(child, str):
                escaped_text(child)
            else:
                encode(child)
        append(f"</{current.tag}>")

    encode(node)
    return "".join(parts)


def _validate_resource_xml(node: _Node, expected_local: str,
                           aggregate_chars: list[int] | None = None) -> bool:
    records = _parse_xml_fragment(_xml_resource_fragment(node, aggregate_chars))
    return bool(records) and records[0]["tag"] == f"{{{XBRLI}}}{expected_local}"


def _transform(raw: str, name: str | None, namespaces: dict[str, str]) -> tuple[str | None, str | None]:
    value = raw.strip()
    if not name:
        return value, None
    qname = _lexical_qname(name, namespaces)
    if not qname or qname[1] not in IXT_URIS:
        return None, "unsupported-transform"
    key = qname[2].lower()
    if key == "num-dot-decimal":
        value = value.replace(",", "")
    elif key == "num-comma-decimal":
        value = value.replace(".", "").replace(",", ".")
    elif key == "zerodash" and value in {"-", "—", "–"}:
        value = "0"
    elif key == "fixed-true" and value.lower() == "true":
        value = "true"
    elif key == "fixed-false" and value.lower() == "false":
        value = "false"
    else:
        return None, "unsupported-transform"
    return value, None


def parse_filing(data: bytes | str, *, source_id: str = "input", max_bytes: int = MAX_BYTES,
                 max_tokens: int = MAX_TOKENS, max_depth: int = MAX_DEPTH,
                 max_facts: int = MAX_FACTS,
                 max_continuation_chars: int = MAX_CONTINUATION_CHARS) -> dict:
    """Extract reviewable facts and source locators; no network or valuation behavior."""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(raw) > max_bytes: raise ValueError("document byte limit exceeded")
    digest = hashlib.sha256(raw).hexdigest()
    # Enforce resource limits while iterating tokenizer tokens, before _Node tree construction.
    decoded = raw.decode("utf-8-sig", "replace")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", decoded, re.I):
        raise ValueError("XML doctype/entity declarations are forbidden")
    stack: list[tuple[str, str, dict[str, str]]] = []
    token_count = facts_count = 0
    continuation_chars = 0
    continuation_depth = 0
    max_depth_observed = 0
    preflight_namespaces: list[dict[str, str]] = [{}]
    for tok in HTMLTokenizer(decoded):
        token_count += 1
        if token_count > max_tokens: raise ValueError("HTML token limit exceeded")
        if tok["type"] == 0:
            raise ValueError("XML/HTML doctype declarations are forbidden")
        if tok["type"] == 7:
            if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", decoded, re.I):
                raise ValueError("XML doctype/entity declarations are forbidden")
            if not (token_count == 1 and decoded.lstrip().startswith("<?xml")):
                raise ValueError("malformed declaration in primary input")
        if tok["type"] == 3:
            lexical_name = tok["name"].lower()
            ns = dict(preflight_namespaces[-1])
            attrs = tok.get("data", {})
            for key, value in attrs.items():
                if key == "xmlns": ns[""] = value
                elif key.startswith("xmlns:"): ns[key[6:]] = value
            name = _canonical_tag(lexical_name, ns)
            if name in FACT_TAGS:
                facts_count += 1
                if facts_count > max_facts: raise ValueError("fact limit exceeded")
            if name == "ix:continuation":
                continuation_chars += len(attrs.get("continuedat", ""))
                if len(attrs.get("continuedat", "")) > 256:
                    raise ValueError("continuation reference limit exceeded")
                if continuation_chars > max_continuation_chars:
                    raise ValueError("continuation length limit exceeded")
            if name == "ix:continuation" and not tok.get("selfClosing"):
                continuation_depth += 1
            if not tok.get("selfClosing") and name not in VOID:
                stack.append((lexical_name, name, ns))
                preflight_namespaces.append(ns)
                max_depth_observed = max(max_depth_observed, len(stack))
                if len(stack) > max_depth: raise ValueError("HTML nesting depth limit exceeded")
        elif tok["type"] == 1 and continuation_depth:
            continuation_chars += len(tok.get("data", ""))
            if continuation_chars > max_continuation_chars:
                raise ValueError("continuation length limit exceeded")
        elif tok["type"] == 4:
            lexical_name = tok["name"].lower()
            name = _canonical_tag(lexical_name, preflight_namespaces[-1])
            match = next((i for i in range(len(stack) - 1, -1, -1)
                          if stack[i][1] == name), None)
            if match is not None:
                removed = stack[match:]
                opened_lexical, opened_name, _ = stack[match]
                if (opened_name.startswith(("ix:", "xbrli:", "xbrldi:"))
                        and opened_lexical != lexical_name):
                    raise ValueError("mismatched Inline XBRL closing tag")
                implicitly_closed = [item[1] for item in removed[1:]
                                     if item[1].startswith(("ix:", "xbrli:", "xbrldi:"))]
                if implicitly_closed:
                    raise ValueError("unclosed Inline XBRL fact/resource markup")
                if any(item[1] == "ix:continuation" for item in removed):
                    continuation_depth = max(0, continuation_depth - sum(item[1] == "ix:continuation" for item in removed))
                del stack[match:]
                del preflight_namespaces[match + 1:]
    malformed = [name for _, name, _ in stack
                 if name.startswith(("ix:", "xbrli:", "xbrldi:"))]
    if malformed:
        raise ValueError("unclosed Inline XBRL fact/resource markup")
    # Build a modest event tree only after preflight succeeds; token count is already bounded.
    root = _Node("#document", {}, {})
    nodes = [root]
    namespaces = [{}]
    for tok in HTMLTokenizer(decoded):
        typ = tok["type"]
        if typ == 3:
            tag = tok["name"].lower(); attrs = dict(tok.get("data", {})); ns = dict(namespaces[-1])
            for k, v in attrs.items():
                if k == "xmlns": ns[""] = v
                elif k.startswith("xmlns:"): ns[k[6:]] = v
            parent = nodes[-1]
            sibling_index = sum(isinstance(child, _Node) for child in parent.children)
            tag = _canonical_tag(tag, ns)
            n = _Node(tag, attrs, ns, path=parent.path + (sibling_index,)); parent.children.append(n)
            if not tok.get("selfClosing") and tag not in VOID:
                nodes.append(n); namespaces.append(ns)
        elif typ == 4:
            name = _canonical_tag(tok["name"].lower(), namespaces[-1])
            match = next((i for i in range(len(nodes)-1, 0, -1) if nodes[i].tag == name), None)
            if match is not None:
                del nodes[match:]; del namespaces[match:]
        elif typ == 1:
            nodes[-1].children.append(tok.get("data", ""))
        elif typ == 2:
            nodes[-1].children.append(tok.get("data", ""))
    all_nodes = list(_walk(root)); ids: dict[str, list[_Node]] = {}
    for n in all_nodes:
        ident = _attr(n.attrs, "id")
        if ident: ids.setdefault(ident, []).append(n)
    diagnostics: list[dict] = []
    for ident, matches in ids.items():
        if len(matches) > 1: diagnostics.append({"code": "duplicate-id", "id": ident, "count": len(matches)})
    continuation_groups: dict[str, list[_Node]] = {}
    for n in all_nodes:
        if n.tag == "ix:continuation":
            ident = _attr(n.attrs, "id")
            if ident: continuation_groups.setdefault(ident, []).append(n)
    continued = {ident: group[0] for ident, group in continuation_groups.items() if len(group) == 1}
    for ident, group in continuation_groups.items():
        if len(group) > 1: diagnostics.append({"code": "duplicate-continuation-id", "id": ident, "count": len(group)})
    contexts: dict[str, dict] = {}
    units: dict[str, dict] = {}
    context_counts: dict[str, int] = {}
    unit_counts: dict[str, int] = {}
    context_nodes = [n for n in all_nodes if n.tag == "xbrli:context" and _attr(n.attrs, "id")]
    unit_nodes = [n for n in all_nodes if n.tag == "xbrli:unit" and _attr(n.attrs, "id")]
    validated_contexts = []
    validated_units = []
    serialized_resource_chars = [0]
    for node in context_nodes:
        ident = _attr(node.attrs, "id")
        try:
            valid = _validate_resource_xml(node, "context", serialized_resource_chars)
        except ValueError as exc:
            if str(exc) == XML_RESOURCE_TOTAL_LIMIT_ERROR:
                raise
            diagnostics.append({"code": "unresolved-context-xml-fragment", "id": ident})
            continue
        if not valid:
            diagnostics.append({"code": "unresolved-context-namespace", "id": ident})
            continue
        validated_contexts.append(node)
    for node in unit_nodes:
        ident = _attr(node.attrs, "id")
        try:
            valid = _validate_resource_xml(node, "unit", serialized_resource_chars)
        except ValueError as exc:
            if str(exc) == XML_RESOURCE_TOTAL_LIMIT_ERROR:
                raise
            diagnostics.append({"code": "unresolved-unit-xml-fragment", "id": ident})
            continue
        if not valid:
            diagnostics.append({"code": "unresolved-unit-namespace", "id": ident})
            continue
        validated_units.append(node)
    context_nodes = validated_contexts
    unit_nodes = validated_units
    for node in context_nodes: context_counts[_attr(node.attrs, "id")] = context_counts.get(_attr(node.attrs, "id"), 0) + 1
    for node in unit_nodes: unit_counts[_attr(node.attrs, "id")] = unit_counts.get(_attr(node.attrs, "id"), 0) + 1
    for n in context_nodes:
        ident = _attr(n.attrs, "id")
        if context_counts[ident] != 1:
            continue
        desc = {"id": ident, "entity": None, "entity_scheme": None, "period": None, "dimensions": [], "unsupported": []}
        for c in _walk(n):
            loc = _local(c.tag)
            if c is n:
                continue
            if c.tag not in {"xbrli:entity", "xbrli:period", "xbrli:segment", "xbrli:scenario", "xbrli:identifier", "xbrli:instant", "xbrli:startdate", "xbrli:enddate", "xbrli:forever", "xbrldi:explicitmember", "xbrldi:typedmember"}:
                if len(c.path) > len(n.path) and c.path[:len(n.path)] == n.path:
                    desc["unsupported"].append("foreign-or-unsupported-context-child")
                continue
            if loc == "identifier":
                desc["entity"] = _plain_text(c).strip()
                desc["entity_scheme"] = _attr(c.attrs, "scheme")
            elif loc in ("instant", "startdate", "enddate", "forever"):
                val = _plain_text(c).strip()
                if loc == "instant": desc["period"] = {"type": "instant", "date": val}
                elif loc == "forever": desc["period"] = {"type": "forever"}
                else: desc.setdefault("period_parts", {})[loc] = val
            elif loc == "explicitmember":
                q = _lexical_qname(_attr(c.attrs, "dimension"), c.ns)
                m = _lexical_qname(_plain_text(c).strip(), c.ns)
                if q and m: desc["dimensions"].append({"dimension": q[0], "dimension_namespace": q[1], "member": m[0], "member_namespace": m[1]})
                else: desc["unsupported"].append("unresolved-dimension-qname")
            elif loc == "typedmember": desc["unsupported"].append("typed-dimension")
        if "period_parts" in desc:
            parts = desc.pop("period_parts")
            if desc["period"] is not None:
                desc["period"] = {"type": "ambiguous-period"}
                desc["unsupported"].append("ambiguous-period")
                contexts[ident] = desc
                continue
            try:
                if set(parts) != {"startdate", "enddate"}: raise ValueError
                date.fromisoformat(parts["startdate"]); date.fromisoformat(parts["enddate"])
                if parts["startdate"] > parts["enddate"]: raise ValueError
                desc["period"] = {"type": "duration", **parts}
            except ValueError:
                desc["period"] = {"type": "invalid-duration", **parts}
                desc["unsupported"].append("invalid-duration")
        contexts[ident] = desc
    for ident, count in context_counts.items():
        if count > 1: diagnostics.append({"code": "duplicate-context-id", "id": ident, "count": count})
    for n in unit_nodes:
        ident = _attr(n.attrs, "id")
        if unit_counts[ident] != 1: continue
        measures = []
        unsupported = []
        for c in _walk(n):
            if c is n:
                continue
            if c.tag == "xbrli:measure":
                lexical = _plain_text(c).strip(); q = _lexical_qname(lexical, c.ns)
                measures.append({"lexical": lexical, "namespace": q[1] if q else None,
                                 "local_name": q[2] if q else None})
            elif c.tag in {"xbrli:unitnumerator", "xbrli:unitdenominator", "xbrli:divide"}:
                unsupported.append("divide-unit-structure")
            elif c.tag != "xbrli:unit":
                unsupported.append("foreign-or-unsupported-unit-structure")
        if len(measures) != 1:
            unsupported.append("unit-measure-count")
        if any(measure["namespace"] is None for measure in measures):
            unsupported.append("unresolved-unit-measure-qname")
        if unsupported:
            diagnostics.extend({"code": code, "unit_id": ident} for code in sorted(set(unsupported)))
        units[ident] = {"id": ident, "measures": measures, "unsupported": sorted(set(unsupported)),
                        "resolved": not unsupported}
    for ident, count in unit_counts.items():
        if count > 1: diagnostics.append({"code": "duplicate-unit-id", "id": ident, "count": count})
    facts = []
    heading_node = None
    for node_index, n in enumerate(all_nodes):
        if n.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading_node = n
        if n.tag not in FACT_TAGS: continue
        a=n.attrs; ident=_attr(a,"id")
        q=_lexical_qname(_attr(a,"name"),n.ns)
        context_ref=_attr(a,"contextref"); unit_ref=_attr(a,"unitref")
        numeric = n.tag in ("ix:nonfraction", "ix:fraction", "ix:qualifiedforex")
        unsupported_numeric_kind = n.tag in ("ix:fraction", "ix:qualifiedforex")
        if unsupported_numeric_kind: diagnostics.append({"code":"unsupported-numeric-kind","fact_id":ident,"tag":n.tag})
        if context_ref not in contexts: diagnostics.append({"code":"missing-context","fact_id":ident,"ref":context_ref})
        if numeric and not unit_ref: diagnostics.append({"code":"missing-numeric-unit","fact_id":ident})
        elif unit_ref and unit_ref not in units: diagnostics.append({"code":"missing-unit","fact_id":ident,"ref":unit_ref})
        raw_text, continuation_broken = _text(n,continued,diagnostics,max_continuation_chars)
        nil_attr = _attr(a,"xsi:nil")
        nil = nil_attr is not None and nil_attr.lower() in {"true", "1"}
        nil_invalid = nil and bool(raw_text.strip())
        if nil_invalid: diagnostics.append({"code":"invalid-nil-content","fact_id":ident})
        if nil_attr is not None and nil_attr.lower() not in {"true", "false", "1", "0"}:
            nil_invalid = True; diagnostics.append({"code":"invalid-nil-value","fact_id":ident})
        transformed, transform_error = _transform(raw_text,_attr(a,"format"),n.ns) if numeric else (raw_text.strip(),None)
        scale_raw=_attr(a,"scale","0"); sign=_attr(a,"sign")
        value=None; numeric_error=transform_error
        if n.tag == "ix:nonfraction" and transformed is not None:
            try:
                scale=int(scale_raw)
                if abs(scale)>1000: raise ValueError("scale outside supported numeric bounds")
                if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", transformed.strip()): raise InvalidOperation
                with localcontext() as ctx:
                    ctx.prec=max(50,len(transformed)+abs(scale)+10)
                    value=Decimal(transformed.strip())*(Decimal(10)**scale)
                    if sign == "-": value=-value
                    elif sign not in (None,"+" ): raise InvalidOperation
            except (InvalidOperation, ValueError, OverflowError, ArithmeticError): numeric_error="invalid-numeric-or-scale"
        if numeric_error: diagnostics.append({"code":numeric_error,"fact_id":ident,"format":_attr(a,"format")})
        if q is None: diagnostics.append({"code":"unresolved-concept-qname","fact_id":ident,"name":_attr(a,"name")})
        duplicate_fact_id = bool(ident and len(ids.get(ident, [])) > 1)
        if duplicate_fact_id: diagnostics.append({"code":"duplicate-fact-id","fact_id":ident})
        path_list = list(n.path)
        heading_text = _plain_text(heading_node).strip() if heading_node else ""
        heading_digest = hashlib.sha256(heading_text.encode("utf-8")).hexdigest()
        content_digest = hashlib.sha256((_node_digest(n) + ":" + str(len(raw_text.encode("utf-8"))) + ":" + raw_text).encode("utf-8")).hexdigest()
        context_bad = context_ref not in contexts or bool(contexts.get(context_ref, {}).get("unsupported")) or (context_ref is not None and context_counts.get(context_ref, 0) > 1) or (context_ref is not None and len(ids.get(context_ref, [])) > 1)
        unit_bad = numeric and (not unit_ref or unit_ref not in units or not units.get(unit_ref, {}).get("resolved") or (unit_ref is not None and unit_counts.get(unit_ref, 0) > 1) or (unit_ref is not None and len(ids.get(unit_ref, [])) > 1))
        duplicate_reference = bool((context_ref and len(ids.get(context_ref, [])) > 1) or (unit_ref and len(ids.get(unit_ref, [])) > 1))
        unresolved = (numeric_error is not None or unsupported_numeric_kind or q is None or duplicate_fact_id or duplicate_reference or context_bad or unit_bad or nil_invalid or continuation_broken)
        if nil and not nil_invalid:
            value = None
        if unresolved: value = None
        fact={"id":ident,"concept":_attr(a,"name"),"concept_namespace":q[1] if q else None,
              "context_ref":context_ref,"context":contexts.get(context_ref),"unit_ref":unit_ref,"unit":units.get(unit_ref),
              "raw_text":raw_text,"value":str(value) if value is not None else None,"scale":scale_raw,
              "sign":sign,"nil":nil,"format":_attr(a,"format"),"decimals":_attr(a,"decimals"),"precision":_attr(a,"precision"),
              "hidden":any(ancestor.tag == "ix:hidden" for ancestor in _ancestors(root, n)),"continued_at":_attr(a,"continuedat"),
              "locator":{"format":"html_dom","source_id":source_id,"sha256":digest,"path":path_list,"tag":n.tag,"content_sha256":content_digest,"heading_sha256":heading_digest,"heading_text":heading_text},
              "heading":heading_text,"unresolved":unresolved,"_value_identity":str(value) if value is not None else None}
        facts.append(fact)
    conflicts: dict[tuple, list[dict]] = {}
    for fact in facts:
        if fact["concept"] and fact["context_ref"] and fact["unit_ref"]:
            key=(fact["concept_namespace"],fact["concept"],fact["context_ref"],fact["unit_ref"])
            conflicts.setdefault(key, []).append(fact)
    for key, group in conflicts.items():
        signatures={(f["_value_identity"], f["decimals"], f["precision"]) for f in group}
        if len(group)>1 and len(signatures)>1:
            diagnostics.append({"code":"conflicting-duplicate-facts","concept":key[1],"context_ref":key[2],"unit_ref":key[3],"fact_ids":[f["id"] for f in group]})
            for fact in group: fact["unresolved"]=True; fact["value"]=None
    for f in facts:
        f.pop("_value_identity",None)
        if not f["id"]: f["unresolved"]=True; f["value"]=None; diagnostics.append({"code":"fact-missing-id","concept":f["concept"]})
    return {"schema":"airline-inline-xbrl/1","source_id":source_id,"sha256":digest,
            "token_count":token_count,"max_depth_observed":max_depth_observed,"facts":facts,"contexts":contexts,"units":units,
            "diagnostics":diagnostics,"limitations":["Only explicit dimensions are resolved; typed dimensions remain unsupported.","Only listed basic numeric transformations are supported; unsupported transforms remain unresolved.","Locator binds exact input digest and fact id; re-resolution requires the original bytes.","No valuation or workbook promotion is performed."]}


def resolve_locator(data: bytes, locator: dict) -> bool:
    """Dispatch a format-tagged locator against the pinned original bytes."""
    if not isinstance(locator, dict) or locator.get("format") != "html_dom": return False
    if hashlib.sha256(data).hexdigest() != locator.get("sha256"): return False
    try:
        result=parse_filing(data,source_id=locator.get("source_id","input"))
    except (ValueError, TypeError):
        return False
    path=locator.get("path")
    if not isinstance(path,list) or not path or any(not isinstance(i,int) for i in path): return False
    candidates=[f for f in result["facts"] if f["locator"].get("path")==path]
    if len(candidates)!=1: return False
    regenerated=candidates[0]["locator"]
    required=("source_id","sha256","path","tag","content_sha256","heading_sha256","heading_text")
    return all(regenerated.get(key)==locator.get(key) for key in required)
