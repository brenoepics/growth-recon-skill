#!/usr/bin/env python3
"""Decode captured browser requests and dataLayer dumps into what actually fired.

The container read tells you what *would* fire. This tells you what *did*, on one
visit, with the numbers attached -- because conversion values are pushed from the
dataLayer at runtime and never appear in the published container. A measurement
hit carries them in the query string, so a saved network capture answers the one
question static configuration cannot.

Usage:
    python3 runtime_parse.py capture.json                  # requests, dataLayer, or both
    python3 runtime_parse.py requests.json --datalayer dl.json
    python3 runtime_parse.py capture.json --json           # machine-readable

Input is deliberately sloppy-tolerant, because capture formats vary by tool:
  - a JSON array of URL strings
  - a JSON array of request objects (`url`, or `name` as performance.getEntries
    gives it, or HAR's nested `request.url`)
  - a HAR file (`{"log": {"entries": [...]}}`)
  - a JSON array of dataLayer pushes
  - one object holding any mix of the above under `requests` / `dataLayer`
  - a plain text file with one URL per line

Honest limits (see references/runtime-observation.md for the full list):
  - One capture is one session, from one geo, under one consent state. Absence of
    a hit is not evidence the tag does not exist.
  - A value in a hit is the value *they computed for this pageview*. It is not
    their CAC, their bid, or their margin.
  - POST bodies (TikTok, Clarity, server-side endpoints) carry their payload
    outside the URL. Unless the capture saved the body, those hits are reported
    as fired-with-payload-unseen rather than guessed at.
"""

import base64
import json
import re
import sys
import urllib.parse
from collections import Counter, OrderedDict

# ---------------------------------------------------------------------------
# What a value param is called, per vendor. Order matters: the first key present
# wins, so the most specific/authoritative name for that vendor goes first.
# `epn.value` beats `ep.value` because GA4's epn.* namespace is the numeric one;
# a value arriving in ep.* was sent as a string, which is worth knowing because
# it is a common cause of a platform refusing to use it for bidding.
# ---------------------------------------------------------------------------
GA4_VALUE_KEYS = ["epn.value", "ep.value", "val", "epn.revenue", "ep.revenue"]
ADS_VALUE_KEYS = ["value", "epn.value", "ep.value", "cost"]
ADS_CURRENCY_KEYS = ["currency_code", "currency", "cu", "epn.currency", "ep.currency"]
GA4_CURRENCY_KEYS = ["cu", "ep.currency", "epn.currency"]

# GA4 packs each ecommerce item into one pr<N> param as `~`-separated fields with
# a two-character type prefix. Without this map the richest part of a purchase
# hit -- the actual basket, with per-item prices -- reads as line noise.
GA4_ITEM_PREFIXES = {
    "id": "item_id", "nm": "item_name", "br": "item_brand", "ca": "item_category",
    "c2": "item_category2", "c3": "item_category3", "c4": "item_category4",
    "c5": "item_category5", "va": "item_variant", "pr": "price", "qt": "quantity",
    "ds": "discount", "af": "affiliation", "cp": "coupon", "ln": "item_list_name",
    "li": "item_list_id", "lp": "index", "pi": "promotion_id", "pn": "promotion_name",
    "cn": "creative_name", "cs": "creative_slot", "lo": "location_id",
}

# Params that mean "a person was identified or matched", never printed by value.
# Presence is the finding; the value is someone's hashed email and none of our
# business. Scanning for these is how you tell enhanced conversions / CAPI
# matching apart from plain pageview tracking.
USER_DATA_KEYS = {
    "uid": "GA4 user_id set",
    "ga_uid": "GA4 user id passed to Google Ads",
    "em": "hashed email (enhanced conversions)",
    "emd": "hashed email, deduped (enhanced conversions)",
    "ph": "hashed phone (enhanced conversions)",
    "fn": "hashed first name (enhanced conversions)",
    "sn": "hashed surname (enhanced conversions)",
    "ec_mode": "enhanced conversions mode declared",
    "oid": "order id (deduplication / offline matching)",
    "eid": "event id (browser<->server deduplication, i.e. a CAPI pair exists)",
    "external_id": "first-party id shared with the platform",
    "pd": "hashed partner data",
    "udff": "user-data fields flag",
}

# Script/loader URLs. These prove a vendor is live and hand over an account ID,
# but they are not events -- counting them as "what fired" inflates the picture,
# which is the main way a runtime read gets overclaimed.
LOADER_PATTERNS = [
    (r"googletagmanager\.com/gtm\.js", "GTM container loaded", r"id=(GTM-[A-Z0-9]+)"),
    (r"googletagmanager\.com/gtag/js", "Google tag loaded", r"id=((?:G|AW|GT|DC)-[A-Z0-9-]+)"),
    (r"connect\.facebook\.net/signals/config/(\d+)", "Meta pixel config loaded", r"config/(\d+)"),
    (r"connect\.facebook\.net/.*/fbevents\.js", "Meta pixel library loaded", None),
    (r"analytics\.tiktok\.com/i18n/pixel/events\.js", "TikTok pixel loaded", r"sdkid=([A-Z0-9]+)"),
    (r"snap\.licdn\.com/li\.lms-analytics", "LinkedIn Insight loaded", None),
    (r"bat\.bing\.com/(?:bat\.js|p/action/(\d+))", "Microsoft UET loaded", r"action/(\d+)"),
    (r"s\.pinimg\.com/ct/core", "Pinterest tag loaded", None),
    (r"clarity\.ms/tag/([a-z0-9]+)", "Microsoft Clarity loaded", r"tag/([a-z0-9]+)"),
    (r"static\.ads-twitter\.com", "X/Twitter tag loaded", None),
    (r"sc-static\.net/scevent", "Snapchat pixel loaded", None),
    (r"cdn\.optimizely\.com", "Optimizely loaded", None),
    (r"cdn\.cookielaw\.org|cookiebot|usercentrics|didomi", "consent platform loaded", None),
]


