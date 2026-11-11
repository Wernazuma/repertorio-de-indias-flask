"""Refresh the coordinates / geometries of every published dataset on GitHub.

Each dataset a user downloaded and *released* lives permanently in the ARCA-data
repo as ``datasets/<prefix>/<stamp>.zip`` (bundle) + ``<stamp>.json`` (meta). The
bundle carries the per-row matching decisions:

  * places domain      -> ``<prefix>_processing.csv``  (gz_id + manual coords)
  * territories domain  -> ``<prefix>_territory_results.csv``  (entidad_id / nivel /
                           start / end)

The *coordinates and footprints* those decisions resolve to are not frozen: the
place point comes from the reference gazetteer (``data/gz_info_1.csv`` /
``gz_entidades.csv``, time-sliced) and the territory polygon from the live
ArcGIS service (correct Entidad_ID, chronology-overlapping slice, correct Nivel).
When the gazetteer is rebuilt (HGIS "Recreate hierarchies") or a footprint is
corrected, the published bundles go stale.

This module re-derives every published dataset against the *current* gazetteer
and footprint service, reusing the exact same builders the live export uses
(:func:`matching.routes._build_export` / ``_build_territory_export``), and
commits a fresh, stamped bundle version back to ``datasets/<prefix>/`` — the
match decisions are untouched, only the coordinates/geometry are updated. The
original versions are left in place (append-only history).

It needs no arcpy: place coords come from the ``data/`` CSVs and territory
geometry from the ArcGIS feature service over HTTP, so it runs anywhere the
Flask app runs. Run it with the working directory set to ``apps_as_factory``
(so ``data/...`` and ``from app import create_app`` resolve):

    py -3.9 -m matching.refresh                 # dry run: report what would change
    py -3.9 -m matching.refresh --commit        # actually commit refreshed bundles
    py -3.9 -m matching.refresh --only <prefix>  # one dataset
    py -3.9 -m matching.refresh --limit 5        # cap how many datasets are touched

Fails soft per dataset: one dataset erroring never stops the rest, and nothing
is committed in a dry run.
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

import pandas as pd


# --- columns the export builders ADD on top of the user's own table. Stripping
#     exactly these from a published results CSV recovers the original base table
#     that the builder merged onto, so we can re-run the merge with fresh coords.
_PLACES_NON_COORD = ["gz_id", "cert", "iglesia_cat", "categoria", "categoria_esp"]
_TERR_NON_COORD = ["entidad_id", "nivel", "tipo", "start", "end"]
# The coordinate pair has been named differently over time; detect which one a
# bundle actually used (so a user's OWN lat/lon column is never mistaken for it).
_COORD_PAIRS = [("lat_y", "lon_x"), ("matched_lat", "matched_lon"), ("lat", "lon")]


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _detect_added_coords(cols):
    """Return the (lat, lon) column pair the bundle used for the MATCHED
    coordinates, or None. Priority order matters: an old bundle names them
    lat_y/lon_x even if the user's table also has lat/lon, so we must not strip
    the user's own columns."""
    cset = set(cols)
    for lat, lon in _COORD_PAIRS:
        if lat in cset and lon in cset:
            return (lat, lon)
    return None


def _added_columns(cols, domain):
    """Every column the builder added to this results CSV (to strip for the
    reconstructed base)."""
    base = list(_TERR_NON_COORD if domain == "territories" else _PLACES_NON_COORD)
    pair = _detect_added_coords(cols)
    added = [c for c in base if c in set(cols)]
    if pair:
        added += [pair[0], pair[1]]
    return added


def _reconstruct_base(results_df, domain):
    """Drop the builder-added columns from a published results CSV, recovering the
    user's original (formatted or original) base table the builder merged onto."""
    drop = _added_columns(results_df.columns, domain)
    return results_df.drop(columns=drop, errors="ignore")


def _domain_of(names):
    """places | territories | None, from the files present in a bundle."""
    if any(n.endswith("_territory_results.csv") for n in names):
        return "territories"
    if any(n.endswith("_processing.csv") for n in names):
        return "places"
    return None


def _read_csv(path):
    return pd.read_csv(path, sep=";", encoding="utf-8-sig")


def _round_coords(obj, ndigits=6):
    """Recursively round every float in a GeoJSON coordinates structure, so two
    geometries that differ only below the significant digits compare equal."""
    if isinstance(obj, (list, tuple)):
        return [_round_coords(x, ndigits) for x in obj]
    if isinstance(obj, float):
        return round(obj, ndigits)
    return obj


