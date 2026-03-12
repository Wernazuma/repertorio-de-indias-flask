import os
from flask import render_template, request, redirect, url_for, flash
from . import bp
from .main import match_phase_1

# Consistent upload folder
UPLOAD_FOLDER = os.path.join("data", "uploads")


@bp.route("/", methods=["GET", "POST"])
def index():
    prefix = ""
    if request.method == "POST":
        prefix = request.form.get("prefix", "").strip()

        if not os.path.exists(UPLOAD_FOLDER):
            os.makedirs(UPLOAD_FOLDER)

        file_path = os.path.join(UPLOAD_FOLDER, f"{prefix}_cleaned.csv")

        if not os.path.exists(file_path):
            flash(f"File not found: {file_path}")
            return render_template("upload_form.html", prefix="")

        flash(f"File loaded successfully: {file_path}")
        return render_template("upload_form.html", prefix=prefix)

    return render_template("upload_form.html", prefix=prefix)


@bp.route("/match_phase_1", methods=["GET"])
def run_matching_route():
    prefix = request.args.get("prefix")
    if not prefix:
        flash("Missing file prefix.")
        return redirect(url_for("matching.index"))

    # Check for debug mode
    debug_mode = request.args.get("debug", "").lower() == "true"
    
    try:
        if debug_mode:
            # Enable debug logging for specific problematic cases
            debug_labels = [
                'puebla', 'puno', 'ciudad real', 'aguas calientes',
                'santiago', 'la paz', 'coquimbo'
            ]
            output_path = match_phase_1(prefix, debug_labels=debug_labels)
            flash(f"Phase 1a completed with DEBUG mode. Check logs/{{prefix}}_debug.log for details.")
        else:
            output_path = match_phase_1(prefix)
            flash(f"Phase 1a completed successfully.")
        
        flash(f"Results saved to {output_path}")
        return redirect(url_for("matching.match_status", prefix=prefix))
    except Exception as e:
        flash(f"Error during matching: {e}")
        return redirect(url_for("matching.index"))


@bp.route("/match_status", methods=["GET"])
def match_status():
    prefix = request.args.get("prefix")
    if not prefix:
        return "Missing prefix", 400

    status_file = os.path.join(UPLOAD_FOLDER, f"{prefix}_status.txt")
    complete_flag = os.path.join(UPLOAD_FOLDER, f"{prefix}_phase1a_complete.flag")

    matching_complete = os.path.exists(complete_flag)
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = "Phase 1a: Matching not started yet."

    return render_template(
        "match_status.html",
        prefix=prefix,
        content=content,
        matching_complete=matching_complete,
    )


@bp.route("/disambiguate")
def disambiguate():
    prefix = request.args.get("prefix")
    if not prefix:
        flash("Missing file prefix.")
        return redirect(url_for("matching.index"))
    flash("Disambiguation step not yet implemented.")
    return redirect(url_for("matching.index"))