# ---------------------------------------------------------------------------
# Loading. Be liberal: the capture comes from whatever tool the agent had, and a
# parser that rejects a shape it has not seen before just sends them back to the
# browser to re-capture.
# ---------------------------------------------------------------------------
URL_KEYS = ("url", "name", "requestUrl", "request_url", "href", "uri", "link")
BODY_KEYS = ("body", "postData", "post_data", "requestBody", "payload", "postBody")


def _url_of(obj):
    """Find a URL in a request-shaped dict, whatever the capture tool called it."""
    for k in URL_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.startswith(("http://", "https://", "//")):
            return v
    req = obj.get("request")
    if isinstance(req, dict):
        return _url_of(req)
    return None


def _body_of(obj):
    """Pull a POST body out, accepting HAR's {"text": ...} wrapper."""
    for k in BODY_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            t = v.get("text") or v.get("raw")
            if isinstance(t, str) and t.strip():
                return t
            if v:
                return json.dumps(v)
    req = obj.get("request")
    if isinstance(req, dict):
        return _body_of(req)
    return None


def _looks_like_request(obj):
    """Tell a captured request from a dataLayer push.

    Both are plain objects and a dataLayer push can legitimately carry a `url`
    key, so a url alone is not enough; the GTM-specific keys are the tiebreak.
    """
    if not isinstance(obj, dict):
        return False
    if any(k in obj for k in ("method", "status", "initiatorType", "resourceType",
                              "response", "statusCode", "mimeType")):
        return True
    if "request" in obj and isinstance(obj["request"], dict):
        return True
    if any(k in obj for k in ("event", "ecommerce", "gtm.start", "gtm.uniqueEventId")):
        return False
    return _url_of(obj) is not None


def _collect(node, requests, pushes, depth=0):
    """Walk an arbitrary decoded JSON structure, sorting what it finds."""
    if depth > 6:
        return
    if isinstance(node, str):
        if node.startswith(("http://", "https://")):
            requests.append({"url": node})
        return
    if isinstance(node, list):
        for item in node:
            if isinstance(item, (str, list)):
                _collect(item, requests, pushes, depth + 1)
            elif isinstance(item, dict):
                if _looks_like_request(item):
                    requests.append(item)
                else:
                    pushes.append(item)
        return
    if isinstance(node, dict):
        # A HAR file, or a hand-rolled wrapper object.
        if isinstance(node.get("log"), dict):
            _collect(node["log"].get("entries") or [], requests, pushes, depth + 1)
        for key in ("requests", "entries", "network", "networkRequests", "hits",
                    "resources", "har"):
            if key in node:
                _collect(node[key], requests, pushes, depth + 1)
        for key in ("dataLayer", "datalayer", "data_layer", "pushes", "dl"):
            if key in node:
                val = node[key]
                if isinstance(val, list):
                    pushes.extend(x for x in val if isinstance(x, dict))
        if not any(k in node for k in ("log", "requests", "entries", "network",
                                       "networkRequests", "hits", "resources", "har",
                                       "dataLayer", "datalayer", "data_layer",
                                       "pushes", "dl")):
            if _looks_like_request(node):
                requests.append(node)
            else:
                pushes.append(node)


def load_capture(path):
    """Read one capture file. Returns (requests, dataLayer pushes, notes)."""
    notes = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as e:
        return [], [], [f"!! could not read {path}: {e}"]
    if not raw.strip():
        return [], [], [f"!! {path} is empty"]

    requests, pushes = [], []
    try:
        data = json.loads(raw)
    except ValueError:
        # Not JSON. A pasted list of URLs is a perfectly reasonable capture, and
        # so is JSON-lines, so pull URLs out line by line instead of refusing.
        for line in raw.splitlines():
            line = line.strip().strip(",").strip('"')
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                (requests if _looks_like_request(obj) else pushes).append(obj)
                continue
            m = re.search(r"https?://\S+", line)
            if m:
                requests.append({"url": m.group(0).rstrip('",;)')})
        notes.append(f"{path} is not JSON -- read {len(requests)} URL(s) line by line")
        return requests, pushes, notes

    _collect(data, requests, pushes)
    return requests, pushes, notes


# ---------------------------------------------------------------------------
# Query-string handling. Two wrinkles the obvious implementation gets wrong:
# Floodlight separates its params with ';' instead of '&', and GA4 sends repeated
# pr<N> params that must not collapse into one.
# ---------------------------------------------------------------------------
def parse_params(url):
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return {}, "", ""
    host, path, query = parsed.netloc.lower(), parsed.path, parsed.query

    # Floodlight puts its payload in the PATH as ;key=value pairs (and some
    # Google Ads endpoints do too), so the standard parser sees no query at all.
    semi = ""
    if ";" in path:
        path, _, semi = path.partition(";")
    blob = query if query else ""
    if semi:
        blob = (blob + "&" if blob else "") + semi.replace(";", "&")

    out = OrderedDict()
    for part in re.split(r"[&]", blob):
        if not part:
            continue
        k, _, v = part.partition("=")
        k = urllib.parse.unquote_plus(k)
        v = urllib.parse.unquote_plus(v)
        if k in out and out[k] != v:
            out[k] = out[k] + " | " + v
        else:
            out[k] = v
    return out, host, path


def as_number(raw):
    """Parse a value param. Returns (float|None, state).

    The state is the point of the whole exercise: a hit that carries `value=` with
    nothing in it is a different and more interesting finding than a hit with no
    value param at all, because it means the plumbing is there and the number is
    not arriving.
    """
    if raw is None:
        return None, "absent"
    s = str(raw).strip()
    if not s:
        return None, "present-but-empty"
    s = s.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})*(\.\d+)?", s) else s
    try:
        return float(s), "number"
    except ValueError:
        return None, "present-but-not-numeric"


def pick_value(params, keys):
    for k in keys:
        if k in params:
            num, state = as_number(params[k])
            return {"value": num, "state": state, "source": k, "raw": params[k][:60]}
    return {"value": None, "state": "absent", "source": None, "raw": None}