def _geom_index(zip_bytes, prefix):
    """Map each feature in the bundle's GeoJSON to a stable, rounded signature of
    its geometry, keyed by row identity. Returns {} when there is no GeoJSON.
    Used to tell whether a refresh actually moved any coordinate/footprint."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            name = next((n for n in z.namelist()
                         if os.path.basename(n).endswith(".geojson")), None)
            if not name:
                return {}
            data = json.load(z.open(name))
    except (zipfile.BadZipFile, OSError, ValueError, KeyError):
        return {}
    out = {}
    for i, feat in enumerate(data.get("features", []) or []):
        props = feat.get("properties") or {}
        key = str(props.get("rowID") or props.get("ID")
                  or props.get("Entidad_ID") or i)
        geom = (feat.get("geometry") or {}).get("coordinates")
        out[key] = json.dumps(_round_coords(geom), sort_keys=True)
    return out


def _geometry_diff(old_zip, new_zip, prefix):
    """(changed, total) features whose geometry differs between the two bundles'
    GeoJSONs (counting additions/removals). (None, None) if neither has one."""
    old_i = _geom_index(old_zip, prefix)
    new_i = _geom_index(new_zip, prefix)
    if not old_i and not new_i:
        return (None, None)
    keys = set(old_i) | set(new_i)
    changed = sum(1 for k in keys if old_i.get(k) != new_i.get(k))
    return (changed, len(keys))


# ---------------------------------------------------------------------------

def _latest_stamp_zip(repo, token, prefix, data_sink, github_sink):
    """The newest <stamp>.zip (and its meta dict) under datasets/<prefix>/, or
    (None, None, None)."""
    entries = github_sink.list_dir("%s/%s" % (data_sink.DATASETS_DIR, prefix), repo, token)
    zips = sorted(e["name"] for e in entries
                  if e.get("type") == "file" and str(e.get("name", "")).endswith(".zip"))
    if not zips:
        return (None, None, None)
    stamp = zips[-1][:-4]  # strip ".zip"
    zpath = "%s/%s/%s.zip" % (data_sink.DATASETS_DIR, prefix, stamp)
    jpath = "%s/%s/%s.json" % (data_sink.DATASETS_DIR, prefix, stamp)
    raw, _sha = github_sink.get_file(zpath, repo, token)
    if raw is None:
        return (None, None, None)
    meta = {}
    mraw, _msha = github_sink.get_file(jpath, repo, token)
    if mraw:
        import json
        try:
            meta = json.loads(mraw.decode("utf-8"))
        except ValueError:
            meta = {}
    return (stamp, raw, meta)


def iter_latest_datasets(repo, token, data_sink, github_sink, only=None):
    """Yield (prefix, stamp, zip_bytes, meta) for the newest bundle of every
    published dataset (or just ``only``)."""
    prefixes = [only] if only else sorted(
        e["name"] for e in github_sink.list_dir(data_sink.DATASETS_DIR, repo, token)
        if e.get("type") == "dir")
    for prefix in prefixes:
        if not data_sink.valid_prefix(prefix):
            continue
        stamp, raw, meta = _latest_stamp_zip(repo, token, prefix, data_sink, github_sink)
        if raw is not None:
            yield (prefix, stamp, raw, meta)


# ---------------------------------------------------------------------------

def refresh_one(prefix, old_zip, routes, workroot, log=print):
    """Re-derive one dataset's coordinates/geometry. Returns a dict:
    {prefix, domain, ok, changed, total, new_zip, error}. ``new_zip`` is the
    rebuilt bundle bytes (None on error / unknown domain)."""
    out = {"prefix": prefix, "domain": None, "ok": False,
           "changed": None, "total": None, "new_zip": None, "error": None}
    work = os.path.join(workroot, prefix)
    os.makedirs(work, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(old_zip)) as z:
            names = [os.path.basename(n) for n in z.namelist() if os.path.basename(n)]
            for n in z.namelist():
                base = os.path.basename(n)
                if base:
                    with z.open(n) as src, open(os.path.join(work, base), "wb") as fh:
                        fh.write(src.read())
    except (zipfile.BadZipFile, OSError) as e:
        out["error"] = "unreadable bundle: %s" % e
        return out

    domain = _domain_of(names)
    out["domain"] = domain
    if domain is None:
        out["error"] = "no _processing.csv or _territory_results.csv in bundle"
        return out

    old_results = os.path.join(work, "%s_results.csv" % prefix)

    # Reconstruct the base tables (user's formatted + original) that the builder
    # merged onto, by stripping the columns a previous export added.
    for added_name, base_name in (("%s_results.csv" % prefix, "%s_formatted.csv" % prefix),
                                  ("%s_original_results.csv" % prefix, "%s.csv" % prefix)):
        src = os.path.join(work, added_name)
        if os.path.exists(src):
            base = _reconstruct_base(_read_csv(src), domain)
            base.to_csv(os.path.join(work, base_name), sep=";", index=False, encoding="utf-8")

    # Which optional place fields were in the old bundle -> keep them.
    opt_fields = []
    if domain == "places" and os.path.exists(old_results):
        have = set(_read_csv(old_results).columns)
        opt_fields = [f for f in routes.OPTIONAL_EXPORT_FIELDS if f in have]

    # Point the builder's working dir at this dataset's temp folder and run the
    # SAME code the live export uses, so derivation stays single-sourced.
    saved_upload = routes.UPLOAD_FOLDER
    routes.UPLOAD_FOLDER = work
    try:
        if domain == "territories":
            new_zip, _stats = routes._build_territory_export(prefix)
        else:
            new_zip, _stats = routes._build_export(prefix, opt_fields)
    except Exception as e:  # one dataset's failure must not sink the batch
        out["error"] = "rebuild failed: %s: %s" % (type(e).__name__, e)
        return out
    finally:
        routes.UPLOAD_FOLDER = saved_upload

    # Did any coordinate / footprint actually move? Compare the geometry in the
    # old vs the freshly rebuilt bundle's GeoJSON (works for both points and
    # polygons, and for bundles that have no results CSV).
    changed, total = _geometry_diff(old_zip, new_zip, prefix)
    out["changed"], out["total"] = changed, total

    out["new_zip"] = new_zip
    out["ok"] = True
    return out


def run(commit=False, only=None, limit=None, log=print):
    """Walk every published dataset, re-derive its coordinates/geometry, and
    (when commit=True) publish a refreshed bundle version. Returns a summary
    dict. Dry run by default."""
    # Local imports: these pull in the Flask app, so keep module import cheap.
    from app import create_app
    from enciclopedia import data_sink, github_sink
    from matching import routes

    app = create_app()
    summary = {"datasets": 0, "refreshed": 0, "committed": 0, "unchanged": 0,
               "errors": [], "details": [], "configured": False, "dry_run": not commit}

    with app.app_context():
        if not data_sink.configured():
            log("ARCA-data repo is not configured (GITHUB_DATA_REPO / GITHUB_DATA_TOKEN). "
                "Nothing to do.")
            return summary
        summary["configured"] = True
        repo, token = data_sink._repo(), data_sink._token()
        workroot = tempfile.mkdtemp(prefix="arca_refresh_")
        log("Refreshing published datasets in %s  (%s)"
            % (repo, "COMMIT" if commit else "dry run"))
        try:
            n = 0
            for prefix, stamp, zip_bytes, meta in iter_latest_datasets(
                    repo, token, data_sink, github_sink, only=only):
                if limit is not None and n >= limit:
                    log("Reached --limit %d; stopping." % limit)
                    break
                n += 1
                summary["datasets"] += 1
                res = refresh_one(prefix, zip_bytes, routes, workroot, log=log)
                tag = res["domain"] or "?"
                if not res["ok"]:
                    summary["errors"].append("%s: %s" % (prefix, res["error"]))
                    log("  x %-30s [%s] %s" % (prefix, tag, res["error"]))
                    continue
                ch, tot = res["changed"], res["total"]
                diff_txt = ("%s/%s feature(s) changed" % (ch, tot)) if ch is not None \
                    else "rebuilt (no geometry to compare)"
                summary["details"].append({"prefix": prefix, "domain": res["domain"],
                                           "from_stamp": stamp, "changed": ch, "total": tot})
                if ch == 0:
                    summary["unchanged"] += 1
                    log("  = %-30s [%s] no coordinate change" % (prefix, tag))
                    # Nothing changed -> don't write a redundant new version.
                    continue
                summary["refreshed"] += 1
                log("  ~ %-30s [%s] %s" % (prefix, tag, diff_txt))
                if commit:
                    new_meta = dict(meta or {})
                    new_meta.update({
                        "prefix": prefix,
                        "refresh": True,
                        "refreshed_from": stamp,
                        "refreshed_at": _now_iso(),
                        "refresh_note": "Coordinates/geometry re-derived from the "
                                        "current gazetteer & footprint service; "
                                        "match decisions unchanged.",
                    })
                    url = data_sink.publish_dataset(prefix, res["new_zip"], new_meta)
                    if url:
                        summary["committed"] += 1
                        log("      -> committed new version")
                    else:
                        summary["errors"].append("%s: commit failed" % prefix)
                        log("      x commit failed")
        finally:
            shutil.rmtree(workroot, ignore_errors=True)

    log("")
    log("Done. %d dataset(s) seen, %d with coordinate changes, %d committed, "
        "%d unchanged, %d error(s)."
        % (summary["datasets"], summary["refreshed"], summary["committed"],
           summary["unchanged"], len(summary["errors"])))
    if not commit and summary["refreshed"]:
        log("Dry run - re-run with --commit to publish the %d refreshed bundle(s)."
            % summary["refreshed"])
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refresh coordinates/geometries of "
                                             "published ARCA datasets on GitHub.")
    ap.add_argument("--commit", action="store_true",
                    help="Actually publish refreshed bundles (default: dry run).")
    ap.add_argument("--only", metavar="PREFIX", default=None,
                    help="Refresh only this dataset prefix.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most this many datasets.")
    args = ap.parse_args(argv)
    summary = run(commit=args.commit, only=args.only, limit=args.limit, log=print)
    # Non-zero exit if any dataset errored, so a scheduler can alert.
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
