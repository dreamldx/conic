import argparse
import difflib
import json
import os
import re
import sys
from collections import Counter

CATALOG_ENV = "CONIC_MODEL_CATALOG"
CATALOG_CANDIDATES = ["data/openrouter_models.json", "conic/data/openrouter_models.json"]
TAG_SUFFIXES = ("batch", "free")
SORT_KEYS = {
    "price_in": "input price per 1M tokens (cheapest first)",
    "price_out": "output price per 1M tokens (cheapest first)",
    "context": "context window (largest first)",
    "params": "number of supported request parameters (most first)",
    "modalities": "number of input plus output modalities (most first)",
    "name": "slug alphabetical",
    "vendor": "vendor alphabetical, then slug",
    "match": "fuzzy match score (best first, needs --query)",
}
DEFAULT_ORDER_DESC = {"context", "params", "modalities", "match"}


def find_catalog_path(explicit):
    candidates = [explicit] if explicit else []
    if os.environ.get(CATALOG_ENV):
        candidates.append(os.environ[CATALOG_ENV])
    candidates.extend(CATALOG_CANDIDATES)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    sys.exit(f"model catalog not found, tried: {', '.join(c for c in candidates if c)}; use --catalog or set {CATALOG_ENV}")


def load_catalog(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def model_name(slug):
    return re.sub(r"^~?[^/]*/", "", slug)


def is_alias(entry):
    return entry["slug"].startswith("~")


def is_variant(entry):
    return ":" in entry["slug"]


def per_million(value):
    if value is None:
        return 0.0
    return None if value < 0 else value * 1e6


def price_in(entry):
    return per_million(entry.get("pricing_prompt"))


def price_out(entry):
    return per_million(entry.get("pricing_completion"))


def format_price(value):
    return "variable" if value is None else f"${value:.4g}/M"


def tokens(text):
    return [t for t in re.split(r"[^a-z0-9.]+", text.lower()) if t]


def match_score(query, entry):
    q = query.strip().lower()
    if not q:
        return 0.0
    slug = entry["slug"].lower()
    name = model_name(slug)
    display = (entry.get("name") or "").lower()
    if q in (slug, name):
        return 100.0
    best = 0.0
    q_tokens = tokens(q)
    for target in (name, slug, display):
        if not target:
            continue
        if target.startswith(q):
            best = max(best, 85.0)
        elif q in target:
            best = max(best, 70.0)
        t_tokens = set(tokens(target))
        if q_tokens and all(any(qt == tt or tt.startswith(qt) for tt in t_tokens) for qt in q_tokens):
            best = max(best, 65.0 + 5.0 * len(q_tokens) / max(len(t_tokens), 1))
        best = max(best, 50.0 * difflib.SequenceMatcher(None, q, target).ratio())
    return best


def capability_counts(entry):
    modalities = len(entry.get("input_modalities") or []) + len(entry.get("output_modalities") or [])
    return len(entry.get("supported_parameters") or []), modalities


def sort_value(key, entry, score):
    params, modalities = capability_counts(entry)
    return {
        "price_in": price_in(entry),
        "price_out": price_out(entry),
        "context": entry.get("context_length") or 0,
        "params": params,
        "modalities": modalities,
        "name": entry["slug"],
        "vendor": (entry.get("vendor") or "", entry["slug"]),
        "match": score,
    }[key]


def passes_filters(entry, args):
    slug = entry["slug"]
    if args.vendor and (entry.get("vendor") or "").lower() not in {v.lower() for v in args.vendor}:
        return False
    if args.no_alias and is_alias(entry):
        return False
    if args.no_variants and is_variant(entry):
        return False
    is_free = price_in(entry) == 0 and price_out(entry) == 0
    is_paid = (price_in(entry) or 0) > 0 or (price_out(entry) or 0) > 0
    if args.free and not is_free:
        return False
    if args.paid and not is_paid:
        return False
    if args.tools and not entry.get("supports_tools"):
        return False
    if args.min_context and (entry.get("context_length") or 0) < args.min_context:
        return False
    if args.max_price_in is not None and (price_in(entry) is None or price_in(entry) > args.max_price_in):
        return False
    if args.max_price_out is not None and (price_out(entry) is None or price_out(entry) > args.max_price_out):
        return False
    for modality in args.input_modality or []:
        if modality not in (entry.get("input_modalities") or []):
            return False
    for modality in args.output_modality or []:
        if modality not in (entry.get("output_modalities") or []):
            return False
    for param in args.param or []:
        if param not in (entry.get("supported_parameters") or []):
            return False
    return not (args.exclude and any(x.lower() in slug.lower() for x in args.exclude))


def format_row(entry, score=None):
    tools = "tools" if entry.get("supports_tools") else "-"
    ctx = entry.get("context_length") or 0
    real = f" -> {entry['real_model']}" if is_alias(entry) else ""
    match = f"  match={score:5.1f}" if score is not None else ""
    return (
        f"{entry['slug']}{real}\n"
        f"    in {format_price(price_in(entry))}  out {format_price(price_out(entry))}  ctx {ctx:,}  {tools}"
        f"  in:{','.join(entry.get('input_modalities') or [])} out:{','.join(entry.get('output_modalities') or [])}{match}"
    )


def command_find(args, catalog):
    scored = [(match_score(args.query, e) if args.query else 0.0, e) for e in catalog]
    if args.query:
        scored = [(s, e) for s, e in scored if s >= args.min_score]
    rows = [(s, e) for s, e in scored if passes_filters(e, args)]
    sort_key = args.sort or ("match" if args.query else "name")
    if sort_key == "match" and not args.query:
        sys.exit("--sort match needs --query")
    descending = (sort_key in DEFAULT_ORDER_DESC) != bool(args.reverse)
    if sort_key in ("price_in", "price_out"):
        unknown = [r for r in rows if sort_value(sort_key, r[1], r[0]) is None]
        rows = [r for r in rows if sort_value(sort_key, r[1], r[0]) is not None]
    else:
        unknown = []
    rows.sort(key=lambda r: sort_value(sort_key, r[1], r[0]), reverse=descending)
    rows += unknown
    total = len(rows)
    rows = rows[: args.limit]
    if args.json:
        print(json.dumps([dict(e, match_score=round(s, 1)) for s, e in rows], ensure_ascii=False, indent=2))
        return
    print(f"{total} models match, showing {len(rows)} (sorted by {sort_key}{' desc' if descending else ' asc'})")
    for score, entry in rows:
        print(format_row(entry, score if args.query else None))


def command_options(args, catalog):
    counter = lambda values: Counter(v for vs in values for v in vs)
    vendors = Counter((e.get("vendor") or "") for e in catalog)
    inputs = counter(e.get("input_modalities") or [] for e in catalog)
    outputs = counter(e.get("output_modalities") or [] for e in catalog)
    params = counter(e.get("supported_parameters") or [] for e in catalog)
    contexts = [e.get("context_length") or 0 for e in catalog]
    priced_in = [price_in(e) for e in catalog if (price_in(e) or 0) > 0]
    priced_out = [price_out(e) for e in catalog if (price_out(e) or 0) > 0]
    data = {
        "models": len(catalog),
        "aliases": sum(is_alias(e) for e in catalog),
        "variants (:batch/:free)": sum(is_variant(e) for e in catalog),
        "vendors": dict(vendors.most_common()),
        "input modalities": dict(inputs.most_common()),
        "output modalities": dict(outputs.most_common()),
        "supported parameters": dict(params.most_common()),
        "sort keys": SORT_KEYS,
        "context length": {"min": min(contexts), "max": max(contexts)},
        "input price per 1M (paid models)": {"min": min(priced_in), "max": max(priced_in)},
        "output price per 1M (paid models)": {"min": min(priced_out), "max": max(priced_out)},
        "free models": sum(1 for e in catalog if price_in(e) == 0 and price_out(e) == 0),
        "variable-price models (router)": sum(1 for e in catalog if price_in(e) is None or price_out(e) is None),
        "tool-capable models": sum(bool(e.get("supports_tools")) for e in catalog),
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    for key, value in data.items():
        if isinstance(value, dict):
            print(f"{key}:")
            for k, v in value.items():
                print(f"  {k}: {v}")
        else:
            print(f"{key}: {value}")


def command_show(args, catalog):
    query = args.query.strip().lower()
    exact = [e for e in catalog if query in (e["slug"].lower(), model_name(e["slug"]).lower())]
    matches = exact or [e for _, e in sorted(((match_score(args.query, e), e) for e in catalog), key=lambda r: -r[0])[:3]]
    for entry in matches:
        print(json.dumps(entry, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description="Search the synced OpenRouter model catalog")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog", help=f"path to openrouter_models.json (or set {CATALOG_ENV})")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    find = sub.add_parser("find", parents=[common], help="fuzzy search, filter and sort models")
    find.add_argument("query", nargs="?", default="", help="fuzzy text: slug, model name or display name")
    find.add_argument("--min-score", type=float, default=30.0, help="fuzzy match threshold 0-100 (default 30)")
    find.add_argument("--vendor", action="append", help="vendor, repeatable, e.g. --vendor openai --vendor anthropic")
    find.add_argument("--tools", action="store_true", help="only models that support tool calling")
    find.add_argument("--input-modality", action="append", help="required input modality, repeatable")
    find.add_argument("--output-modality", action="append", help="required output modality, repeatable")
    find.add_argument("--param", action="append", help="required supported parameter, repeatable, e.g. reasoning")
    find.add_argument("--min-context", type=int, help="minimum context length in tokens")
    find.add_argument("--max-price-in", type=float, help="maximum input price, USD per 1M tokens")
    find.add_argument("--max-price-out", type=float, help="maximum output price, USD per 1M tokens")
    find.add_argument("--free", action="store_true", help="only free models")
    find.add_argument("--paid", action="store_true", help="only paid models")
    find.add_argument("--no-alias", action="store_true", help="hide ~vendor/name-latest aliases")
    find.add_argument("--no-variants", action="store_true", help="hide :batch and :free variants")
    find.add_argument("--exclude", action="append", help="drop slugs containing this text, repeatable")
    find.add_argument("--sort", choices=list(SORT_KEYS), help="sort key (default: match with a query, else name)")
    find.add_argument("--reverse", action="store_true", help="reverse the default sort direction")
    find.add_argument("--limit", type=int, default=15, help="rows to show (default 15)")
    find.set_defaults(func=command_find)

    options = sub.add_parser("options", parents=[common], help="list every value the options accept, computed from the catalog")
    options.set_defaults(func=command_options)

    show = sub.add_parser("show", parents=[common], help="print the full catalog record for a model")
    show.add_argument("query", help="slug or model name")
    show.set_defaults(func=command_show)
    return parser


def main():
    args = build_parser().parse_args()
    catalog = load_catalog(find_catalog_path(args.catalog))
    args.func(args, catalog)


if __name__ == "__main__":
    main()