def pick_currency(params, keys):
    for k in keys:
        v = (params.get(k) or "").strip()
        if v:
            return v.upper()[:8]
    return None


def decode_gcs(gcs):
    """Decode Google's consent signal string, e.g. G111.

    Format is 'G' + format version + ad_storage + analytics_storage, each 1 or 0.
    Reported only when it is that exact shape; Google has changed this before and
    a mis-decoded consent state is worse than a raw one.
    """
    if not isinstance(gcs, str) or not re.fullmatch(r"G\d{3}", gcs):
        return None
    m = {"1": "granted", "0": "denied"}
    return {"ad_storage": m.get(gcs[2], gcs[2]),
            "analytics_storage": m.get(gcs[3], gcs[3])}


def decode_ga4_items(params):
    """Decode pr1..prN into readable items."""
    items = []
    for key in sorted((k for k in params if re.fullmatch(r"pr\d+", k)),
                      key=lambda k: int(k[2:])):
        item = {}
        custom = {}
        for field in params[key].split("~"):
            if len(field) < 2:
                continue
            prefix, val = field[:2], field[2:]
            if re.fullmatch(r"k\d", prefix):
                custom.setdefault(prefix[1], {})["key"] = val
            elif re.fullmatch(r"v\d", prefix):
                custom.setdefault(prefix[1], {})["value"] = val
            else:
                name = GA4_ITEM_PREFIXES.get(prefix)
                if name and val != "":
                    item[name] = val
        for slot in custom.values():
            if slot.get("key"):
                item[slot["key"]] = slot.get("value", "")
        if item:
            items.append(item)
    return items


def decode_semicolon_blob(raw):
    """Google Ads remarketing packs its custom params into one `data=` string as
    `key=value;key=value`. It routinely carries the page type, product id and the
    business vertical -- their own segmentation, in their own words."""
    out = OrderedDict()
    for part in str(raw).split(";"):
        k, _, v = part.partition("=")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def try_base64_json(raw):
    """Some Google endpoints carry a base64 JSON blob (`sdl`) listing the page's
    products with prices. Attempted only where it is cheap, and silently skipped
    when it is not that -- most base64-looking params are opaque ids."""
    s = str(raw)
    if len(s) < 24 or not re.fullmatch(r"[A-Za-z0-9+/_=-]+", s):
        return None
    try:
        pad = "=" * (-len(s) % 4)
        text = base64.urlsafe_b64decode(s + pad).decode("utf-8", errors="strict")
    except Exception:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def user_data_present(params):
    return [f"{k} ({USER_DATA_KEYS[k]})" for k in params
            if k in USER_DATA_KEYS and str(params[k]).strip()]


# ---------------------------------------------------------------------------
# Per-vendor decoders. Each returns a hit dict or None.
#
# Routing is by `tid` prefix before host, deliberately: /ccm/collect is served
# from google.com, pagead2.googlesyndication.com and others, and carries GA4
# hits (tid=G-) and Google Ads hits (tid=AW-) through the same path. Routing on
# the host alone mislabels every Ads conversion on a modern gtag site.
# ---------------------------------------------------------------------------
def _base_hit(vendor, endpoint, url, params):
    gcs = params.get("gcs")
    return {
        "vendor": vendor,
        "endpoint": endpoint,
        "event": None,
        "account": None,
        "value": None,
        "value_state": "absent",
        "value_source": None,
        "currency": None,
        "items": [],
        "event_params": {},
        "user_data": user_data_present(params),
        "consent": {"gcs": gcs, "gcs_decoded": decode_gcs(gcs),
                    "gcd_raw": params.get("gcd"),
                    "npa": params.get("npa")},
        "page": params.get("dl") or params.get("url") or params.get("p") or params.get("~oref"),
        "notes": [],
        "url": url[:400],
    }


def decode_google_collect(url, host, path, params):
    """GA4 and gtag-routed Google Ads hits: /g/collect, /collect, /ccm/collect."""
    tid = params.get("tid") or params.get("tids") or ""
    is_ads = tid.startswith(("AW-", "DC-"))
    vendor = "Google Ads (gtag)" if is_ads else "GA4"
    hit = _base_hit(vendor, host + path, url, params)
    hit["account"] = tid or None
    hit["event"] = params.get("en") or None
    v = pick_value(params, ADS_VALUE_KEYS if is_ads else GA4_VALUE_KEYS)
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ADS_CURRENCY_KEYS if is_ads else GA4_CURRENCY_KEYS)
    hit["items"] = decode_ga4_items(params)
    hit["event_params"] = {k.split(".", 1)[1]: val for k, val in params.items()
                           if k.startswith(("ep.", "epn.", "up.", "upn."))}
    if not tid:
        hit["notes"].append("no tid in this hit -- destination property not identifiable")
    if v["source"] == "ep.value":
        hit["notes"].append("value arrived in ep.* (string namespace), not epn.* (numeric)")
    if "sdl" in params:
        blob = try_base64_json(params["sdl"])
        if blob is not None:
            hit["notes"].append("sdl= carries a base64 JSON product list")
            hit["decoded_extras"] = {"sdl": blob}
    return hit


