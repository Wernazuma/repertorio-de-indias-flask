import os
import io
import glob
import json
import shutil
import zipfile
import threading
import pandas as pd
from rapidfuzz import fuzz
from flask import render_template, request, redirect, url_for, flash, jsonify, send_file
from . import bp
from .main import match_phase_1, match_phase_1b, run_pipeline, read_status
from .cleaning import clean_toponym
from .saints import load_saints, extract_saint, compare_saints
from .filters import ensure_dtype_consistency
from .regions import region_code_to_name

UPLOAD_FOLDER = os.path.join("data", "uploads")
GAZETTEER_FILE = os.path.join("data", "reference_gazetteer.csv")
ENTIDADES_FILE = os.path.join("data", "gz_entidades.csv")
GEO_DISTRICTS_FILE = os.path.join("data", "geo", "source", "genericos.geojson")

# Parsed district polygons, cached in memory (trimmed to the props we use).
_DISTRICTS_GJ = None


def _load_districts():
    global _DISTRICTS_GJ
    if _DISTRICTS_GJ is None:
        with open(GEO_DISTRICTS_FILE, "r", encoding="utf-8") as f:
            gj = json.load(f)
        for feat in gj.get("features", []):
            p = feat.get("properties", {}) or {}
            feat["properties"] = {
                "Partido": p.get("Partido"),
                "Provincia": p.get("Provincia"),
                "Region": p.get("Region"),
            }
        _DISTRICTS_GJ = gj
    return _DISTRICTS_GJ


def _identified_places(prefix):
    """Already auto-matched / adopted places (with coords) for map context."""
    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(processing_path):
        return []
    df = pd.read_csv(processing_path, sep=";", encoding="utf-8")
    df["gz_id"] = pd.to_numeric(df["gz_id"], errors="coerce").astype("Int64")
    sub = df[df["phase_1a_outcome"].isin(["auto_adopt", "adopted_candidate"])]
    if sub.empty:
        return []
    gaz = pd.read_csv(GAZETTEER_FILE, sep=";", encoding="utf-8")
    gaz["gz_id"] = pd.to_numeric(gaz["gz_id"], errors="coerce").astype("Int64")
    gaz = gaz.drop_duplicates(subset=["gz_id"]).set_index("gz_id")

    out, seen = [], set()
    for _, r in sub.iterrows():
        gz = r.get("gz_id")
        if pd.isna(gz) or int(gz) in seen or int(gz) not in gaz.index:
            continue
        grow = gaz.loc[int(gz)]
        lat = _to_coord(grow.get("lat"))
        lon = _to_coord(grow.get("lon"))
        if lat is None or lon is None:
            continue
        seen.add(int(gz))
        out.append({
            "label": str(r.get("ref_Label", "") or ""),
            "lugar_label": str(grow.get("lugar_label", "") or ""),
            "lat": lat,
            "lon": lon,
        })
    return out


# Human-readable label for each pipeline stage, in order.
_STAGE_LABELS = {
    "start_matching": "Matching not started yet",
    "progress": "Matching in progress",
    "disambiguation": "Disambiguation of candidates",
    "constraints": "Phase 3 — global constraints",
    "territory": "Phase 3 — territory resolution",
    "export": "Ready to finalize & export",
    "territory_match": "Territory matching",
}


def _get_domain(prefix):
    """'places' (default) or 'territories', persisted by the transform step."""
    try:
        with open(os.path.join(UPLOAD_FOLDER, f"{prefix}_domain.txt"), encoding="utf-8") as f:
            d = f.read().strip()
        return d if d in ("places", "territories") else "places"
    except OSError:
        return "places"


def _pipeline_stage(prefix):
    """Detect where a prefix's workflow currently stands. Returns (stage, endpoint,
    kwargs). stage 'transform' means it hasn't been cleaned/transformed yet."""
    up = UPLOAD_FOLDER
    if not os.path.exists(os.path.join(up, f"{prefix}_cleaned.csv")):
        return ("transform", None, {})

    # Territories use a separate gazetteer (Niveles_matching) and workflow.
    if _get_domain(prefix) == "territories":
        return ("territory_match", "matching.territory_match", {"prefix": prefix})

    if not os.path.exists(os.path.join(up, f"{prefix}_phase1a_complete.flag")):
        return ("start_matching", "matching.run_matching_route", {"prefix": prefix})
    if not os.path.exists(os.path.join(up, f"{prefix}_phase1b_complete.flag")):
        return ("progress", "matching.match_status", {"prefix": prefix})

    outcomes = set()
    try:
        pdf = pd.read_csv(os.path.join(up, f"{prefix}_processing.csv"), sep=";", encoding="utf-8")
        outcomes = set(pdf["phase_1a_outcome"].dropna().astype(str).unique())
    except Exception:
        pass

    if "candidate" in outcomes:
        return ("disambiguation", "matching.disambiguate", {"prefix": prefix})
    if not os.path.exists(os.path.join(up, f"{prefix}_constraints.json")):
        return ("constraints", "matching.set_constraints", {"prefix": prefix})
    if "relegated" in outcomes:
        return ("territory", "matching.territory_choice", {"prefix": prefix})
    return ("export", "matching.export_results", {"prefix": prefix})


@bp.route("/", methods=["GET", "POST"])
def index():
    prefix = request.values.get("prefix", "").strip()
    stage = endpoint = None
    kwargs = {}
    if prefix:
        if not os.path.exists(UPLOAD_FOLDER):
            os.makedirs(UPLOAD_FOLDER)
        stage, endpoint, kwargs = _pipeline_stage(prefix)
        if stage == "transform":
            flash(f"No cleaned table found for “{prefix}”. Transform/upload it first.")
            prefix = ""

    continue_url = url_for(endpoint, **kwargs) if endpoint else None
    return render_template(
        "upload_form.html",
        prefix=prefix,
        stage=stage,
        stage_label=_STAGE_LABELS.get(stage),
        continue_url=continue_url,
    )


@bp.route("/resume")
def resume():
    """Jump straight to wherever a prefix's workflow left off."""
    prefix = request.args.get("prefix", "").strip()
    if not prefix:
        return redirect(url_for("matching.index"))
    stage, endpoint, kwargs = _pipeline_stage(prefix)
    if stage == "transform":
        flash(f"No cleaned table found for “{prefix}”. Transform/upload it first.")
        return redirect(url_for("matching.index"))
    return redirect(url_for(endpoint, **kwargs))


def _clear_run_artifacts(prefix):
    """Remove status/flags from a previous run so a fresh run starts clean."""
    for suffix in ("_status.json", "_status.json.tmp", "_status.txt",
                   "_phase1a_complete.flag", "_phase1b_complete.flag"):
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, f"{prefix}{suffix}"))
        except OSError:
            pass


@bp.route("/match_phase_1", methods=["GET"])
def run_matching_route():
    """Start the full matching pipeline (Phase 1a + 1b) in the background and
    send the user straight to the live progress page."""
    prefix = request.args.get("prefix")
    if not prefix:
        flash("Missing file prefix.")
        return redirect(url_for("matching.index"))

    cleaned = os.path.join(UPLOAD_FOLDER, f"{prefix}_cleaned.csv")
    if not os.path.exists(cleaned):
        flash(f"File not found: {cleaned}")
        return redirect(url_for("matching.index"))

    _clear_run_artifacts(prefix)
    thread = threading.Thread(target=run_pipeline, args=(prefix,), daemon=True)
    thread.start()

    return redirect(url_for("matching.match_status", prefix=prefix))


@bp.route("/match_status", methods=["GET"])
def match_status():
    prefix = request.args.get("prefix")
    if not prefix:
        return "Missing prefix", 400
    return render_template("match_status.html", prefix=prefix)


@bp.route("/match_progress/<prefix>", methods=["GET"])
def match_progress(prefix):
    """JSON status polled by the live progress page."""
    status = read_status(prefix) or {}
    p1a = os.path.exists(os.path.join(UPLOAD_FOLDER, f"{prefix}_phase1a_complete.flag"))
    p1b = os.path.exists(os.path.join(UPLOAD_FOLDER, f"{prefix}_phase1b_complete.flag"))

    error = status.get("error")
    stage = status.get("stage", "running")
    done = (stage == "done") or p1b

    if error:
        out_stage = "error"
    elif done:
        out_stage = "done"
    else:
        out_stage = "running"

    return jsonify({
        "phase": status.get("phase", "1a"),
        "processed": status.get("processed", 0),
        "total": status.get("total", 0),
        "percent": status.get("percent", 0),
        "stage": out_stage,
        "error": error,
        "phase1a_complete": p1a,
        "phase1b_complete": p1b,
        "redirect": url_for("matching.disambiguate", prefix=prefix) if (done and not error) else None,
    })


# ---------------------------------------------------------------------------
# Phase 1b: manual re-run (the normal flow runs it automatically after 1a)
# ---------------------------------------------------------------------------

@bp.route("/match_phase_1b", methods=["GET"])
def run_phase_1b_route():
    prefix = request.args.get("prefix")
    if not prefix:
        flash("Missing file prefix.")
        return redirect(url_for("matching.index"))
    try:
        _, n_fixed = match_phase_1b(prefix)
        flash(f"Phase 1b complete: {n_fixed} relegated rows recovered.")
        return redirect(url_for("matching.match_status", prefix=prefix))
    except Exception as e:
        flash(f"Error during Phase 1b: {e}")
        return redirect(url_for("matching.index"))


