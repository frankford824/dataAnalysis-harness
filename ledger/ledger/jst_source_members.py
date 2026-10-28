"""Pure bounded proofs from original XLSX/XML and captured ERP response bytes.

No paths, network, prices or financial writes are accepted here. The trusted
caller retains source bytes and applies normal authorization/CAS checks later.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from html.parser import HTMLParser
import hashlib
import io
import json
import posixpath
import re
from typing import Mapping
import xml.etree.ElementTree as ET
import zipfile

PARSER_SHA256 = "0c636672b044492c249572b7f3940eaa4cdf9c3f0d05b8761ac7ddd3929477b8"
MAX_RESPONSE = 16 * 1024 * 1024
MAX_BLOBS = 256 * 1024 * 1024
MAX_XML = 128 * 1024 * 1024
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

class MemberPending(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason

def _need(ok, reason):
    if not ok:
        raise MemberPending(reason)

@dataclass(frozen=True)
class Anchor:
    sha256: str
    sheet: str
    row: int

@dataclass(frozen=True)
class Node:
    order_id: str
    sha256: str

@dataclass(frozen=True)
class Capture:
    nodes: tuple[Node, ...]
    relations: tuple[Node, ...] = ()
    parser_sha256: str = PARSER_SHA256

@dataclass(frozen=True)
class Request:
    source_order_id: str
    platform_order_id: str
    platform: str
    store_names: tuple[str, ...]
    shop_ids: tuple[str, ...]
    cost_anchors: tuple[Anchor, ...]
    native_anchors: tuple[Anchor, ...]

def _hash(raw):
    return hashlib.sha256(raw).hexdigest()

def _id(value):
    _need(not isinstance(value, (bool, float)), "unsafe_identity")
    result = str(value or "").strip()
    _need(bool(re.fullmatch(r"[1-9][0-9]{0,23}", result)), "invalid_identity")
    return result

def _number(value):
    _need(not isinstance(value, (bool, float)) and value is not None, "quantity_literal_missing")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise MemberPending("quantity_invalid") from exc
    # Representation bounds keep <=2000 component sums exact in 256 digits.
    _need(result.is_finite() and result > 0 and len(result.as_tuple().digits) <= 100
          and -64 <= result.as_tuple().exponent <= 64 and -64 <= result.adjusted() <= 64, "quantity_invalid")
    return result

def _day(value):
    try:
        text = str(value)
        _need(bool(re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[ T])", text)), "original_day_invalid")
        return date.fromisoformat(text[:10]).isoformat()
    except (ValueError, TypeError) as exc:
        raise MemberPending("original_day_invalid") from exc

def _created_day(value):
    try:
        _need(isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", value)), "physical_creation_timestamp_invalid")
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError) as exc:
        raise MemberPending("physical_creation_timestamp_invalid") from exc

def _observed(row, fields):
    return {field: {"present": field in row,
                    "value": str(row[field]) if isinstance(row.get(field), Decimal) else row.get(field)}
            for field in fields}

def _json(raw):
    def pairs(values):
        result = {}
        for k, v in values:
            _need(k not in result, "duplicate_json_key")
            result[k] = v
        return result
    def invalid_constant(_):
        raise MemberPending("nonfinite_json")
    try:
        result = json.loads(raw, parse_float=Decimal, parse_int=Decimal,
                            object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, MemberPending):
            raise
        raise MemberPending("source_json_invalid") from exc
    stack, count = [(result, 0)], 0
    while stack:
        v, depth = stack.pop()
        count += 1
        _need(depth <= 64 and count <= 200000, "source_json_budget")
        if isinstance(v, dict):
            stack.extend((child, depth + 1) for child in v.values())
        elif isinstance(v, list):
            stack.extend((child, depth + 1) for child in v)
    return result

def _quoted_end(text, start):
    quote, i = text[start], start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == quote:
            return i + 1
        else:
            i += 1
    raise MemberPending("source_script_truncated")

def _object_end(text, start):
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c == '"':
            i = _quoted_end(text, i)
            continue
        if c in "{[":
            depth += 1
            _need(depth <= 64, "source_json_budget")
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise MemberPending("source_script_truncated")

def _html(raw):
    _need(isinstance(raw, bytes) and len(raw) <= MAX_RESPONSE, "response_budget")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemberPending("source_utf8_invalid") from exc
    _need(not re.search(r"loginReact|erp-login|无访问权限|未登录", text[:10000]), "source_login_pending")
    return text

class _Scripts(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text, self.scripts, self.active, self.inert = text, [], None, 0
        self.offsets = [0, *(m.end() for m in re.finditer("\n", text))]
    def position(self):
        row, col = self.getpos()
        return self.offsets[row - 1] + col
    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"template", "noscript", "textarea", "title", "xmp", "iframe"}:
            self.inert += 1
        if tag.lower() == "script":
            _need(self.active is None, "source_script_ambiguous")
            self.active = (self.position() + len(self.get_starttag_text()), attrs, self.inert)
    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.active is not None:
            start, attrs, inert = self.active
            body = self.text[start:self.position()]
            if re.search(r"\b_OrderData\s*=", body):
                names = [k.lower() for k, _ in attrs]
                _need(len(set(names)) == len(names), "source_script_ambiguous")
                options = {k.lower(): (v or "").strip().lower() for k, v in attrs}
                _need(not inert and "src" not in options and options.get("type", "") in {"", "text/javascript", "application/javascript"}, "source_script_unsupported")
                self.scripts.append((start, body))
            self.active = None
        if tag.lower() in {"template", "noscript", "textarea", "title", "xmp", "iframe"}:
            self.inert = max(0, self.inert - 1)

def parse_order(raw: bytes, expected_id: str):
    """Reparse a unique top-level JSON assignment, with no JS evaluation."""
    text = _html(raw)
    parser = _Scripts(text)
    parser.feed(text)
    parser.close()
    _need(parser.active is None, "source_script_truncated")
    spans = []
    for offset, body in parser.scripts:
        depth, previous, i, prefix = 0, ";", 0, []
        while i < len(body):
            c = body[i]
            if c in "\"'`":
                i = _quoted_end(body, i)
                previous = "quoted"
                prefix.append(" quoted ")
                continue
            if body.startswith("//", i):
                end = body.find("\n", i + 2)
                i = len(body) if end < 0 else end
                prefix.append(" ")
                continue
            if body.startswith("/*", i):
                end = body.find("*/", i + 2)
                _need(end >= 0, "source_script_truncated")
                i = end + 2
                prefix.append(" ")
                continue
            _need(c != "/", "source_script_unsupported")
            m = re.match(r"_OrderData\b\s*=\s*(\{)", body[i:])
            if m and (i == 0 or not re.match(r"[\w$.]", body[i - 1])):
                declaration = re.search(r"(?:^|[;}])\s*(?:var|let|const)\s*$", "".join(prefix))
                _need(depth == 0 and (declaration is not None or previous in {";", "}"}), "source_script_unsupported")
                start = i + m.end() - 1
                end = _object_end(body, start)
                spans.append((len(text[:offset + start].encode()), len(text[:offset + end].encode())))
                i, previous = end, "}"
                prefix.append("}")
                continue
            depth += (c == "{") - (c == "}")
            if not c.isspace():
                previous = c
            prefix.append(c)
            i += 1
    _need(len(spans) == 1, "source_assignment_not_unique")
    start, end = spans[0]
    data = _json(raw[start:end])
    _need(isinstance(data, dict) and _id(data.get("o_id")) == _id(expected_id), "detail_identity_conflict")
    _need(isinstance(data.get("items"), list) and len(data["items"]) <= 2000, "source_items_budget_or_schema")
    for item in data["items"]:
        _need(isinstance(item, dict) and _id(item.get("o_id")) == _id(expected_id), "member_parent_conflict")
    return data, {"sha256": _hash(raw), "start_byte": start, "end_byte": end, "json_sha256": _hash(raw[start:end])}

class _Relations(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.ids = []
    def handle_starttag(self, tag, attrs):
        callbacks = [v for k, v in attrs if k.lower() == "onclick" and v and "ShowOrder" in v]
        if callbacks:
            _need(tag.lower() == "span" and len(callbacks) == 1, "relation_schema_or_budget")
            m = re.fullmatch(r"\s*ShowOrder\(([1-9][0-9]*)\)\s*;?\s*", callbacks[0])
            _need(m is not None, "relation_schema_or_budget")
            self.ids.append(_id(m.group(1)))

def _related(raw):
    parser = _Relations()
    parser.feed(_html(raw))
    parser.close()
    _need(0 < len(parser.ids) <= 30, "relation_schema_or_budget")
    return set(parser.ids)

class _Workbook:
    def __init__(self, raw):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(raw))
            infos = self.zip.infolist()
            _need(len(infos) <= 2000 and len({i.filename for i in infos}) == len(infos), "workbook_zip_ambiguous")
            _need(sum(i.file_size for i in infos) <= 512 * 1024 * 1024 and all(i.file_size <= MAX_XML for i in infos), "workbook_budget")
            self.strings = []
            if "xl/sharedStrings.xml" in self.zip.namelist():
                self.strings = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in self.xml("xl/sharedStrings.xml").findall(NS + "si")]
            relations = {}
            for v in self.xml("xl/_rels/workbook.xml.rels"):
                _need(v.get("Id") not in relations, "workbook_relation_ambiguous")
                relations[v.get("Id")] = v.get("Target")
            book = self.xml("xl/workbook.xml")
            self.date1904 = any(p.get("date1904") in {"1", "true"} for p in book.findall(NS + "workbookPr"))
            self.sheets, self.cache = {}, {}
            for s in book.findall(NS + "sheets/" + NS + "sheet"):
                target = relations[s.get(REL)]
                path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
                _need(path.startswith("xl/worksheets/") and ".." not in path.split("/"), "workbook_sheet_path")
                _need(s.get("name") not in self.sheets, "workbook_sheet_ambiguous")
                self.sheets[s.get("name")] = path
        except (zipfile.BadZipFile, KeyError, ET.ParseError, RuntimeError, NotImplementedError) as exc:
            raise MemberPending("workbook_invalid") from exc
    def xml(self, name):
        raw = self.zip.read(name)
        _need(b"\0" not in raw, "workbook_xml_encoding")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MemberPending("workbook_xml_encoding") from exc
        declaration = re.match(r"\s*<\?xml\b[^?]*encoding\s*=\s*['\"]([^'\"]+)", text, re.I)
        _need(declaration is None or declaration.group(1).lower() in {"utf-8", "utf8"}, "workbook_xml_encoding")
        _need("<!DOCTYPE" not in text and "<!ENTITY" not in text, "workbook_xml_entity")
        return ET.fromstring(text)
    @staticmethod
    def column_number(column):
        n = 0
        for c in column:
            n = n * 26 + ord(c) - ord("A") + 1
        return n
    def _sheet(self, name):
        if name not in self.cache:
            _need(name in self.sheets, "source_sheet_missing")
            root, rows = self.xml(self.sheets[name]), {}
            for row in root.findall(NS + "sheetData/" + NS + "row"):
                number = int(row.get("r"))
                _need(0 < number <= 1048576 and number not in rows, "source_row_ambiguous")
                cells = {}
                for c in row.findall(NS + "c"):
                    address = c.get("r", "")
                    m = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", address)
                    _need(m is not None and int(m.group(2)) == number, "source_cell_row_conflict")
                    col = m.group(1)
                    _need(col not in cells, "source_cell_ambiguous")
                    v = c.find(NS + "v")
                    value, kind = v.text if v is not None else None, c.get("t", "n")
                    if kind == "s":
                        _need(value is not None and bool(re.fullmatch(r"[0-9]+", value)), "source_shared_string_index")
                        index = int(value)
                        _need(0 <= index < len(self.strings), "source_shared_string_index")
                        value = self.strings[index]
                    elif kind == "inlineStr":
                        value = "".join(t.text or "" for t in c.iter(NS + "t"))
                    cells[col] = (value, kind, c.find(NS + "f") is not None, address)
                rows[number] = cells
            merges = []
            for v in root.findall(NS + "mergeCells/" + NS + "mergeCell"):
                m = re.fullmatch(r"([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)", v.get("ref", ""))
                _need(m is not None, "source_merge_invalid")
                a, b, c, d = m.groups()
                ac, cc = self.column_number(a), self.column_number(c)
                _need(ac <= cc and int(b) <= int(d), "source_merge_invalid")
                merges.append((a, int(b), ac, int(d), cc))
            self.cache[name] = rows, merges
        return self.cache[name]
    def cell(self, sheet, row, column):
        rows, merges = self._sheet(sheet)
        result = rows.get(row, {}).get(column)
        _need(result is None or not result[2], "source_cell_missing_or_formula")
        col = self.column_number(column)
        matches = [(a, b) for a, b, c, d, e in merges if b <= row <= d and c <= col <= e]
        _need(len(matches) <= 1, "source_merge_ambiguous")
        if matches and (result is None or result[0] is None):
            a, b = matches[0]
            result = rows.get(b, {}).get(a)
        _need(result is not None and not result[2], "source_cell_missing_or_formula")
        return result
    def value(self, sheet, row, column):
        value, kind, _, _ = self.cell(sheet, row, column)
        if kind == "n" and value is not None:
            try:
                n = Decimal(value)
                # Convert only bounded identity-sized integers. Do not expand
                # a malicious exponent before the quantity validator sees it.
                if n.is_finite() and n.adjusted() <= 24 and n.as_tuple().exponent >= -64 and n == n.to_integral():
                    return str(int(n))
            except InvalidOperation:
                pass
        return str(value or "").strip()
    def headers(self, sheet, required=(), allow_first=()):
        rows, _ = self._sheet(sheet)
        h, duplicates = {}, set()
        for col in sorted(rows.get(2, {}), key=self.column_number):
            cell = self.cell(sheet, 2, col)
            if cell[0] is not None:
                name = str(cell[0]).strip()
                if name in h:
                    duplicates.add(name)
                else:
                    h[name] = col
        _need(not ((duplicates & set(required)) - set(allow_first)), "source_header_role_ambiguous")
        return h
    def day(self, sheet, row, column):
        value, kind, _, _ = self.cell(sheet, row, column)
        if kind == "n":
            try:
                n = Decimal(value)
                _need(n.is_finite() and 0 < n < 100000, "original_day_invalid")
                days = int(n)
                base = date(1904, 1, 1) if self.date1904 else date(1899, 12, 30)
                if not self.date1904 and days < 60:
                    days += 1
                return (base + timedelta(days=days)).isoformat()
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise MemberPending("original_day_invalid") from exc
        return _day(value)

def _platform(value, shops):
    text = str(value or "").strip()
    if ":" in text:
        prefix, text = text.split(":", 1)
        _need(prefix in shops, "platform_shop_prefix_conflict")
    _need(bool(text) and "," not in text and ";" not in text, "platform_member_not_atomic")
    return text

def _company(order):
    owner = order.get("owner_co_id")
    return _id(owner if owner is not None and str(owner).strip() not in {"", "0"} else order.get("co_id"))

def normalize_members(request: Request, capture: Capture, blobs: Mapping[str, bytes]) -> dict:
    """A source proof is separate from dated price and financial authorization."""
    result = {"schema": "jst_source_members.v1", "status": "pending", "members": [], "rows": [], "reasons": []}
    try:
        _need(isinstance(request, Request) and isinstance(capture, Capture), "typed_source_request_required")
        _need(0 < len(request.cost_anchors) <= 100 and 0 < len(request.native_anchors) <= 100, "source_anchor_budget")
        _need(0 < len(capture.nodes) <= 12 and len(capture.relations) <= 6, "source_graph_budget")
        _need(capture.parser_sha256 == PARSER_SHA256, "source_parser_identity_changed")
        _need(len(blobs) <= 240 and all(isinstance(v, bytes) for v in blobs.values())
              and sum(len(v) for v in blobs.values()) <= MAX_BLOBS, "source_blob_budget")
        for digest, raw in blobs.items():
            _need(_hash(raw) == digest, "source_blob_sha_changed")
        shops = {_id(v) for v in request.shop_ids}
        _need(shops and request.store_names, "source_shop_scope_missing")
        source_id, books = _id(request.source_order_id), {}
        def book(a):
            _need(isinstance(a, Anchor) and isinstance(a.row, int) and not isinstance(a.row, bool)
                  and 2 < a.row <= 1048576 and a.sha256 in blobs, "source_anchor_invalid")
            if a.sha256 not in books:
                books[a.sha256] = _Workbook(blobs[a.sha256])
            return books[a.sha256]
        native_days, native_children = set(), set()
        times = {"alibaba1688": "订单创建时间", "taobao": "订单创建时间", "douyin": "订单提交时间"}
        _need(request.platform in times, "native_platform_contract_pending")
        for a in request.native_anchors:
            b = book(a)
            parent = "订单号" if request.platform == "alibaba1688" else "主订单编号"
            roles = {parent, times[request.platform]}
            if request.platform != "alibaba1688":
                roles.add("子订单编号")
            # The approved Ali native template explicitly names occurrence=0:
            # its second 订单号 is a manually filled merged-cell copy.
            h = b.headers(a.sheet, roles, ("订单号",) if request.platform == "alibaba1688" else ())
            _need(parent in h and times[request.platform] in h, "native_header_contract_missing")
            _need(b.value(a.sheet, a.row, h[parent]) == request.platform_order_id, "native_parent_conflict")
            native_days.add(b.day(a.sheet, a.row, h[times[request.platform]]))
            if request.platform != "alibaba1688":
                _need("子订单编号" in h, "native_child_contract_missing")
                child = b.value(a.sheet, a.row, h["子订单编号"])
                _need(bool(child), "native_child_identity_missing")
                native_children.add(child)
        _need(len(native_days) == 1, "native_original_days_conflict")
        original_day, logical = next(iter(native_days)), {}
        fields = {"oid": "内部订单号", "iid": "子订单编号", "parent": "原始线上订单号", "child": "线上子订单编号",
                  "sku": "商品编码", "qty": "数量", "day": "下单时间", "shop": "店铺名称", "type": "订单类型"}
        for a in request.cost_anchors:
            b = book(a)
            h = b.headers(a.sheet, fields.values())
            _need(all(v in h for v in fields.values()), "cost_header_contract_missing")
            row = {k: b.value(a.sheet, a.row, h[v]) for k, v in fields.items()}
            _need(row["type"] == "普通订单", "reship_original_source_separate")
            _need(row["oid"] == source_id and row["parent"] == request.platform_order_id and row["shop"] in request.store_names, "original_cost_identity_conflict")
            _need(request.platform == "alibaba1688" or row["child"] in native_children, "native_child_conflict")
            _need(b.day(a.sheet, a.row, h["下单时间"]) == original_day, "original_cost_native_day_conflict")
            c = b.cell(a.sheet, a.row, h["数量"])
            _need(c[1] == "n", "quantity_literal_missing")
            qty, key = _number(c[0]), (_id(row["iid"]), row["sku"])
            if key in logical:
                _need(logical[key]["quantity"] == qty and logical[key]["child"] == row["child"], "duplicate_original_sources_conflict")
            else:
                logical[key] = {"quantity": qty, "child": row["child"], "anchors": []}
            logical[key]["anchors"].append({"sha256": a.sha256, "sheet": a.sheet, "row": a.row, "quantity_cell": c[3]})
        nodes, refs = {}, {}
        for ref in capture.nodes:
            _need(isinstance(ref, Node) and ref.sha256 in blobs and ref.order_id not in nodes, "source_node_ambiguous")
            data, proof = parse_order(blobs[ref.sha256], ref.order_id)
            _need(_id(data.get("shop_id")) in shops, "source_cross_shop")
            _need(data.get("type") == "普通订单", "non_normal_order_source_separate")
            _company(data)
            for item in data["items"]:
                _need(_id(item.get("co_id")) == _id(data.get("co_id")), "member_company_namespace_conflict")
            nodes[ref.order_id], refs[ref.order_id] = data, proof
        _need(source_id in nodes, "original_source_node_missing")
        source = nodes[source_id]
        _need(_day(source.get("order_date")) == original_day, "source_original_day_conflict")
        _need(request.platform_order_id in {_platform(v, shops) for v in str(source.get("so_id") or "").split(",")}, "source_original_parent_conflict")
        related = {}
        for ref in capture.relations:
            _need(ref.order_id in nodes and ref.order_id not in related and ref.sha256 in blobs, "relation_source_ambiguous")
            related[ref.order_id] = _related(blobs[ref.sha256])
            _need(related[ref.order_id] <= set(nodes), "source_graph_incomplete")
        edges = defaultdict(set)
        for oid, node in nodes.items():
            status = str(node.get("status") or "")
            _need(status in {"Sent", "Split", "Merged"}, "source_lifecycle_pending")
            link = node.get("link_o_id")
            if link not in (None, "", 0, Decimal(0)):
                target = _id(link)
                _need(target in nodes, "typed_relation_target_missing")
                a, b = (oid, target) if status == "Merged" else (target, oid)
                _need(a != b and (b in related.get(a, set()) or a in related.get(b, set())), "typed_relation_not_observed")
                edges[a].add(b)
        visiting, visited = set(), set()
        def visit(oid):
            _need(oid not in visiting, "source_graph_cycle")
            if oid in visited:
                return
            visiting.add(oid)
            for child in sorted(edges[oid]):
                visit(child)
            visiting.remove(oid)
            visited.add(oid)
        visit(source_id)
        _need(all(oid in related for oid in visited if nodes[oid]["status"] in {"Split", "Merged"}), "source_graph_incomplete")
        terminals = {oid: nodes[oid] for oid in sorted(visited) if nodes[oid]["status"] == "Sent"}
        _need(terminals, "no_proved_terminal_shipments")
        graph_refs = [{"kind": kind, "order_id": n.order_id, "sha256": n.sha256}
                      for kind, collection in (("detail", capture.nodes), ("relations", capture.relations))
                      for n in sorted(collection, key=lambda v: v.order_id)]
        used = set()
        for (iid, sku), row in sorted(logical.items()):
            record = {"source_member_id": iid, "source_sku": sku, "logical_quantity": str(row["quantity"]), "status": "pending", "members": []}
            try:
                parents = [i for i in source["items"] if _id(i.get("oi_id")) == iid]
                _need(parents, "original_member_missing")
                scalar = len(parents) == 1 and str(parents[0].get("sku_id")) == sku
                if scalar:
                    _need(_number(parents[0].get("qty")) == row["quantity"], "original_member_quantity_changed")
                else:
                    _need(all(i.get("is_combine_item") is True and not i.get("created") and str(i.get("src_combine_sku_id")) == sku
                              and _number(i.get("src_combine_sku_qty")) == row["quantity"] for i in parents), "bundle_original_inventory_unproved")
                component_keys, members = set(), []
                for parent in parents:
                    _need(parent.get("sku_type") == "normal" and parent.get("is_gift") is not True and parent.get("is_virtual") is not True, "non_normal_member_separate")
                    _need(_platform(parent.get("oi_so_id"), shops) == request.platform_order_id and str(parent.get("outer_oi_id") or "") == row["child"], "original_member_platform_conflict")
                    keys = ("oi_so_id", "outer_oi_id", "raw_so_id", "outer_so_id", "raw_sku_id", "src_combine_sku_id")
                    binding = {k: str(parent[k]) for k in keys if parent.get(k) not in (None, "")}
                    _need(binding.get("oi_so_id") and binding.get("raw_so_id"), "original_member_identity_insufficient")
                    key = str(parent.get("sku_id")), tuple(sorted(binding.items()))
                    _need(key not in component_keys, "original_component_identity_ambiguous")
                    component_keys.add(key)
                    equivalent = [i for i in source["items"] if str(i.get("sku_id")) == key[0] and all(str(i.get(k, "")) == v for k, v in binding.items())]
                    _need(len(equivalent) == 1, "original_component_identity_ambiguous")
                    candidates = []
                    for oid, target in terminals.items():
                        for index, item in enumerate(target["items"]):
                            if str(item.get("sku_id")) != key[0] or any(str(item.get(k, "")) != v for k, v in binding.items()):
                                continue
                            _need(item.get("item_status") == "Sent" and item.get("sku_type") == "normal" and item.get("is_gift") is not True and item.get("is_virtual") is not True
                                  and item.get("is_combine_item") is not True and item.get("is_combine") is not True and bool(item.get("created")), "physical_member_lifecycle_unproved")
                            _need(_created_day(item["created"]) >= original_day, "physical_member_creation_precedes_original")
                            physical = oid, _id(item.get("oi_id"))
                            _need(physical not in used, "physical_member_reused")
                            _need(_company(target) == _company(source), "price_company_changed")
                            candidates.append((physical, item, index))
                    _need(candidates and len({v[0] for v in candidates}) == len(candidates), "physical_member_missing_or_ambiguous")
                    with localcontext() as ctx:
                        ctx.prec = 256
                        total = sum((_number(v[1].get("qty")) for v in candidates), Decimal(0))
                    _need(total == _number(parent.get("qty")), "physical_quantity_not_conserved")
                    for physical, item, index in candidates:
                        members.append({"internal_order_id": physical[0], "internal_sub_order_id": physical[1], "source_order_id": source_id,
                            "source_member_id": iid, "source_sku": sku, "sku": key[0], "quantity": str(_number(item["qty"])),
                            "pricing_day": original_day, "company": _company(source), "logical_quantity": str(row["quantity"]),
                            "bundle_component": not scalar, "platform_order_id": request.platform_order_id,
                            "platform_sub_order_id": row["child"], "source_anchors": row["anchors"], "shop_id": _id(source.get("shop_id")),
                            "native_anchors": [{"sha256": a.sha256, "sheet": a.sheet, "row": a.row} for a in request.native_anchors],
                            "graph_refs": graph_refs, "source_raw": refs[source_id], "physical_raw": refs[physical[0]],
                            "observed_flags": {
                                "source_member": _observed(parent, ("is_suspect", "sku_type", "is_gift", "is_virtual", "is_combine_item", "is_combine", "item_status", "created")),
                                "physical_member": _observed(item, ("is_suspect", "sku_type", "is_gift", "is_virtual", "is_combine_item", "is_combine", "item_status", "created")),
                                "source_header": _observed(source, ("type", "status", "is_split", "is_merge", "order_date")),
                                "physical_header": _observed(nodes[physical[0]], ("type", "status", "is_split", "is_merge", "order_date"))},
                            "physical_item_pointer": "/items/" + str(index)})
                _need(len({(m["internal_order_id"], m["internal_sub_order_id"]) for m in members}) == len(members), "physical_member_reused")
                used.update((m["internal_order_id"], m["internal_sub_order_id"]) for m in members)
                record.update(status="proved", members=members)
                result["members"].extend(members)
            except MemberPending as exc:
                record["reason"] = exc.reason
                result["reasons"].append(exc.reason)
            result["rows"].append(record)
        result["status"] = "proved" if all(r["status"] == "proved" for r in result["rows"]) else "pending"
    except MemberPending as exc:
        result["reasons"].append(exc.reason)
        result["members"] = []
    except (KeyError, IndexError, TypeError, ValueError, ET.ParseError, zipfile.BadZipFile, OverflowError, DecimalException):
        result["reasons"].append("source_contract_invalid")
        result["members"] = []
    return result