def decode_google_ads_conversion(url, host, path, params):
    """googleadservices /pagead/conversion, doubleclick viewthroughconversion,
    rmkt/collect and 1p-user-list. Conversion id is in the path, label in a param."""
    m = re.search(r"/(?:pagead/conversion|pagead/viewthroughconversion|rmkt/collect|"
                  r"pagead/1p-user-list|pagead/landing)/(\d{6,12})", path)
    conv_id = m.group(1) if m else (params.get("tid") or "").replace("AW-", "") or None
    kind = ("conversion" if "pagead/conversion" in path else
            "remarketing / view-through" if "viewthroughconversion" in path or "rmkt" in path else
            "user list (audience)" if "1p-user-list" in path else "ads")
    hit = _base_hit("Google Ads", host + path, url, params)
    hit["account"] = f"AW-{conv_id}" if conv_id else None
    hit["event"] = params.get("en") or kind
    hit["conversion_label"] = params.get("label") or None
    hit["conversion_kind"] = kind
    v = pick_value(params, ADS_VALUE_KEYS)
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ADS_CURRENCY_KEYS)
    if params.get("bttype"):
        hit["notes"].append(f"conversion type declared as '{params['bttype']}'")
    if params.get("data"):
        hit["event_params"] = decode_semicolon_blob(params["data"])
    if kind == "user list (audience)":
        hit["notes"].append("audience membership ping, not a conversion -- do not count it as one")
    # A conversion that reports 0 is not a broken value; it is a deliberate
    # choice, and it says this action is not feeding value-based bidding.
    if hit["value_state"] == "number" and hit["value"] == 0:
        hit["notes"].append("value is exactly 0 -- this action reports no revenue, so it "
                            "cannot be driving value-based bidding")
    if hit["conversion_label"] is None and kind == "conversion":
        hit["notes"].append("no label param -- conversion action not identifiable from this hit")
    return hit


def decode_floodlight(url, host, path, params):
    hit = _base_hit("Floodlight (DV360/CM360)", host + path, url, params)
    hit["account"] = params.get("src") and f"DC-{params['src']}" or None
    hit["event"] = "/".join(x for x in (params.get("type"), params.get("cat")) if x) or None
    v = pick_value(params, ["cost", "value", "u1"])
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ["currency_code", "currency"])
    if params.get("qty"):
        hit["event_params"]["qty"] = params["qty"]
    for k, val in params.items():
        if re.fullmatch(r"u\d{1,2}", k) and val:
            hit["event_params"][k] = val
    hit["notes"].append("type/cat are the Floodlight group and activity tags -- "
                        "their own names for this activity")
    return hit


def decode_meta(url, host, path, params):
    hit = _base_hit("Meta pixel", host + path, url, params)
    hit["account"] = params.get("id") or None
    hit["event"] = params.get("ev") or None
    v = pick_value(params, ["cd[value]", "cd[value][value]", "value"])
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ["cd[currency]", "currency"])
    hit["event_params"] = {k[3:-1]: val for k, val in params.items()
                           if k.startswith("cd[") and k.endswith("]")}
    if params.get("eid"):
        hit["notes"].append("eid present -- browser and Conversions API hits are being deduped, "
                            "so a server-side copy of this event exists")
    return hit


def decode_uet(url, host, path, params):
    hit = _base_hit("Microsoft UET", host + path, url, params)
    hit["account"] = params.get("ti") or None
    hit["event"] = params.get("evt") or params.get("ea") or None
    # gv is UET's revenue field; ev is the generic event value. Both are numeric
    # and sites use them inconsistently, so report which one carried the number.
    v = pick_value(params, ["gv", "ev", "revenue_value"])
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ["gc", "currency"])
    for k in ("ec", "ea", "el"):
        if params.get(k):
            hit["event_params"][{"ec": "category", "ea": "action", "el": "label"}[k]] = params[k]
    if params.get("msclkid") and params["msclkid"] not in ("N", ""):
        hit["notes"].append("msclkid present -- this visit arrived from a Microsoft ad click")
    return hit


def decode_linkedin(url, host, path, params):
    hit = _base_hit("LinkedIn", host + path, url, params)
    hit["account"] = params.get("pid") or None
    hit["event"] = ("conversion" if params.get("conversionId") else
                    params.get("event") or "insight ping")
    if params.get("conversionId"):
        hit["conversion_label"] = params["conversionId"]
    v = pick_value(params, ["value", "conversionValue", "amount"])
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ["currency", "currencyCode"])
    return hit


def decode_tiktok(url, host, path, params, body=None):
    hit = _base_hit("TikTok", host + path, url, params)
    hit["account"] = params.get("sdkid") or params.get("pixel_code") or None
    hit["event"] = params.get("event") or None
    payload = None
    if body:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, dict):
        hit["event"] = payload.get("event") or hit["event"]
        ctx = payload.get("context") or {}
        pixel = (ctx.get("pixel") or {}) if isinstance(ctx, dict) else {}
        hit["account"] = pixel.get("code") or hit["account"]
        props = payload.get("properties") or {}
        if isinstance(props, dict):
            v = pick_value(props, ["value", "price"])
            hit.update({"value": v["value"], "value_state": v["state"],
                        "value_source": v["source"]})
            hit["currency"] = pick_currency(props, ["currency"])
            hit["event_params"] = {k: str(val)[:120] for k, val in props.items()
                                   if k not in ("value", "currency")}
    elif path.rstrip("/").endswith(("/pixel", "/track")):
        hit["notes"].append("TikTok posts its payload in the request body -- "
                            "event and value are not in the URL and were not captured")
    return hit


def decode_pinterest(url, host, path, params):
    hit = _base_hit("Pinterest", host + path, url, params)
    hit["account"] = params.get("tid") or None
    hit["event"] = params.get("event") or None
    v = pick_value(params, ["value"])
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ["currency"])
    # Pinterest nests event data as a JSON blob in `ed`, where the value lives.
    if params.get("ed"):
        try:
            ed = json.loads(params["ed"])
        except ValueError:
            ed = None
        if isinstance(ed, dict):
            v = pick_value(ed, ["value", "order_value"])
            if v["state"] != "absent":
                hit.update({"value": v["value"], "value_state": v["state"],
                            "value_source": "ed." + str(v["source"])})
            hit["currency"] = hit["currency"] or pick_currency(ed, ["currency"])
            hit["event_params"] = {k: str(val)[:120] for k, val in ed.items()
                                  if k not in ("value", "currency")}
    return hit


def decode_reddit(url, host, path, params):
    hit = _base_hit("Reddit", host + path, url, params)
    hit["account"] = params.get("id") or params.get("advertiser_id") or None
    hit["event"] = params.get("event") or params.get("ev") or None
    v = pick_value(params, ["m.value", "value", "conversion_value"])
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ["m.currency", "currency"])
    return hit