# ---------------------------------------------------------------------------
# Phase 2: Disambiguation
# ---------------------------------------------------------------------------

def _to_coord(val):
    """Parse a gazetteer lat/lon value (comma decimal) into a float, or None."""
    if val is None:
        return None
    try:
        if isinstance(val, float) and pd.isna(val):
            return None
    except TypeError:
        pass
    s = str(val).strip().replace(",", ".")
    if not s or s.lower() in ("nan", "none"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f == 0.0:
        return None
    return f


# Pretty labels for territory levels
_LEVEL_LABELS = {
    "partido": "Partido", "jurisdiccion": "Jurisdicción",
    "provincia_menor": "Provincia menor", "provincia": "Provincia",
    "provincia_mayor": "Provincia mayor", "obispado": "Obispado",
    "adm1": "Adm1", "audiencia": "Audiencia", "adm0": "Country",
    "adm0_iso": "Country (ISO)", "region": "Region",
}


def _submitted_context(prefix, row_id, span=1):
    """The submitted row plus its neighbours, for an at-a-glance sanity check.

    Returns {position, total, columns, rows:[{pos, is_current, cells}]} or None.
    """
    for suffix in ("_cleaned.csv", "_formatted.csv", "_processing.csv"):
        path = os.path.join(UPLOAD_FOLDER, f"{prefix}{suffix}")
        if not os.path.exists(path):
            continue
        try:
            t = pd.read_csv(path, sep=";", encoding="utf-8", dtype=str).reset_index(drop=True)
        except Exception:
            continue
        if "rowID" not in t.columns:
            continue
        # a rowID can span several processing rows (candidates) — the position in
        # the *submitted* table is the first occurrence.
        matches = t.index[t["rowID"].astype(str) == str(row_id)].tolist()
        if not matches:
            continue
        # de-duplicate to one line per submitted rowID for a clean neighbour view
        order = list(dict.fromkeys(t["rowID"].astype(str)))
        try:
            oi = order.index(str(row_id))
        except ValueError:
            oi = 0
        lo, hi = max(0, oi - span), min(len(order), oi + span + 1)
        drop = {"phase_1a_outcome", "gz_id", "lugar_label", "lugar_partido_generico",
                "lugar_provincia_generica", "manual_lat", "manual_lon", "manual_category"}
        cols = [c for c in t.columns if c not in drop]
        first = t.drop_duplicates(subset=["rowID"], keep="first").reset_index(drop=True)
        rows = []
        for i in range(lo, hi):
            rid = order[i]
            r = first[first["rowID"].astype(str) == rid]
            if r.empty:
                continue
            r = r.iloc[0]
            rows.append({
                "pos": i + 1,
                "is_current": rid == str(row_id),
                "cells": {c: ("" if pd.isna(r[c]) else str(r[c])) for c in cols},
            })
        return {"position": oi + 1, "total": len(order), "columns": cols, "rows": rows}
    return None


@bp.route("/disambiguate", methods=["GET", "POST"])
def disambiguate():
    prefix = request.args.get("prefix")
    if not prefix:
        flash("Missing file prefix.")
        return redirect(url_for("matching.index"))

    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(processing_path):
        flash(f"Missing processing file: {processing_path}")
        return redirect(url_for("matching.index"))

    df = pd.read_csv(processing_path, sep=";", encoding="utf-8")
    df["gz_id"] = pd.to_numeric(df["gz_id"], errors="coerce").astype("Int64")

    if request.method == "POST":
        try:
            rowID = int(request.form.get("row_id"))
        except (TypeError, ValueError):
            rowID = None
        selected_gz = request.form.get("choice", "")

        decided = rowID is not None and selected_gz and selected_gz != "skip"
        if decided:
            is_candidate = (df["rowID"] == rowID) & (df["phase_1a_outcome"] == "candidate")
            if selected_gz == "reject":
                # Rejecting all candidates does NOT discard the row — it goes back
                # to manual resolution in step 3. Collapse to one relegated
                # placeholder (clear gz_id) and drop the other candidate rows.
                cand_idx = df.index[is_candidate].tolist()
                if cand_idx:
                    df.loc[cand_idx[0], "phase_1a_outcome"] = "relegated"
                    df.loc[cand_idx[0], "gz_id"] = pd.NA
                    if len(cand_idx) > 1:
                        df = df.drop(index=cand_idx[1:])
            else:
                try:
                    gz_val = int(selected_gz)
                    df.loc[is_candidate & (df["gz_id"] == gz_val), "phase_1a_outcome"] = "adopted_candidate"
                    df.loc[is_candidate & (df["gz_id"] != gz_val), "phase_1a_outcome"] = "deleted_candidate"
                except ValueError:
                    pass
            df.to_csv(processing_path, sep=";", index=False, encoding="utf-8")

        try:
            idx = int(request.form.get("idx", 0))
        except (TypeError, ValueError):
            idx = 0
        # The case list is rebuilt from the live "candidate" rows on every GET.
        # A decision removes the current row from that list, so the next case
        # shifts INTO the current idx -> keep idx. Only "skip" leaves the row in
        # the list, so we must step past it.
        next_idx = idx if decided else idx + 1
        return redirect(url_for("matching.disambiguate", prefix=prefix, idx=next_idx))

    # GET: build case-by-case view with map data
    try:
        gazetteer_df = pd.read_csv(GAZETTEER_FILE, sep=";", encoding="utf-8")
        gazetteer_df["gz_id"] = pd.to_numeric(gazetteer_df["gz_id"], errors="coerce").astype("Int64")

        gz_cols = ["gz_id", "lugar_nombre", "lugar_variantes", "lugar_categoria",
                   "lugar_categoria_especial", "lugar_iglesia_cat", "lugar_region",
                   "lat", "lon"]
        gz_subset = gazetteer_df[[c for c in gz_cols if c in gazetteer_df.columns]].copy()
        merge_cols = ["gz_id"] + [c for c in gz_subset.columns if c != "gz_id" and c not in df.columns]
        merged = df.merge(gz_subset[merge_cols], on="gz_id", how="left")

        # Full gazetteer rows (with all territorial fields), keyed by gz_id, for
        # recomputing the finest comparable territory level per candidate.
        gz_full = gazetteer_df.drop_duplicates(subset=["gz_id"]).set_index("gz_id")

        from .disambiguation import disambiguate_candidates
        disambig_df = disambiguate_candidates(merged)

        if disambig_df.empty:
            return render_template(
                "disambiguate.html", prefix=prefix, has_candidates=False,
            )

        ordered_ids = disambig_df["rowID"].drop_duplicates().tolist()
        total = len(ordered_ids)
        try:
            idx = int(request.args.get("idx", 0))
        except ValueError:
            idx = 0
        if idx >= total:
            flash("Disambiguation complete.")
            return redirect(url_for("matching.set_constraints", prefix=prefix))
        idx = max(0, idx)

        current_row_id = ordered_ids[idx]
        group = disambig_df[disambig_df["rowID"] == current_row_id].copy()

        # Cross-row awareness: which gz_ids are already adopted for, or are still
        # candidates of, OTHER input rows — so the user isn't blind to a place
        # being claimed twice. Built from the live processing df.
        cross_adopted, cross_candidate = {}, {}
        for _, rr in df.iterrows():
            g = rr.get("gz_id")
            rid = rr.get("rowID")
            if pd.isna(g) or pd.isna(rid) or int(rid) == int(current_row_id):
                continue
            g = int(g)
            entry = {"rowID": int(rid), "label": str(rr.get("ref_Label", "") or "")}
            outcome = str(rr.get("phase_1a_outcome", ""))
            if outcome in ADOPTED_OUTCOMES:
                cross_adopted.setdefault(g, []).append(entry)
            elif outcome == "candidate":
                cross_candidate.setdefault(g, []).append(entry)

        # Reference (input) context columns
        ref_row = group.iloc[0]
        ref_info = {}
        for col in group.columns:
            if col.startswith("ref_") and col != "ref_Label":
                val = ref_row[col]
                if pd.notna(val) and str(val).strip():
                    ref_info[col] = str(val)
        ref_label = str(ref_row.get("ref_Label", ""))

        # Candidate dicts for display + map
        from .fast_match import territory_detail
        candidates = []
        for _, c in group.iterrows():
            lat = _to_coord(c.get("lat"))
            lon = _to_coord(c.get("lon"))

            # Finest comparable territory level vs the full input (reveals a
            # fine-level mismatch hidden behind a coarse 1b region match).
            terr_level_key, terr_status = "", ""
            gzid = c.get("gz_id")
            if pd.notna(gzid) and int(gzid) in gz_full.index:
                terr_level_key, terr_status = territory_detail(c, gz_full.loc[int(gzid)])

            candidates.append({
                "gz_id": int(c["gz_id"]) if pd.notna(c["gz_id"]) else None,
                "lugar_label": str(c.get("lugar_label", "") or ""),
                "lugar_nombre": str(c.get("lugar_nombre", "") or ""),
                "lugar_variantes": str(c.get("lugar_variantes", "") or ""),
                "lugar_categoria": str(c.get("lugar_categoria", "") or ""),
                "lugar_categoria_especial": str(c.get("lugar_categoria_especial", "") or ""),
                "lugar_iglesia_cat": str(c.get("lugar_iglesia_cat", "") or ""),
                "lugar_partido_generico": str(c.get("lugar_partido_generico", "") or ""),
                "lugar_provincia_generica": str(c.get("lugar_provincia_generica", "") or ""),
                "lugar_region": str(c.get("lugar_region", "") or ""),
                "toponym_match": str(c.get("toponym_match", "") or ""),
                "saint_match": str(c.get("saint_match", "") or ""),
                "category_match": str(c.get("category_match", "") or ""),
                "fuzzy_score": c.get("fuzzy_score"),
                "score": int(c.get("score", 0)),
                "terr_level": _LEVEL_LABELS.get(terr_level_key, terr_level_key),
                "terr_status": terr_status,
                "lat": lat,
                "lon": lon,
                "also_adopted_for": cross_adopted.get(int(c["gz_id"]), []) if pd.notna(c["gz_id"]) else [],
                "also_candidate_for": cross_candidate.get(int(c["gz_id"]), []) if pd.notna(c["gz_id"]) else [],
            })

        # Context points: already auto-matched / adopted places (for geographic context).
        # The gazetteer has duplicate gz_id rows, so dedupe by gz_id.
        context_points = []
        seen_ctx = set()
        ctx_mask = merged["phase_1a_outcome"].isin(["auto_adopt", "adopted_candidate"])
        for _, c in merged[ctx_mask].iterrows():
            gz = c.get("gz_id")
            if pd.isna(gz) or int(gz) in seen_ctx:
                continue
            lat = _to_coord(c.get("lat"))
            lon = _to_coord(c.get("lon"))
            if lat is None or lon is None:
                continue
            seen_ctx.add(int(gz))
            context_points.append({
                "label": str(c.get("ref_Label", "") or ""),
                "lugar_label": str(c.get("lugar_label", "") or ""),
                "lat": lat,
                "lon": lon,
            })

        return render_template(
            "disambiguate.html",
            prefix=prefix,
            has_candidates=True,
            idx=idx,
            total=total,
            row_id=current_row_id,
            ref_label=ref_label,
            ref_info=ref_info,
            candidates=candidates,
            candidates_json=json.dumps(candidates),
            context_json=json.dumps(context_points),
            table_context=_submitted_context(prefix, current_row_id, span=2),
        )
    except Exception as e:
        flash(f"Error loading disambiguation data: {e}")
        return redirect(url_for("matching.index"))


# ---------------------------------------------------------------------------
# Cached gazetteer + entidades (for the Phase 3b relaxed search)
# ---------------------------------------------------------------------------

_GAZ_DF = None
_SAINTS = None
_ENTIDADES_DF = None

CENTRAL_IGLESIA = {"Curato", "Mision cabecera"}
CIUDAD_VILLA = {"Ciudad", "Villa"}
PLACE_TYPES = ["Ciudad", "Villa", "Pueblo", "Poblacion", "Rural", "Fuerte", "Localidad"]


def _load_gaz():
    global _GAZ_DF, _SAINTS
    if _GAZ_DF is None:
        # NOTE: do NOT run ensure_dtype_consistency here — it coerces the
        # comma-decimal lat/lon ("20,676143") to NaN. _to_coord parses the
        # raw strings instead.
        ref = pd.read_csv(GAZETTEER_FILE, sep=";", encoding="utf-8")
        ref["_gz"] = pd.to_numeric(ref["gz_id"], errors="coerce")
        _GAZ_DF = ref
        _SAINTS = load_saints(os.path.join("data", "santos.csv"))
    return _GAZ_DF, _SAINTS


def _entidades():
    global _ENTIDADES_DF
    if _ENTIDADES_DF is None:
        d = pd.read_csv(ENTIDADES_FILE, sep=";", encoding="utf-8")
        d.columns = d.columns.str.lower()
        _ENTIDADES_DF = d
    return _ENTIDADES_DF


def _provinces_for_region(region_code):
    d = _entidades()
    return sorted(d[d["region"] == region_code]["provincia_generica"].dropna().unique().tolist())


def _districts_for_province(province):
    d = _entidades()
    return sorted(d[d["provincia_generica"] == province]["partido_generico"].dropna().unique().tolist())


def _all_region_codes():
    d = _entidades()
    return sorted(c for c in d["region"].dropna().astype(str).str.strip().unique()
                  if c and c.lower() not in ("dummy", "nan"))


def _best_toponym(lc, cand):
    """Best (field, score 0-100) of the cleaned input vs nombre/label/variantes."""
    best_field, best = None, 0
    for field, col in (("nombre", "lugar_nombre"), ("label", "lugar_label"), ("variante", "lugar_variantes")):
        val = cand.get(col)
        if pd.isna(val) or not str(val).strip():
            continue
        for piece in clean_toponym(str(val)).split("@"):
            piece = piece.strip()
            if not piece:
                continue
            s = fuzz.partial_ratio(lc, piece)
            if s > best:
                best, best_field = s, field
    return best_field, best


def _search_relegated(ref_label, region_code, province, district, central, ciudadvilla, limit=250):
    """Search the gazetteer for candidates within the chosen territory + place-type
    filters, ranked by toponym field (nombre>label>variante) and saint match.

    No fuzzy cut-off is applied: the relegated label can be misleading (e.g.
    "Curato de la Catedral" is actually "Charcas"), so every place in the chosen
    territory is offered, just ranked by name similarity."""
    ref, saints = _load_gaz()
    lc = clean_toponym(str(ref_label or ""))
    if not lc:
        return []

    df = ref
    if district and district != "Unknown":
        df = df[df["lugar_partido_generico"].astype(str).str.strip() == district]
    elif province:
        df = df[df["lugar_provincia_generica"].astype(str).str.strip() == province]
    elif region_code:
        region_name = region_code_to_name(region_code)
        if not region_name:
            return []
        df = df[df["lugar_region"].astype(str).str.strip() == region_name]
    else:
        return []  # need at least a region to bound the search

    if central:
        df = df[df["lugar_iglesia_cat"].astype(str).str.strip().isin(CENTRAL_IGLESIA)]
    if ciudadvilla:
        df = df[df["lugar_categoria"].astype(str).str.strip().isin(CIUDAD_VILLA)]
    if df.empty:
        return []

    saint_in = extract_saint(lc, saints)
    field_weight = {"nombre": 3, "label": 2, "variante": 1}
    out, seen = [], set()
    for _, c in df.iterrows():
        gz = c.get("_gz")
        if pd.isna(gz):
            continue
        gzi = int(gz)
        if gzi in seen:
            continue
        field, score = _best_toponym(lc, c)
        if field is None:
            field, score = "label", 0  # no name match, but still listed (ranked low)
        seen.add(gzi)
        name_clean = clean_toponym(str(c.get("lugar_nombre") or c.get("lugar_label") or ""))
        saint_status = compare_saints(saint_in, extract_saint(name_clean, saints))
        rank = (field_weight[field] * 100 + score
                + (50 if saint_status == "saint_match" else (-200 if saint_status == "saint_mismatch" else 0)))
        out.append({
            "gz_id": gzi,
            "lugar_label": str(c.get("lugar_label", "") or ""),
            "lugar_nombre": str(c.get("lugar_nombre", "") or ""),
            "lugar_variantes": str(c.get("lugar_variantes", "") or ""),
            "lugar_categoria": str(c.get("lugar_categoria", "") or ""),
            "lugar_iglesia_cat": str(c.get("lugar_iglesia_cat", "") or ""),
            "lugar_partido_generico": str(c.get("lugar_partido_generico", "") or ""),
            "lugar_provincia_generica": str(c.get("lugar_provincia_generica", "") or ""),
            "lugar_region": str(c.get("lugar_region", "") or ""),
            "match_field": field,
            "match_score": round(score / 100, 3),
            "saint_status": saint_status,
            "rank": rank,
            "lat": _to_coord(c.get("lat")),
            "lon": _to_coord(c.get("lon")),
        })
    # Two tiers: strong name matches (score >= 0.6) ranked by toponym+saint;
    # everything else (label can be misleading) listed alphabetically by label.
    strong = [r for r in out if r["match_score"] >= 0.6]
    weak = [r for r in out if r["match_score"] < 0.6]
    strong.sort(key=lambda r: r["rank"], reverse=True)
    weak.sort(key=lambda r: (r["lugar_label"] or "").lower())
    return (strong + weak)[:limit]


# ---------------------------------------------------------------------------
# Phase 3a: Global constraints
# ---------------------------------------------------------------------------

@bp.route("/set_constraints/<prefix>", methods=["GET", "POST"])
def set_constraints(prefix):
    if not os.path.exists(ENTIDADES_FILE):
        flash(f"Entidades file not found: {ENTIDADES_FILE}")
        return redirect(url_for("matching.index"))

    entidades_df = pd.read_csv(ENTIDADES_FILE, sep=";", encoding="utf-8")
    entidades_df.columns = entidades_df.columns.str.lower()

    # Region options as (code, verbose name). The checkbox value stays the code
    # (used for filtering/fetching); only the label shows the full name.
    # Column "region" = 3-letter code, "region_full" = verbose name.
    name_map = {}
    if "region_full" in entidades_df.columns:
        for code, name in zip(entidades_df["region"], entidades_df["region_full"]):
            if pd.notna(code) and pd.notna(name):
                c = str(code).strip()
                if c and c not in name_map:
                    name_map[c] = str(name).strip()
    region_codes = entidades_df["region"].dropna().astype(str).str.strip().unique().tolist()
    region_options = sorted(
        [(c, name_map.get(c, c)) for c in region_codes
         if c and c.lower() not in ("dummy", "nan")],
        key=lambda t: t[1].lower(),
    )

    if request.method == "POST":
        selected_regions = request.form.getlist("regions")
        selected_provinces = request.form.getlist("provinces")

        constraints = {
            "regions": selected_regions,
            "provinces": selected_provinces,
        }
        constraints_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_constraints.json")
        with open(constraints_path, "w", encoding="utf-8") as f:
            json.dump(constraints, f)

        flash("Global constraints saved.")
        # Entered before matching ("after=match") -> start the run now; otherwise
        # this is the Phase-3 step and we continue to territory resolution.
        after = request.form.get("after") or request.args.get("after") or ""
        if after == "match":
            return redirect(url_for("matching.run_matching_route", prefix=prefix))
        return redirect(url_for("matching.territory_choice", prefix=prefix))

    after = request.args.get("after", "")
    return render_template(
        "phase_3_constraints.html",
        prefix=prefix,
        region_options=region_options,
        identified_json=json.dumps(_identified_places(prefix)),
        after=after,
        pre_matching=(after == "match"),
    )


@bp.route("/districts_geojson")
def districts_geojson():
    """District polygons as GeoJSON, optionally filtered to region code(s)
    via ?reg=CHA,NES (comma-separated)."""
    gj = _load_districts()
    regs = request.args.get("reg", "")
    reg_set = {r.strip() for r in regs.split(",") if r.strip()}
    if reg_set:
        feats = [f for f in gj["features"] if f["properties"].get("Region") in reg_set]
    else:
        feats = gj["features"]
    return jsonify({"type": "FeatureCollection", "features": feats})


@bp.route("/get_provinces/<region_code>")
def get_provinces(region_code):
    if not os.path.exists(ENTIDADES_FILE):
        return jsonify([])
    df = pd.read_csv(ENTIDADES_FILE, sep=";", encoding="utf-8")
    df.columns = df.columns.str.lower()
    provinces = sorted(
        df[df["region"] == region_code]["provincia_generica"].dropna().unique().tolist()
    )
    return jsonify(provinces)


# ---------------------------------------------------------------------------
# Phase 3b: Case-specific territory choice for relegated rows
# ---------------------------------------------------------------------------

def _terr_filter_args(src):
    """Normalise the Phase 3b filter selections from a request source."""
    truthy = ("1", "on", "true")
    return {
        "region": (src.get("region") or "").strip(),
        "province": (src.get("province") or "").strip(),
        "district": (src.get("district") or "").strip(),
        "central": "1" if src.get("central") in truthy else "",
        "ciudadvilla": "1" if src.get("ciudadvilla") in truthy else "",
    }


@bp.route("/territory_choice/<prefix>", methods=["GET", "POST"])
def territory_choice(prefix):
    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(processing_path):
        flash(f"Missing processing file: {processing_path}")
        return redirect(url_for("matching.index"))

    df = pd.read_csv(processing_path, sep=";", encoding="utf-8")
    df["gz_id"] = pd.to_numeric(df["gz_id"], errors="coerce").astype("Int64")

    constraints_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_constraints.json")
    if os.path.exists(constraints_path):
        with open(constraints_path, "r", encoding="utf-8") as f:
            constraints = json.load(f)
    else:
        constraints = {"regions": [], "provinces": []}

    # ----- POST: record a decision and advance -----
    if request.method == "POST":
        filters = _terr_filter_args(request.form)
        try:
            idx = int(request.form.get("idx", 0))
        except (TypeError, ValueError):
            idx = 0
        try:
            rowID = int(request.form.get("row_id"))
        except (TypeError, ValueError):
            rowID = None
        action = request.form.get("action", "")
        decided = False

        if rowID is not None:
            mask = (df["rowID"] == rowID) & (df["phase_1a_outcome"] == "relegated")
            if action == "adopt" and request.form.get("choice"):
                try:
                    gz = int(request.form.get("choice"))
                except ValueError:
                    gz = None
                if gz is not None:
                    ref_df, _ = _load_gaz()
                    grow = ref_df[ref_df["_gz"] == gz]
                    df.loc[mask, "gz_id"] = gz
                    df.loc[mask, "phase_1a_outcome"] = "adopted_relegated"
                    if not grow.empty:
                        g0 = grow.iloc[0]
                        df.loc[mask, "lugar_label"] = str(g0.get("lugar_label", "") or "")
                        df.loc[mask, "lugar_partido_generico"] = str(g0.get("lugar_partido_generico", "") or "")
                        df.loc[mask, "lugar_provincia_generica"] = str(g0.get("lugar_provincia_generica", "") or "")
                    decided = True
            elif action == "adopt_new":
                # Place not in the database: manual coordinates + place type.
                lat = (request.form.get("manual_lat") or "").strip()
                lon = (request.form.get("manual_lon") or "").strip()
                ptype = (request.form.get("place_type") or "").strip()
                if lat and lon and ptype:
                    df.loc[mask, "phase_1a_outcome"] = "manual_new"
                    df.loc[mask, "gz_id"] = pd.NA
                    df.loc[mask, "manual_lat"] = lat
                    df.loc[mask, "manual_lon"] = lon
                    df.loc[mask, "manual_category"] = ptype
                    decided = True
                else:
                    flash("New place needs coordinates and a place type.")
            elif action == "reject":
                df.loc[mask, "phase_1a_outcome"] = "rejected_relegated"
                decided = True
            # action == "skip": leave as relegated

        df.to_csv(processing_path, sep=";", index=False, encoding="utf-8")
        next_idx = idx if decided else idx + 1
        return redirect(url_for("matching.territory_choice", prefix=prefix, idx=next_idx, **filters))

    # ----- GET: show the current relegated case -----
    relegated_ids = df[df["phase_1a_outcome"] == "relegated"]["rowID"].drop_duplicates().tolist()
    total = len(relegated_ids)
    if total == 0:
        flash("No relegated cases left to resolve.")
        return redirect(url_for("matching.export_results", prefix=prefix))

    try:
        idx = int(request.args.get("idx", 0))
    except ValueError:
        idx = 0
    if idx >= total:
        flash("Territory resolution complete.")
        return redirect(url_for("matching.export_results", prefix=prefix))
    idx = max(0, idx)

    current_row_id = relegated_ids[idx]
    case = df[df["rowID"] == current_row_id].iloc[0]
    ref_label = str(case.get("ref_Label", "") or "")
    ref_info = {}
    for col in df.columns:
        if col.startswith("ref_") and col != "ref_Label":
            val = case.get(col)
            if pd.notna(val) and str(val).strip():
                ref_info[col] = str(val)

    filters = _terr_filter_args(request.args)
    # On a fresh entry (no filter args) default the territory to the constraints.
    fresh = not any(k in request.args for k in ("region", "province", "district", "central", "ciudadvilla"))
    if fresh:
        if len(constraints.get("regions", [])) == 1:
            filters["region"] = constraints["regions"][0]
        if len(constraints.get("provinces", [])) == 1:
            filters["province"] = constraints["provinces"][0]

    # Selector options (region/province limited to the global constraints)
    region_codes = constraints.get("regions") or _all_region_codes()
    region_options = [(code, region_code_to_name(code) or code) for code in region_codes]
    if filters["region"]:
        province_options = _provinces_for_region(filters["region"])
        if constraints.get("provinces"):
            allowed = set(constraints["provinces"])
            province_options = [p for p in province_options if p in allowed] or province_options
    else:
        province_options = constraints.get("provinces") or []
    district_options = _districts_for_province(filters["province"]) if filters["province"] else []

    # Optional free-text lookup: overrides the row's label but keeps the
    # territory + place-type filters (search within the 3b constraints).
    lookup = (request.args.get("q") or "").strip()
    search_term = lookup or ref_label
    candidates = _search_relegated(
        search_term, filters["region"], filters["province"], filters["district"],
        filters["central"] == "1", filters["ciudadvilla"] == "1",
    )

    # New places already created in this run that share this toponym — offered
    # for reuse so repeated names (e.g. two "Manila" rows) get the same
    # coordinates instead of being pinned twice.
    prior_new = []
    if (df["phase_1a_outcome"] == "manual_new").any():
        cur_norm = _lc(ref_label)
        seen_nc = set()
        for _, r in df[df["phase_1a_outcome"] == "manual_new"].iterrows():
            if r.get("rowID") == current_row_id:
                continue
            lbl = str(r.get("ref_Label", "") or "")
            lat = str(r.get("manual_lat", "") or "").strip()
            lon = str(r.get("manual_lon", "") or "").strip()
            cat = str(r.get("manual_category", "") or "").strip()
            if not lat or not lon or _lc(lbl) != cur_norm:
                continue
            key = (lat, lon, cat)
            if key in seen_nc:
                continue
            seen_nc.add(key)
            prior_new.append({"label": lbl, "lat": lat, "lon": lon, "category": cat})

    return render_template(
        "phase_3_territory_choice.html",
        prior_new=prior_new,
        prefix=prefix,
        idx=idx,
        total=total,
        lookup=lookup,
        search_term=search_term,
        row_id=int(current_row_id),
        ref_label=ref_label,
        ref_info=ref_info,
        filters=filters,
        region_options=region_options,
        province_options=province_options,
        district_options=district_options,
        place_types=PLACE_TYPES,
        candidates=candidates,
        candidates_json=json.dumps(candidates),
        identified_json=json.dumps(_identified_places(prefix)),
    )


@bp.route("/get_districts/<province>")
def get_districts(province):
    if not os.path.exists(ENTIDADES_FILE):
        return jsonify([])
    df = pd.read_csv(ENTIDADES_FILE, sep=";", encoding="utf-8")
    df.columns = df.columns.str.lower()
    districts = sorted(
        df[df["provincia_generica"] == province]["partido_generico"].dropna().unique().tolist()
    )
    if "Unknown" not in districts:
        districts.insert(0, "Unknown")
    return jsonify(districts)


# ---------------------------------------------------------------------------
# Phase 4: Finalize & export (aggregate resolved gz_id + coords onto the tables)
# ---------------------------------------------------------------------------

_GZINFO_DF = None
ADOPTED_OUTCOMES = {"auto_adopt", "adopted_candidate", "adopted_relegated"}
OPTIONAL_EXPORT_FIELDS = ["cert", "iglesia_cat", "categoria", "categoria_esp"]


def _gzinfo():
    global _GZINFO_DF
    if _GZINFO_DF is None:
        g = pd.read_csv(os.path.join("data", "gz_info_1.csv"), sep=";",
                        encoding="utf-8", low_memory=False)
        g["_gz"] = pd.to_numeric(g["gz_id"], errors="coerce")
        g["_start"] = pd.to_numeric(g["start"], errors="coerce")
        g["_end"] = pd.to_numeric(g["end_"], errors="coerce")
        _GZINFO_DF = g
    return _GZINFO_DF


def _to_year(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none"):
            return None
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _derive_place(gz_id, start, end):
    """Coords + attributes for a gz_id: latest gz_info_1 slice overlapping the
    row's [start, end], else the gz_entidades base entry."""
    out = {"lat_y": "", "lon_x": "", "cert": "", "iglesia_cat": "",
           "categoria": "", "categoria_esp": "", "source": "none"}

    g = _gzinfo()
    sl = g[g["_gz"] == gz_id]
    if not sl.empty and start is not None and end is not None:
        ov = sl[(sl["_start"] <= end) & (sl["_end"] >= start)]
        if not ov.empty:
            chosen = ov.sort_values(["_start", "_end"]).iloc[-1]  # latest slice
            out.update(
                lat_y=_to_coord(chosen.get("lat")) or "",
                lon_x=_to_coord(chosen.get("lon")) or "",
                cert=str(chosen.get("cert", "") or ""),
                iglesia_cat=str(chosen.get("iglesia_cat", "") or ""),
                categoria=str(chosen.get("categoria", "") or ""),
                categoria_esp=str(chosen.get("categoria_especial", "") or ""),
                source="gz_info_1",
            )
            return out

    # Fallback: gz_entidades base (no overlapping chronology)
    ent = _entidades()
    er = ent[pd.to_numeric(ent["gz_id"], errors="coerce") == gz_id]
    if not er.empty:
        e0 = er.iloc[0]
        out.update(
            lat_y=_to_coord(e0.get("lat")) or "",
            lon_x=_to_coord(e0.get("lon")) or "",
            categoria=str(e0.get("categoria", "") or ""),
            source="gz_entidades",
        )
    return out


ARCA_PROVENANCE = (
    'This dataset has been matched to the "HGIS de las Indias" gazetteer and added '
    'to the ARCA de las Indias data collection, contributing to compiling and '
    'connecting historical datasets for Latin America.'
)


def _build_readme(prefix):
    """README content = the dataset's Dublin Core metadata (from transform, if
    present), with the ARCA provenance note prepended to dc:description, plus
    'Arca de las Indias' as an additional contributor and publisher."""
    meta_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_metadata.txt")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            lines = f.read().rstrip("\n").split("\n")
    else:
        lines = [f"<dc:title>Spatial data: {prefix}</dc:title>"]

    # Prepend the ARCA/matching provenance note to dc:description (add one if absent).
    open_t, close_t = "<dc:description>", "</dc:description>"
    injected = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(open_t) and s.endswith(close_t):
            inner = s[len(open_t):-len(close_t)]
            sep = " " if inner.strip() else ""
            lines[i] = f"{open_t}{ARCA_PROVENANCE}{sep}{inner}{close_t}"
            injected = True
            break
    if not injected:
        lines.insert(1 if len(lines) > 1 else len(lines),
                     f"{open_t}{ARCA_PROVENANCE}{close_t}")

    lines.append("<dc:contributor>Arca de las Indias</dc:contributor>")
    lines.append("<dc:publisher>Arca de las Indias</dc:publisher>")
    return "\n".join(lines) + "\n"


def _find_formatted(prefix):
    exact = os.path.join(UPLOAD_FOLDER, f"{prefix}_formatted.csv")
    if os.path.exists(exact) and os.path.getsize(exact) > 0:
        return exact
    cands = [p for p in glob.glob(os.path.join(UPLOAD_FOLDER, f"{prefix}*_formatted.csv"))
             if os.path.getsize(p) > 0]
    return max(cands, key=os.path.getmtime) if cands else None


def _resolve_rows(proc):
    """Per rowID -> {ID, gz_id, lat_y, lon_x, cert, iglesia_cat, categoria, categoria_esp}."""
    proc = proc.copy()
    proc["_gz"] = pd.to_numeric(proc["gz_id"], errors="coerce")
    records = []
    for rid, grp in proc.groupby("rowID"):
        s = _to_year(grp["ref_START"].iloc[0]) if "ref_START" in grp.columns else None
        e = _to_year(grp["ref_END"].iloc[0]) if "ref_END" in grp.columns else None
        rec = {"ID": rid, "gz_id": "", "lat_y": "", "lon_x": "", "cert": "",
               "iglesia_cat": "", "categoria": "", "categoria_esp": ""}

        manual = grp[grp["phase_1a_outcome"] == "manual_new"]
        adopted = grp[grp["phase_1a_outcome"].isin(ADOPTED_OUTCOMES) & grp["_gz"].notna()]
        if not manual.empty:
            m = manual.iloc[0]
            rec.update(
                lat_y=_to_coord(m.get("manual_lat")) or "",
                lon_x=_to_coord(m.get("manual_lon")) or "",
                categoria=str(m.get("manual_category", "") or ""),
            )
        elif not adopted.empty:
            gz = int(adopted["_gz"].iloc[0])
            d = _derive_place(gz, s, e)
            rec.update(gz_id=gz, lat_y=d["lat_y"], lon_x=d["lon_x"], cert=d["cert"],
                       iglesia_cat=d["iglesia_cat"], categoria=d["categoria"],
                       categoria_esp=d["categoria_esp"])
        records.append(rec)
    return pd.DataFrame(records)


def _build_export(prefix, opt_fields):
    """Build the results files + zip; returns (zip_bytes, stats)."""
    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    proc = pd.read_csv(processing_path, sep=";", encoding="utf-8")

    resolved = _resolve_rows(proc)
    cols = ["ID", "gz_id", "lat_y", "lon_x"] + [f for f in opt_fields if f in OPTIONAL_EXPORT_FIELDS]
    add_df = resolved[cols]

    outdir = os.path.join(UPLOAD_FOLDER, "results", prefix)
    os.makedirs(outdir, exist_ok=True)
    written = []

    def _merge_and_write(src_path, out_name):
        base = pd.read_csv(src_path, sep=";", encoding="utf-8-sig")
        if "ID" not in base.columns:
            return None
        base["ID"] = pd.to_numeric(base["ID"], errors="coerce")
        merged = base.merge(add_df, on="ID", how="left")
        # keep gz_id as a clean integer string (no trailing .0), empty if unresolved
        merged["gz_id"] = merged["gz_id"].apply(
            lambda v: "" if (pd.isna(v) or str(v).strip() == "") else str(int(float(v)))
        )
        path = os.path.join(outdir, out_name)
        merged.to_csv(path, sep=";", index=False, encoding="utf-8")
        written.append(out_name)
        return merged

    # formatted -> {prefix}_results.csv ; original -> {prefix}_original_results.csv
    fmt_path = _find_formatted(prefix)
    if fmt_path:
        _merge_and_write(fmt_path, f"{prefix}_results.csv")
    orig_path = os.path.join(UPLOAD_FOLDER, f"{prefix}.csv")
    if os.path.exists(orig_path):
        _merge_and_write(orig_path, f"{prefix}_original_results.csv")

    # Point GeoJSON of the resolved places (lon_x / lat_y).
    pt_feats = []
    for r in add_df.itertuples(index=False):
        lat = _to_coord(getattr(r, "lat_y", ""))
        lon = _to_coord(getattr(r, "lon_x", ""))
        if lat is None or lon is None:
            continue
        props = {c: ("" if pd.isna(getattr(r, c)) else getattr(r, c)) for c in add_df.columns}
        pt_feats.append({"type": "Feature",
                         "properties": props,
                         "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    with open(os.path.join(outdir, f"{prefix}.geojson"), "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": pt_feats}, fh)
    written.append(f"{prefix}.geojson")

    shutil.copy(processing_path, os.path.join(outdir, f"{prefix}_processing.csv"))
    written.append(f"{prefix}_processing.csv")

    # Dataset metadata: the Dublin Core metadata (if produced in transform) with
    # "Arca de las Indias" added as an additional contributor and publisher.
    # Named {prefix}_metadata.txt (was README.txt).
    meta_name = f"{prefix}_metadata.txt"
    with open(os.path.join(outdir, meta_name), "w", encoding="utf-8") as fh:
        fh.write(_build_readme(prefix))
    written.append(meta_name)

    # Supporting files produced during transform, carried through if present:
    # per-column definitions and an uploaded bibliography.
    for extra in (f"{prefix}_field_definitions.csv", f"{prefix}_bibliography.txt"):
        src = os.path.join(UPLOAD_FOLDER, extra)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(outdir, extra))
            written.append(extra)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in written:
            z.write(os.path.join(outdir, fn), arcname=fn)
    buf.seek(0)

    stats = {
        "total": len(resolved),
        "with_gz": int((resolved["gz_id"].astype(str).str.strip() != "").sum()),
        "with_coords": int((resolved["lat_y"].astype(str).str.strip() != "").sum()),
        "files": written,
        "formatted_used": os.path.basename(fmt_path) if fmt_path else None,
    }
    return buf.read(), stats


# Every file that belongs to one dataset's run (exact names, so a prefix like
# "Tlaxcala" never deletes "Tlaxcala_1"). Used by the "download & delete" finish.
_DATASET_SUFFIXES = (
    ".csv", "_formatted.csv", "_cleaned.csv", "_processing.csv",
    "_domain.txt", "_metadata.txt", "_bibliography.txt", "_field_definitions.csv",
    "_constraints.json", "_status.json", "_status.json.tmp", "_status.txt",
    "_phase1a_complete.flag", "_phase1b_complete.flag",
    "_results.csv", "_original_results.csv", ".geojson",
)


def _delete_dataset(prefix):
    """Remove every artifact belonging to a dataset run. Returns names removed."""
    removed = []
    for suf in _DATASET_SUFFIXES:
        p = os.path.join(UPLOAD_FOLDER, f"{prefix}{suf}")
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(os.path.basename(p))
        except OSError:
            pass
    return removed


@bp.route("/export/<prefix>", methods=["GET", "POST"])
def export_results(prefix):
    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(processing_path):
        flash(f"Missing processing file: {processing_path}")
        return redirect(url_for("matching.index"))

    if request.method == "POST":
        opt_fields = [f for f in OPTIONAL_EXPORT_FIELDS if request.form.get(f)]
        zip_bytes, _stats = _build_export(prefix, opt_fields)
        resp = send_file(io.BytesIO(zip_bytes), mimetype="application/zip",
                         as_attachment=True, download_name=f"{prefix}_results.zip")
        # "Download & delete": once the bundle has streamed to the user, wipe the
        # dataset from the server (they chose not to keep/publish it).
        if request.form.get("and_delete"):
            resp.call_on_close(lambda: _delete_dataset(prefix))
        return resp

    # GET: summary + field choices
    proc = pd.read_csv(processing_path, sep=";", encoding="utf-8")
    per_row = proc.drop_duplicates(subset=["rowID"])
    outcome_counts = proc.groupby("rowID")["phase_1a_outcome"].agg(
        lambda s: "resolved" if (set(s) & (ADOPTED_OUTCOMES | {"manual_new"})) else "unresolved"
    ).value_counts().to_dict()
    fmt_path = _find_formatted(prefix)
    return render_template(
        "export.html",
        prefix=prefix,
        total_rows=int(proc["rowID"].nunique()),
        resolved=outcome_counts.get("resolved", 0),
        unresolved=outcome_counts.get("unresolved", 0),
        formatted_name=os.path.basename(fmt_path) if fmt_path else None,
        optional_fields=OPTIONAL_EXPORT_FIELDS,
        metadata_done=os.path.exists(os.path.join(UPLOAD_FOLDER, f"{prefix}_metadata.txt")),
    )


# ---------------------------------------------------------------------------
# Territories domain: match against Niveles_matching (entity + level/footprint)
# ---------------------------------------------------------------------------

NIVELES_FILE = os.path.join("data", "Niveles_matching.csv")
_NIVELES_DF = None
_NIVEL_ORDER = ["Partido", "Jurisdiccion", "Provincia_menor", "Provincia", "Provincia_mayor",
                "Intendencia", "Obispado", "Arzobispado", "Audiencia", "Senorio",
                "Fronteras", "Principal", "Virreinato", "Extranjero"]


def _load_niveles():
    global _NIVELES_DF
    if _NIVELES_DF is None:
        n = pd.read_csv(NIVELES_FILE, sep=";", encoding="utf-8", low_memory=False)
        n["yr_start"] = pd.to_numeric(n["START"], errors="coerce")
        n["yr_end"] = pd.to_numeric(n["END_"], errors="coerce")
        n["names_clean"] = [
            [clean_toponym(p) for p in
             "@".join(str(x) for x in (lab, nom, var) if pd.notna(x)).split("@") if p.strip()]
            for lab, nom, var in zip(n["Label"], n["Nombre"], n["Variantes"])
        ]
        _NIVELES_DF = n
    return _NIVELES_DF


def _nivel_rank(nivel):
    return _NIVEL_ORDER.index(nivel) if nivel in _NIVEL_ORDER else len(_NIVEL_ORDER)


_ENTIDADES_MAP = None


def _load_entidades_map():
    """Entidad_ID -> {prov, reg, region, ...}. Provincia_generica / Region are in
    entidades.csv (not in Niveles_matching), keyed by 'Entidad'."""
    global _ENTIDADES_MAP
    if _ENTIDADES_MAP is None:
        e = pd.read_csv(os.path.join("data", "entidades.csv"), sep=";",
                        encoding="utf-8", low_memory=False)
        m = {}
        for r in e.itertuples(index=False):
            m[str(r.Entidad)] = {
                "prov": str(getattr(r, "Provincia_generica", "") or ""),
                "reg": str(getattr(r, "Reg", "") or ""),
                "region": str(getattr(r, "Region", "") or ""),
                "lat": getattr(r, "Lat_Capital", None),
                "lon": getattr(r, "Lon_Capital", None),
            }
        _ENTIDADES_MAP = m
    return _ENTIDADES_MAP


def _lc(s):
    return str(s or "").strip().lower()


def _search_territory(name, start, end, nivel=None, provincia=None, region=None, limit=40):
    """Match a territory name against Niveles_matching, grouped by entity, with
    optional narrowing by Nivel (from Niveles_matching) and generic province /
    region (joined from entidades.csv). Narrowing is conservative: an entity is
    dropped only when it HAS a value that clearly disagrees."""
    n = _load_niveles()
    lc = clean_toponym(str(name or ""))
    if not lc:
        return []
    df = n
    if start is not None and end is not None:
        df = df[df["yr_start"].isna() | df["yr_end"].isna() | ((df["yr_start"] <= end) & (df["yr_end"] >= start))]

    ents = {}
    for r in df.itertuples(index=False):
        best = 0
        for piece in r.names_clean:
            s = fuzz.partial_ratio(lc, piece)
            if s > best:
                best = s
        if best < 70:
            continue
        eid = str(r.Entidad_ID)
        e = ents.get(eid)
        if e is None:
            e = ents[eid] = {"entidad_id": eid, "label": str(r.Label or ""),
                             "nombre": str(r.Nombre or ""), "score": 0, "slices": {}}
        e["score"] = max(e["score"], best)
        nv = str(r.Nivel or "")
        if nv:
            st = None if pd.isna(r.yr_start) else int(r.yr_start)
            en = None if pd.isna(r.yr_end) else int(r.yr_end)
            # one option per distinct (level, tipo, date-range) footprint slice
            e["slices"][(nv, str(r.Tipo or ""), st, en)] = True

    # --- optional narrowing ---
    nivel_lc, prov_lc, reg_lc = _lc(nivel), _lc(provincia), _lc(region)
    ent_map = _load_entidades_map() if (prov_lc or reg_lc) else {}

    out = []
    for e in ents.values():
        eid = e["entidad_id"]
        keys = list(e["slices"].keys())  # (nivel, tipo, start, end)
        if nivel_lc:
            lv_vals = {_lc(k[0]) for k in keys}
            tp_vals = {_lc(k[1]) for k in keys}
            if (lv_vals or tp_vals) and nivel_lc not in lv_vals and nivel_lc not in tp_vals:
                continue
        em = ent_map.get(eid, {})
        if prov_lc and em.get("prov") and _lc(em["prov"]) != prov_lc:
            continue
        if reg_lc and (em.get("reg") or em.get("region")) \
                and reg_lc != _lc(em.get("reg")) and reg_lc != _lc(em.get("region")):
            continue

        levels = []
        for (nv, tp, st, en) in keys:
            levels.append({
                "nivel": nv, "tipo": tp, "start": st, "end": en,
                "pick": "|".join([eid, nv, tp,
                                  str(st) if st is not None else "",
                                  str(en) if en is not None else ""]),
            })
        levels.sort(key=lambda s: (_nivel_rank(s["nivel"]),
                                   s["start"] if s["start"] is not None else -9999))
        out.append({
            "entidad_id": eid, "label": e["label"], "nombre": e["nombre"],
            "score": round(e["score"] / 100, 3), "levels": levels,
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


# --- Territory footprint geometry: one GeoJSON per Nivel ---
# e.g. data/geo/source/provincia_menor.geojson, provincia.geojson, partido.geojson…
# Each file's features carry Entidad_ID + START_Territorio / END_Territorio.
NIVEL_GEO_DIR = os.path.join("data", "geo", "source")
_NIVEL_GEO_CACHE = {}


def _nivel_geo_path(nivel):
    return os.path.join(NIVEL_GEO_DIR, f"{str(nivel or '').strip().lower()}.geojson")


def _load_nivel_geo(nivel):
    """Index one Nivel's GeoJSON by Entidad_ID -> [features]. Cached; returns {}
    if the file for that Nivel doesn't exist yet."""
    key = str(nivel or "").strip().lower()
    if key not in _NIVEL_GEO_CACHE:
        idx = {}
        path = _nivel_geo_path(nivel)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    gj = json.load(f)
                for feat in gj.get("features", []):
                    eid = str((feat.get("properties") or {}).get("Entidad_ID") or "")
                    if eid:
                        idx.setdefault(eid, []).append(feat)
            except Exception:
                idx = {}
        _NIVEL_GEO_CACHE[key] = idx
    return _NIVEL_GEO_CACHE[key]


def _footprint_feature(eid, nivel, start, end):
    """Footprint polygon for an entity at a given Nivel, from that Nivel's GeoJSON.
    Prefers the chronology-overlapping slice (latest START_Territorio)."""
    feats = _load_nivel_geo(nivel).get(str(eid), [])
    if not feats:
        return None

    def _yr(feat, k):
        return _to_year((feat.get("properties") or {}).get(k))

    cand = feats
    if start is not None and end is not None:
        ov = []
        for feat in feats:
            s, e = _yr(feat, "START"), _yr(feat, "END_")
            if s is None or e is None or (s <= end and e >= start):
                ov.append(feat)
        cand = ov or feats
    return max(cand, key=lambda f: (_yr(f, "START")
                                    if _yr(f, "START") is not None else -9999))


@bp.route("/territory_geojson/<prefix>")
def territory_geojson(prefix):
    """Return the best footprint polygon per requested entity (?ids=EID1,EID2,
    optional &nivel=Level and &start=&end= for the row's date)."""
    ids = [x.strip() for x in (request.args.get("ids") or "").split(",") if x.strip()]
    nivel = (request.args.get("nivel") or "").strip()
    start = _to_year(request.args.get("start"))
    end = _to_year(request.args.get("end"))
    feats = [f for f in (_footprint_feature(eid, nivel, start, end) for eid in ids) if f]
    return jsonify({"type": "FeatureCollection", "features": feats})


def _territory_name_col(inp):
    for c in ("ref_Label", "ref_label"):
        if c in inp.columns:
            return c
    for c in inp.columns:
        if c.startswith("ref_") and c not in ("ref_START", "ref_END", "ref_Region"):
            return c
    return inp.columns[0] if len(inp.columns) else None


def _build_territory_export(prefix):
    """Build the territories bundle: a polygon GeoJSON (full footprint geometry
    per matched row) + the results CSV + README. Returns (zip_bytes, stats)."""
    results_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_territory_results.csv")
    res = pd.read_csv(results_path, sep=";", encoding="utf-8")

    feats = []
    n_geom = 0
    for r in res.itertuples(index=False):
        if str(getattr(r, "outcome", "")) != "matched":
            continue
        eid = str(getattr(r, "entidad_id", "") or "").strip()
        if not eid:
            continue
        nivel = str(getattr(r, "nivel", "") or "").strip()
        st = _to_year(getattr(r, "start", None))
        en = _to_year(getattr(r, "end", None))
        feat = _footprint_feature(eid, nivel, st, en)
        if not feat:
            continue
        # tag the footprint with the row's identity so it round-trips
        props = feat.get("properties", {}) or {}
        props.update({
            "rowID": int(getattr(r, "rowID")) if pd.notna(getattr(r, "rowID")) else None,
            "ref_Label": str(getattr(r, "ref_Label", "") or ""),
            "Entidad_ID": eid, "Nivel": nivel,
            "match_tipo": str(getattr(r, "tipo", "") or ""),
            "match_start": st, "match_end": en,
        })
        feat["properties"] = props
        feats.append(feat)
        n_geom += 1

    outdir = os.path.join(UPLOAD_FOLDER, "results", prefix)
    os.makedirs(outdir, exist_ok=True)
    written = []

    with open(os.path.join(outdir, f"{prefix}_territories.geojson"), "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
    written.append(f"{prefix}_territories.geojson")

    # Join entity / level / capital coords back onto the original + formatted tables.
    ent_map = _load_entidades_map()
    recs = []
    for r in res.itertuples(index=False):
        rid = getattr(r, "rowID")
        matched = str(getattr(r, "outcome", "")) == "matched"
        eid = str(getattr(r, "entidad_id", "") or "") if matched else ""
        em = ent_map.get(eid, {}) if eid else {}
        recs.append({
            "ID": int(rid) if pd.notna(rid) else None,
            "entidad_id": eid,
            "nivel": str(getattr(r, "nivel", "") or "") if matched else "",
            "tipo": str(getattr(r, "tipo", "") or "") if matched else "",
            "start": getattr(r, "start", "") if matched else "",
            "end": getattr(r, "end", "") if matched else "",
            "lat_y": _to_coord(em.get("lat")) or "",
            "lon_x": _to_coord(em.get("lon")) or "",
        })
    add_df = pd.DataFrame(recs)

    def _merge_write(src_path, out_name):
        base = pd.read_csv(src_path, sep=";", encoding="utf-8-sig")
        if "ID" not in base.columns:
            return
        base["ID"] = pd.to_numeric(base["ID"], errors="coerce")
        merged = base.merge(add_df, on="ID", how="left")
        merged.to_csv(os.path.join(outdir, out_name), sep=";", index=False, encoding="utf-8")
        written.append(out_name)

    fmt_path = _find_formatted(prefix)
    if fmt_path:
        _merge_write(fmt_path, f"{prefix}_results.csv")
    orig_path = os.path.join(UPLOAD_FOLDER, f"{prefix}.csv")
    if os.path.exists(orig_path):
        _merge_write(orig_path, f"{prefix}_original_results.csv")

    shutil.copy(results_path, os.path.join(outdir, f"{prefix}_territory_results.csv"))
    written.append(f"{prefix}_territory_results.csv")

    # Dataset metadata, named {prefix}_metadata.txt (was README.txt).
    meta_name = f"{prefix}_metadata.txt"
    with open(os.path.join(outdir, meta_name), "w", encoding="utf-8") as fh:
        fh.write(_build_readme(prefix))
    written.append(meta_name)

    # Supporting files from transform, carried through if present.
    for extra in (f"{prefix}_field_definitions.csv", f"{prefix}_bibliography.txt"):
        src = os.path.join(UPLOAD_FOLDER, extra)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(outdir, extra))
            written.append(extra)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in written:
            z.write(os.path.join(outdir, fn), arcname=fn)
    buf.seek(0)
    stats = {"total": len(res),
             "matched": int((res["outcome"].astype(str) == "matched").sum()),
             "with_geometry": n_geom, "files": written}
    return buf.read(), stats


@bp.route("/territory_export/<prefix>", methods=["GET", "POST"])
def territory_export(prefix):
    results_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_territory_results.csv")
    if not os.path.exists(results_path):
        flash("No territory results to export yet.")
        return redirect(url_for("matching.territory_match", prefix=prefix))

    if request.method == "POST":
        zip_bytes, _stats = _build_territory_export(prefix)
        return send_file(io.BytesIO(zip_bytes), mimetype="application/zip",
                         as_attachment=True, download_name=f"{prefix}_territories.zip")

    res = pd.read_csv(results_path, sep=";", encoding="utf-8")
    matched = int((res["outcome"].astype(str) == "matched").sum())
    return render_template("territory_export.html", prefix=prefix,
                           total=len(res), matched=matched)


@bp.route("/territory_match/<prefix>", methods=["GET", "POST"])
def territory_match(prefix):
    cleaned = os.path.join(UPLOAD_FOLDER, f"{prefix}_cleaned.csv")
    if not os.path.exists(cleaned):
        flash(f"No cleaned table found for “{prefix}”.")
        return redirect(url_for("matching.index"))

    inp = pd.read_csv(cleaned, sep=";", encoding="utf-8")
    if "rowID" not in inp.columns:
        inp.insert(0, "rowID", range(1, len(inp) + 1))
    name_col = _territory_name_col(inp)

    results_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_territory_results.csv")
    res_cols = ["rowID", "ref_Label", "entidad_id", "nivel", "tipo", "start", "end", "outcome"]
    if os.path.exists(results_path):
        res = pd.read_csv(results_path, sep=";", encoding="utf-8")
    else:
        res = pd.DataFrame(columns=res_cols)

    if request.method == "POST":
        try:
            rowID = int(request.form.get("row_id"))
        except (TypeError, ValueError):
            rowID = None
        action = request.form.get("action", "")
        try:
            idx = int(request.form.get("idx", 0))
        except (TypeError, ValueError):
            idx = 0
        q = (request.form.get("q") or "").strip()
        decided = False
        if rowID is not None and action in ("adopt", "reject"):
            res = res[res["rowID"] != rowID]
            label = str(inp[inp["rowID"] == rowID][name_col].iloc[0]) if name_col else ""
            rec = {"rowID": rowID, "ref_Label": label, "entidad_id": "",
                   "nivel": "", "tipo": "", "start": "", "end": "", "outcome": action}
            if action == "adopt":
                # "pick" = entidad_id|nivel|tipo|start|end from the chosen slice
                parts = (request.form.get("pick", "") or "").split("|")
                parts += [""] * (5 - len(parts))
                rec["entidad_id"], rec["nivel"], rec["tipo"], rec["start"], rec["end"] = parts[:5]
                rec["outcome"] = "matched"
            res = pd.concat([res, pd.DataFrame([rec])], ignore_index=True)
            res.to_csv(results_path, sep=";", index=False, encoding="utf-8")
            decided = True
        next_idx = idx if decided else idx + 1
        return redirect(url_for("matching.territory_match", prefix=prefix, idx=next_idx, q=q or None))

    # GET
    resolved_ids = set(pd.to_numeric(res["rowID"], errors="coerce").dropna().astype(int).tolist())
    ordered = [int(r) for r in inp["rowID"].tolist() if int(r) not in resolved_ids]
    total = len(inp)
    done = len(resolved_ids)
    if not ordered:
        flash("Territory matching complete.")
        return redirect(url_for("matching.territory_export", prefix=prefix))

    try:
        idx = int(request.args.get("idx", 0))
    except ValueError:
        idx = 0
    idx = max(0, min(idx, len(ordered) - 1))
    current = ordered[idx]
    row = inp[inp["rowID"] == current].iloc[0]
    ref_label = str(row.get(name_col, "") or "") if name_col else ""
    start = _to_year(row.get("ref_START"))
    end = _to_year(row.get("ref_END"))
    ref_info = {c: str(row[c]) for c in inp.columns
                if c.startswith("ref_") and c != name_col and pd.notna(row[c]) and str(row[c]).strip()}

    in_nivel = str(row.get("ref_Nivel", "") or "").strip()
    in_prov = str(row.get("ref_Provincia_generica", "") or "").strip()
    in_region = str(row.get("ref_Region", "") or "").strip()
    has_chrono = start is not None and end is not None

    # Which constraints the user has switched off (checkboxes), and which are
    # available at all (the input actually carries that field).
    off = {k: request.args.get("no_" + k) in ("1", "on", "true")
           for k in ("nivel", "prov", "region", "chrono")}
    avail = {"nivel": bool(in_nivel), "prov": bool(in_prov),
             "region": bool(in_region), "chrono": has_chrono}
    use = {k: avail[k] and not off[k] for k in avail}

    def _run(u):
        return _search_territory(
            lookup or ref_label,
            start if u["chrono"] else None, end if u["chrono"] else None,
            nivel=in_nivel if u["nivel"] else None,
            provincia=in_prov if u["prov"] else None,
            region=in_region if u["region"] else None,
        )

    lookup = (request.args.get("q") or "").strip()
    candidates = _run(use)

    # Auto-relax: if the reference-field constraints yield nothing, drop them;
    # if still nothing, drop chronology too.
    auto_relaxed = []
    if not candidates and (use["nivel"] or use["prov"] or use["region"]):
        u2 = dict(use, nivel=False, prov=False, region=False)
        candidates = _run(u2)
        if candidates:
            auto_relaxed = [k for k in ("nivel", "prov", "region") if use[k]]
            use = u2
    if not candidates and use["chrono"]:
        u3 = dict(use, chrono=False)
        candidates = _run(u3)
        if candidates:
            auto_relaxed.append("chrono")
            use = u3

    # Preselect the slice of the top-scoring candidate whose Nivel matches the input,
    # preferring one whose date range overlaps the row's chronology.
    presel_pick = ""
    if in_nivel:
        fallback = ""
        for c in candidates:
            for lv in c["levels"]:
                if _lc(lv["nivel"]) != _lc(in_nivel):
                    continue
                if not fallback:
                    fallback = lv["pick"]
                if start is not None and end is not None and lv["start"] is not None and lv["end"] is not None:
                    if lv["start"] <= end and lv["end"] >= start:
                        presel_pick = lv["pick"]
                        break
                else:
                    presel_pick = lv["pick"]
                    break
            if presel_pick:
                break
        if not presel_pick:
            presel_pick = fallback

    return render_template(
        "territory_match.html",
        prefix=prefix, idx=idx, remaining=len(ordered), done=done, total=total,
        row_id=current, ref_label=ref_label, ref_info=ref_info,
        start=start, end=end, lookup=lookup, candidates=candidates,
        avail=avail, use=use, off=off, auto_relaxed=auto_relaxed,
        in_nivel=in_nivel, in_prov=in_prov, in_region=in_region,
        presel_pick=presel_pick,
    )


# ---------------------------------------------------------------------------
# Phase 1a Review: case-by-case manual flagging of match results
# ---------------------------------------------------------------------------

REVIEW_COL = "review_status"
# Outcomes to review (skip pure relegated with no gz_id)
REVIEW_OUTCOMES = {"auto_adopt", "candidate"}

# Context columns to show (anything that starts with ref_ or Contexto)
def _context_cols(df):
    return [c for c in df.columns
            if (c.startswith("ref_") or c.lower().startswith("contexto")) and c != "ref_Label"]

@bp.route("/review", methods=["GET", "POST"])
def review():
    prefix = request.args.get("prefix")
    if not prefix:
        flash("Missing file prefix.")
        return redirect(url_for("matching.index"))

    processing_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_processing.csv")
    if not os.path.exists(processing_path):
        flash(f"Missing processing file: {processing_path}")
        return redirect(url_for("matching.index"))

    df = pd.read_csv(processing_path, sep=";", encoding="utf-8")

    # Ensure review column exists
    if REVIEW_COL not in df.columns:
        df[REVIEW_COL] = ""

    # Build list of unique rowIDs that have at least one reviewable outcome
    reviewable_ids = (
        df[df["phase_1a_outcome"].isin(REVIEW_OUTCOMES)]["rowID"]
        .unique().tolist()
    )
    total = len(reviewable_ids)

    if total == 0:
        flash("No reviewable rows (auto_adopt or candidate) found in the processing file.")
        return redirect(url_for("matching.match_status", prefix=prefix))

    try:
        idx = int(request.args.get("idx", 0))
    except ValueError:
        idx = 0
    idx = max(0, min(idx, total - 1))

    if request.method == "POST":
        action = request.form.get("action", "")
        current_row_id = request.form.get("row_id")
        if current_row_id:
            try:
                current_row_id = int(current_row_id)
            except ValueError:
                pass

        if action == "confirm":
            df.loc[df["rowID"] == current_row_id, REVIEW_COL] = "confirmed"
        elif action == "false_match":
            df.loc[df["rowID"] == current_row_id, REVIEW_COL] = "false_match"
        elif action == "correct_candidate":
            df.loc[df["rowID"] == current_row_id, REVIEW_COL] = "correct_candidate"

        df.to_csv(processing_path, sep=";", index=False, encoding="utf-8")

        # Navigate
        if action in ("confirm", "false_match", "correct_candidate", "skip"):
            next_idx = idx + 1
        else:
            next_idx = idx

        if next_idx >= total:
            flash("Review complete.")
            return redirect(url_for("matching.match_status", prefix=prefix))
        return redirect(url_for("matching.review", prefix=prefix, idx=next_idx))

    # GET: show current case
    current_row_id = reviewable_ids[idx]
    case_rows = df[df["rowID"] == current_row_id].copy()
    outcome = case_rows["phase_1a_outcome"].iloc[0]
    current_review = case_rows[REVIEW_COL].iloc[0] if REVIEW_COL in case_rows.columns else ""
    ctx_cols = _context_cols(df)

    # Merge gazetteer for additional lugar_ columns if needed
    gz_extra = {}
    if not case_rows["gz_id"].isna().all():
        try:
            gaz = pd.read_csv(GAZETTEER_FILE, sep=";", encoding="utf-8")
            for gz_id_val in case_rows["gz_id"].dropna().unique():
                gz_row = gaz[gaz["gz_id"] == gz_id_val]
                if not gz_row.empty:
                    gz_extra[int(gz_id_val)] = gz_row.iloc[0].to_dict()
        except Exception:
            pass

    return render_template(
        "review.html",
        prefix=prefix,
        idx=idx,
        total=total,
        row_id=current_row_id,
        outcome=outcome,
        case_rows=case_rows.to_dict(orient="records"),
        ctx_cols=ctx_cols,
        gz_extra=gz_extra,
        current_review=current_review,
    )
