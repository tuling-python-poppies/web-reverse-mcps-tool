"""Bounded, loss-aware request comparison. No requests are issued or replayed."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qsl, urlsplit


def request_fields(entry: dict, include_headers: bool, include_body: bool) -> dict:
    parts = urlsplit(entry["url"])
    fields = {"method": entry["method"], "origin": parts.scheme + "://" + parts.netloc,
              "path": parts.path, "query.raw": parts.query}
    query: dict[str, list[str]] = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.setdefault(key, []).append(value)
    for key, values in query.items():
        # JSON quoting keeps literal dots/slashes in names unambiguous.
        fields["query[" + json.dumps(key, ensure_ascii=False) + "]"] = values
    if include_headers:
        for key, value in (entry.get("request_headers") or {}).items():
            fields["header[" + json.dumps(key.lower()) + "]"] = value
    if include_body:
        body = entry.get("request_post_data")
        fields["body.raw"] = body
        if isinstance(body, str):
            try:
                parsed = json.loads(body)
                # Preserve duplicate JSON keys in body.raw; this view is only
                # an explanatory projection, never input for a signing formula.
                if isinstance(parsed, dict):
                    for key, value in parsed.items():
                        fields["body.json[" + json.dumps(key, ensure_ascii=False) + "]"] = value
            except (ValueError, RecursionError):
                pass
    return fields


def value_summary(value, max_chars: int, present: bool = True) -> dict:
    if not present:
        return {"present": False}
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    summary = {"present": True, "preview": encoded[:max_chars], "length": len(encoded),
               "length_unit": "serialized_json_characters",
               "truncated": len(encoded) > max_chars,
               "sha256_scope": "canonical_json_utf8",
               "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}
    if isinstance(value, str):
        raw = value.encode("utf-8")
        summary["raw_utf8"] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return summary



def compare_requests(entries: list[dict], *, include_headers: bool, include_body: bool,
                     max_value_chars: int, max_fields: int) -> dict:
    samples = [request_fields(e, include_headers, include_body) for e in entries]
    keys = sorted(set().union(*(s.keys() for s in samples)))
    changed, constant = [], []
    for key in keys:
        summaries = [value_summary(s.get(key), max_value_chars, key in s) for s in samples]
        # Compare full digests, not truncated previews. Absence differs from null.
        identity = {(v["present"], v.get("sha256")) for v in summaries}
        row = {"field": key, "values": [{"request_id": e["id"], **v} for e, v in zip(entries, summaries)]}
        (changed if len(identity) > 1 else constant).append(row)
    warnings = []
    if include_headers and any(not e.get("headers_complete") for e in entries):
        warnings.append("Some headers are incomplete; absence cannot prove a header was not sent.")
    if include_body and any(e.get("request_post_data_error") for e in entries):
        warnings.append("Some request bodies were unavailable; body.raw=null is not proof of an empty body.")
    result = {"request_ids": [e["id"] for e in entries],
              "changed": changed[:max_fields], "constant_fields": [r["field"] for r in constant][:max_fields],
              "changed_count": len(changed), "constant_count": len(constant),
              "fields_truncated": len(changed) > max_fields or len(constant) > max_fields,
              "warnings": warnings,
              "interpretation": "Observed sample differences only; varying values do not identify a signing algorithm. query.raw/body.raw preserve spelling and order; decoded fields are explanatory. Summary sha256 hashes canonical JSON; raw_utf8 hashes exact string bytes (use body.raw.raw_utf8 for HTTP body checks)."}
    return result