def decode_snapchat(url, host, path, params):
    hit = _base_hit("Snapchat", host + path, url, params)
    hit["account"] = params.get("pid") or None
    hit["event"] = params.get("ev") or None
    v = pick_value(params, ["price", "value"])
    hit.update({"value": v["value"], "value_state": v["state"], "value_source": v["source"]})
    hit["currency"] = pick_currency(params, ["currency"])
    return hit


# Host/path -> decoder. Checked in order, so the specific Google endpoints are
# matched before the generic /collect rule.
ROUTES = [
    (r"doubleclick\.net/(?:activity|activityi)|\.fls\.doubleclick\.net", decode_floodlight),
    (r"/pagead/conversion/|/pagead/viewthroughconversion/|/rmkt/collect/|/pagead/1p-user-list/",
     decode_google_ads_conversion),
    (r"/g/collect|/ccm/collect|/ccm/s/collect|/mp/collect", decode_google_collect),
    (r"facebook\.com/tr", decode_meta),
    (r"bat\.bing\.com/action", decode_uet),
    (r"ads\.linkedin\.com|px\.ads\.linkedin\.com", decode_linkedin),
    (r"analytics\.tiktok\.com/api|tiktokw\.us/api", decode_tiktok),
    (r"ct\.pinterest\.com", decode_pinterest),
    (r"redditmedia\.com|alb\.reddit\.com", decode_reddit),
    (r"tr\.snapchat\.com", decode_snapchat),
]


def is_bare_ga_collect(path, params):
    """A plain `/collect` path is only a GA hit when its params say so.

    Server-side GTM moves collection onto the target's own domain, so the hit can
    arrive at `theircdn.example.com/collect` with nothing Google-shaped about the
    host. Matching the path alone instead swallows every unrelated `/collect`
    endpoint (Microsoft Clarity serves one), so require the GA payload shape.
    """
    if not re.search(r"/collect/?$", path):
        return False
    tid = params.get("tid") or ""
    return bool(params.get("v") == "2" or params.get("en")
                or tid.startswith(("G-", "AW-", "UA-", "DC-", "GT-")))


def classify_loader(url):
    for pattern, label, id_pat in LOADER_PATTERNS:
        if re.search(pattern, url, re.I):
            ident = None
            if id_pat:
                m = re.search(id_pat, url)
                ident = m.group(1) if m else None
            return {"what": label, "id": ident, "url": url[:200]}
    return None


def decode_request(obj):
    """Decode one captured request. Returns ("hit"|"loader"|"other", payload)."""
    url = _url_of(obj) if isinstance(obj, dict) else None
    if not url:
        return "other", {"reason": "no URL found in this entry",
                         "entry": json.dumps(obj, default=str)[:160]}
    if url.startswith("//"):
        url = "https:" + url
    params, host, path = parse_params(url)
    body = _body_of(obj) if isinstance(obj, dict) else None
    target = host + path

    matched = None
    for pattern, fn in ROUTES:
        if re.search(pattern, target, re.I) or re.search(pattern, url, re.I):
            matched = fn
            break
    if matched is None and is_bare_ga_collect(path, params):
        matched = decode_google_collect
    if matched is not None:
        try:
            hit = (matched(url, host, path, params, body)
                   if matched is decode_tiktok else matched(url, host, path, params))
        except Exception as e:
            return "other", {"reason": f"decoder failed: {e}", "entry": url[:160]}
        method = (obj.get("method") or "GET").upper() if isinstance(obj, dict) else "GET"
        if method != "GET" and not params and not body:
            hit["notes"].append(
                f"{method} with no captured body -- it fired, but its payload "
                f"(event, value) was not recorded")
        return "hit", hit

    loader = classify_loader(url)
    if loader:
        return "loader", loader
    return "other", {"reason": "not a known measurement endpoint", "entry": url[:200]}


# ---------------------------------------------------------------------------
# dataLayer. This is where the values the container could only reference as
# {{variable}} are sitting as literals, so it is read for numbers first.
# ---------------------------------------------------------------------------
VALUE_PATHS = [
    ("ecommerce.value", ("ecommerce", "value")),
    ("ecommerce.purchase.actionField.revenue", ("ecommerce", "purchase", "actionField", "revenue")),
    ("ecommerce.checkout.actionField.revenue", ("ecommerce", "checkout", "actionField", "revenue")),
    ("ecommerce.transaction_total", ("ecommerce", "transaction_total")),
    ("value", ("value",)),
    ("conversionValue", ("conversionValue",)),
    ("conversion_value", ("conversion_value",)),
    ("lead_value", ("lead_value",)),
    ("revenue", ("revenue",)),
    ("cart_total", ("cart_total",)),
    ("order_total", ("order_total",)),
    ("price", ("price",)),
]
CURRENCY_PATHS = [
    ("ecommerce", "currency"), ("ecommerce", "currencyCode"),
    ("currency",), ("currencyCode",), ("ecommerce", "purchase", "actionField", "currency"),
]
# Product arrays, GA4 and legacy Enhanced Ecommerce. Both are still live in the
# wild -- Shopify dataLayer apps in particular still push the legacy `detail`
# and `purchase` shapes, which a GA4-only reader would walk straight past.
PRODUCT_PATHS = [
    ("ecommerce", "items"),
    ("ecommerce", "detail", "products"), ("ecommerce", "add", "products"),
    ("ecommerce", "remove", "products"), ("ecommerce", "checkout", "products"),
    ("ecommerce", "purchase", "products"), ("ecommerce", "impressions"),
    ("ecommerce", "click", "products"), ("ecommerce", "cart_contents", "products"),
]
# Keys whose values are a person's data. Reported as present, never printed.
PII_KEY_RE = re.compile(r"(^|_)(email|e_mail|phone|tel|first_?name|last_?name|surname|"
                        r"address|zip|postcode|dob|birth|ssn|user_email)($|_)", re.I)


def _dig(obj, path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def describe_push(push, index):
    """Summarise one dataLayer push, resolving its value and currency."""
    out = {"index": index, "event": None, "value": None, "value_state": "absent",
           "value_source": None, "currency": None, "products": [], "keys": [],
           "consent": None, "person_data_keys": [], "notes": []}
    if not isinstance(push, dict):
        out["notes"].append(f"not an object: {str(push)[:80]}")
        return out
    out["keys"] = [k for k in push if not k.startswith("gtm.")][:20]

    # gtag() calls land in the dataLayer as an arguments object with numeric
    # keys, so `{"0":"config","1":"AW-..."}` is a real push and not junk.
    if "0" in push and isinstance(push.get("0"), str):
        cmd = push.get("0")
        out["event"] = f"gtag({cmd})"
        target = push.get("1")
        if isinstance(target, str):
            out["notes"].append(f"gtag command target: {target}")
        payload = push.get("2")
        if isinstance(payload, dict):
            push = {**payload, "event": out["event"]}

    ev = push.get("event")
    if isinstance(ev, str):
        out["event"] = ev
    elif isinstance(push.get("value"), dict) and isinstance(push["value"].get("event"), str):
        # Some wrappers nest the real push one level down under `value`.
        push = push["value"]
        out["event"] = push.get("event")

    for label, path in VALUE_PATHS:
        found = _dig(push, path)
        if found is not None and not isinstance(found, (dict, list)):
            num, state = as_number(found)
            out.update({"value": num, "value_state": state, "value_source": label})
            break
    for path in CURRENCY_PATHS:
        cur = _dig(push, path)
        if isinstance(cur, str) and cur.strip():
            out["currency"] = cur.strip().upper()[:8]
            break
    for path in PRODUCT_PATHS:
        prods = _dig(push, path)
        if isinstance(prods, list) and prods:
            out["products"] = [p for p in prods if isinstance(p, dict)][:20]
            out["products_from"] = ".".join(path)
            break

    blob = json.dumps(push, default=str)
    if re.search(r"(ad_storage|analytics_storage|consent_v2|consent_state|gtm\.init_consent)", blob):
        out["consent"] = _dig(push, ("marketing", "consent_v2")) or _dig(push, ("consent",)) \
            or _dig(push, ("consent_v2",)) or "referenced"
    for key in re.findall(r'"([A-Za-z0-9_]{2,40})"\s*:', blob):
        if PII_KEY_RE.search(key) and key not in out["person_data_keys"]:
            out["person_data_keys"].append(key)

    # A value with no currency is a real defect, not a parsing gap: platforms
    # that receive it have to assume an account default.
    if out["value_state"] == "number" and not out["currency"]:
        out["notes"].append("value present with no currency on this push")
    return out


def product_value(prod):
    """Best numeric price on a product entry, GA4 or legacy field names."""
    for key in ("price", "item_price", "value", "amount"):
        if key in prod:
            num, state = as_number(prod[key])
            if state == "number":
                return num, key
    return None, None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def analyse(requests=None, pushes=None, notes=None):
    requests = requests or []
    pushes = pushes or []
    out = {
        "counts": {"requests_seen": len(requests), "datalayer_pushes": len(pushes)},
        "hits": [], "loaders": [], "unrecognised": [], "datalayer": [],
        "accounts": {}, "values": [], "consent": {}, "user_data": [],
        "notes": [], "warnings": [],
    }
    # A file that could not be read is a problem with the run, not a footnote,
    # so it is promoted out of the notes and printed with the warnings.
    for n in (notes or []):
        (out["warnings"] if n.startswith("!!") else out["notes"]).append(n.lstrip("! "))

    for obj in requests:
        kind, payload = decode_request(obj)
        if kind == "hit":
            out["hits"].append(payload)
        elif kind == "loader":
            out["loaders"].append(payload)
        else:
            out["unrecognised"].append(payload)

    # Accounts seen, per vendor, from both events and loaders.
    acc = {}
    for h in out["hits"]:
        if h.get("account"):
            acc.setdefault(h["vendor"], set()).add(str(h["account"]))
    for l in out["loaders"]:
        if l.get("id"):
            acc.setdefault(l["what"], set()).add(str(l["id"]))
    out["accounts"] = {k: sorted(v) for k, v in sorted(acc.items())}

    # The money section: every real number observed, with what carried it.
    seen = set()
    for h in out["hits"]:
        if h["value_state"] != "number":
            continue
        # The label belongs in the key: two conversion actions firing the same
        # value is a real pattern (monday.com fires two labels at value=0), and
        # collapsing them hides half their optimisation surface.
        key = (h["vendor"], h.get("event"), h["value"], h["currency"],
               h.get("conversion_label"), h.get("account"))
        if key in seen:
            continue
        seen.add(key)
        out["values"].append({
            "source": "network hit", "vendor": h["vendor"], "event": h.get("event"),
            "account": h.get("account"), "label": h.get("conversion_label"),
            "value": h["value"], "currency": h["currency"], "param": h["value_source"],
        })

    out["datalayer"] = [describe_push(p, i) for i, p in enumerate(pushes)]
    for d in out["datalayer"]:
        if d["value_state"] == "number":
            key = ("dataLayer", d["event"], d["value"], d["currency"])
            if key not in seen:
                seen.add(key)
                out["values"].append({
                    "source": "dataLayer", "vendor": "dataLayer", "event": d["event"],
                    "account": None, "label": None, "value": d["value"],
                    "currency": d["currency"], "param": d["value_source"],
                })
        for prod in d["products"]:
            num, field = product_value(prod)
            if num is None:
                continue
            key = ("product", d["event"], num, d["currency"], prod.get("id") or prod.get("item_id"))
            if key in seen:
                continue
            seen.add(key)
            out["values"].append({
                "source": f"dataLayer product ({d.get('products_from', 'items')})",
                "vendor": "dataLayer", "event": d["event"],
                "account": prod.get("id") or prod.get("item_id"),
                "label": (prod.get("name") or prod.get("item_name") or "")[:60] or None,
                "value": num, "currency": d["currency"], "param": field,
            })
    for h in out["hits"]:
        for item in h.get("items", []):
            num, field = product_value(item)
            if num is None:
                continue
            key = ("hititem", h["vendor"], num, h["currency"], item.get("item_id"))
            if key in seen:
                continue
            seen.add(key)
            out["values"].append({
                "source": f"{h['vendor']} item in hit", "vendor": h["vendor"],
                "event": h.get("event"), "account": item.get("item_id"),
                "label": (item.get("item_name") or "")[:60] or None,
                "value": num, "currency": h["currency"], "param": field,
            })

    # Values the plumbing carries but did not fill in. Reported separately,
    # because "they measure value but it arrives empty" is a finding about their
    # setup and the opposite of "no value is tracked".
    out["empty_values"] = [
        {"vendor": h["vendor"], "event": h.get("event"), "param": h["value_source"],
         "state": h["value_state"]}
        for h in out["hits"] if h["value_state"] in ("present-but-empty",
                                                     "present-but-not-numeric")]

    # Consent picture across the capture.
    gcs_counts = Counter(h["consent"]["gcs"] for h in out["hits"] if h["consent"]["gcs"])
    out["consent"] = {
        "gcs_values_seen": dict(gcs_counts),
        "gcs_decoded": {g: decode_gcs(g) for g in gcs_counts},
        "consent_update_hits": sum(1 for h in out["hits"]
                                   if (h.get("event") or "") == "consent_update"),
        "npa_flag_seen": sorted({h["consent"]["npa"] for h in out["hits"]
                                 if h["consent"].get("npa") is not None}),
        "datalayer_consent_pushes": [d["index"] for d in out["datalayer"] if d["consent"]],
    }
    if len(gcs_counts) > 1:
        out["warnings"].append(
            "more than one consent state (gcs) appears in this capture -- the hits "
            "sent before and after the consent update are not comparable; split them "
            "before concluding anything about what is consent-gated")

    ud = Counter()
    for h in out["hits"]:
        for u in h["user_data"]:
            ud[u] += 1
    out["user_data"] = [{"signal": k, "hits": v} for k, v in ud.most_common()]

    person_keys = sorted({k for d in out["datalayer"] for k in d["person_data_keys"]})
    if person_keys:
        out["warnings"].append(
            "the dataLayer carries person-data keys (" + ", ".join(person_keys[:8]) +
            "). Their presence is the finding; the values are a visitor's personal "
            "data and are out of scope -- this parser never prints them.")

    if not out["hits"] and requests:
        out["warnings"].append(
            "no measurement hits decoded from " + str(len(requests)) + " request(s). "
            "Either nothing fired on this visit (consent, geo, or an ad-blocking "
            "profile), or the capture recorded only same-origin requests -- many "
            "capture tools do. Check the capture before reporting an empty stack.")
    if not requests and not pushes:
        out["warnings"].append("nothing to parse: no requests and no dataLayer pushes found")
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fmt_value(v, cur, state):
    if state == "number":
        return f"{v:g} {cur}" if cur else f"{v:g} (no currency)"
    return {"absent": "-", "present-but-empty": "EMPTY",
            "present-but-not-numeric": "NOT A NUMBER"}.get(state, state)


def render(s):
    L = []
    a = L.append
    c = s["counts"]
    a("# Runtime observation")
    a(f"{c['requests_seen']} captured requests · {c['datalayer_pushes']} dataLayer pushes")
    a(f"{len(s['hits'])} measurement hits decoded · {len(s['loaders'])} tag loaders · "
      f"{len(s['unrecognised'])} unrecognised\n")

    for w in s["warnings"]:
        a(f"!! {w}\n")

    # The headline section: numbers, which is the whole reason to do this.
    a("## Conversion values actually observed")
    if s["values"]:
        a("   (a value here is what they computed for THIS pageview -- not their CAC,")
        a("    not their bid, not their margin)")
        for v in s["values"]:
            amount = f"{v['value']:g} {v['currency'] or '(currency missing)'}"
            label = f"  {v['label']}" if v.get("label") else ""
            acct = f"  {v['account']}" if v.get("account") else ""
            a(f"  {amount:>22}   {str(v['event'] or '?'):<22} {v['vendor']:<18} "
              f"[{v['param']}]{acct}{label}")
    else:
        a("  none. No hit or dataLayer push in this capture carried a numeric value.")
        a("  That is a statement about THIS visit, not about their setup: value-bearing")
        a("  events are usually add_to_cart, purchase, or a form thank-you page.")

    if s.get("empty_values"):
        a("\n## Values the plumbing carries but left empty")
        a("   (the field is wired up and arriving blank -- a real setup defect,")
        a("    and the opposite of 'they don't track value')")
        for e in s["empty_values"]:
            a(f"  {e['vendor']:<20} {str(e['event'] or '?'):<22} {e['param']} -> {e['state']}")

    a("\n## What fired")
    if not s["hits"]:
        a("  no network capture in this input" if not c["requests_seen"]
          else "  nothing in the capture matched a known measurement endpoint")
    grouped = OrderedDict()
    for h in s["hits"]:
        key = (h["vendor"], h.get("event"), h.get("account"),
               h.get("conversion_label"), h["value"], h["currency"], h["value_state"])
        grouped.setdefault(key, []).append(h)
    for (vendor, event, account, label, value, cur, state), hits in grouped.items():
        times = f" x{len(hits)}" if len(hits) > 1 else ""
        a(f"\n  {vendor}  {event or '(no event name)'}{times}")
        a(f"      account: {account or 'not in hit'}"
          + (f"   label: {label}" if label else ""))
        a(f"      value:   {_fmt_value(value, cur, state)}"
          + (f"   [from {hits[0]['value_source']}]" if hits[0]["value_source"] else ""))
        h = hits[0]
        if h.get("conversion_kind"):
            a(f"      kind:    {h['conversion_kind']}")
        if h.get("event_params"):
            shown = list(h["event_params"].items())[:8]
            a("      params:  " + ", ".join(f"{k}={str(v)[:40]}" for k, v in shown))
        if h.get("items"):
            a(f"      items:   {len(h['items'])}")
            for item in h["items"][:4]:
                a("        " + ", ".join(f"{k}={str(v)[:44]}" for k, v in list(item.items())[:6]))
        if h["consent"]["gcs"]:
            dec = h["consent"]["gcs_decoded"]
            detail = (f"  (ad_storage {dec['ad_storage']}, analytics_storage "
                      f"{dec['analytics_storage']})") if dec else ""
            a(f"      consent: gcs={h['consent']['gcs']}{detail}")
        if h["user_data"]:
            a(f"      identity: {', '.join(h['user_data'][:4])}")
        if h["endpoint"]:
            a(f"      endpoint: {h['endpoint']}")
        for n in h["notes"][:3]:
            a(f"      note:    {n}")
        if h.get("decoded_extras"):
            blob = json.dumps(h["decoded_extras"], default=str)
            a(f"      decoded: {blob[:300]}{'...' if len(blob) > 300 else ''}")

    if s["accounts"]:
        a("\n## Accounts and IDs reached on this visit")
        for k, v in s["accounts"].items():
            a(f"  {k}: {', '.join(v)}")

    if s["consent"]["gcs_values_seen"]:
        a("\n## Consent state during the capture")
        for g, n in s["consent"]["gcs_values_seen"].items():
            dec = s["consent"]["gcs_decoded"].get(g)
            detail = (f"ad_storage {dec['ad_storage']}, analytics_storage "
                      f"{dec['analytics_storage']}") if dec else "shape not recognised"
            a(f"  gcs={g}  ({detail})  on {n} hit(s)")
        if s["consent"]["consent_update_hits"]:
            a(f"  consent_update hits: {s['consent']['consent_update_hits']} "
              f"-- consent changed mid-capture")
        if s["consent"]["npa_flag_seen"]:
            a(f"  npa (non-personalised ads) values seen: "
              f"{', '.join(s['consent']['npa_flag_seen'])}")
        a("  gcd (consent default string) is reported raw in --json; its encoding is")
        a("  undocumented and changes, so it is not decoded here.")

    if s["user_data"]:
        a("\n## Identity and matching signals")
        a("   (presence only -- the values are a visitor's data and are not printed)")
        for u in s["user_data"]:
            a(f"  {u['hits']:>3} hit(s)  {u['signal']}")

    if s["datalayer"]:
        a(f"\n## dataLayer ({len(s['datalayer'])} pushes, in order)")
        for d in s["datalayer"]:
            if not (d["event"] or d["value_state"] != "absent" or d["products"]
                    or d["consent"]):
                continue
            val = (f"   value={_fmt_value(d['value'], d['currency'], d['value_state'])}"
                   f" [{d['value_source']}]" if d["value_state"] != "absent" else "")
            a(f"  [{d['index']:>3}] {d['event'] or '(no event key)'}{val}")
            if d["consent"]:
                a(f"        consent: {json.dumps(d['consent'], default=str)[:160]}")
            for prod in d["products"][:4]:
                num, field = product_value(prod)
                name = prod.get("name") or prod.get("item_name") or prod.get("id") or "?"
                a(f"        product: {str(name)[:52]:<52} "
                  f"{(str(num) + ' (' + str(field) + ')') if num is not None else 'no price'}")
            if len(d["products"]) > 4:
                a(f"        ... {len(d['products']) - 4} more products (use --json)")
            for n in d["notes"][:2]:
                a(f"        note: {n}")
            if d["person_data_keys"]:
                a(f"        person-data keys present (values withheld): "
                  f"{', '.join(d['person_data_keys'][:6])}")

    if s["loaders"]:
        a("\n## Tag libraries loaded (these prove presence, not that anything fired)")
        for l in s["loaders"]:
            a(f"  {l['what']}{'  ' + l['id'] if l.get('id') else ''}")

    if s["unrecognised"]:
        a(f"\n## Not recognised as measurement endpoints ({len(s['unrecognised'])})")
        a("   (listed so a vendor this parser does not know does not vanish silently)")
        for u in s["unrecognised"][:20]:
            a(f"  {u.get('entry', '')[:150]}")
        if len(s["unrecognised"]) > 20:
            a(f"  ... {len(s['unrecognised']) - 20} more (use --json)")

    if s["notes"]:
        a("\n## Capture notes")
        for n in s["notes"]:
            a(f"  - {n}")

    a("\n## What this capture does and does not show")
    a("  Shows: that these endpoints received these events, with these values, on one")
    a("         visit from this browser, geo and consent state.")
    a("  Does not show: what fires for other visitors, in other markets, or after a")
    a("         conversion you did not perform (and must not); whether the receiving ad")
    a("         account still spends; or anything about budget, bids or performance.")
    return "\n".join(L)


def main():
    # An explicit --help is a request that succeeded, so it exits 0. Running with
    # no arguments at all is a usage error and still exits 1.
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    argv = sys.argv[1:]
    if not argv or "--help" in argv or "-h" in argv:
        print(__doc__)
        return 1
    files, dl_files = [], []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--datalayer", "--dl"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                dl_files.append(argv[i + 1])
                i += 1
            else:
                print(f"{arg} needs a file path", file=sys.stderr)
                return 2
        elif not arg.startswith("--"):
            files.append(arg)
        i += 1

    requests, pushes, notes = [], [], []
    for path in files:
        r, p, n = load_capture(path)
        requests += r
        pushes += p
        notes += n
        notes.append(f"{path}: {len(r)} request(s), {len(p)} dataLayer push(es)")
    for path in dl_files:
        r, p, n = load_capture(path)
        # An explicit --datalayer file is a dataLayer file even if the sniffing
        # heuristic disagreed about some of its entries.
        pushes += p + [x for x in r if isinstance(x, dict)]
        notes += n
        notes.append(f"{path} (as dataLayer): {len(p) + len(r)} push(es)")

    try:
        s = analyse(requests, pushes, notes)
    except Exception as e:
        print(f"could not analyse capture: {e}", file=sys.stderr)
        return 2
    print(json.dumps(s, indent=2, default=str) if "--json" in argv else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
