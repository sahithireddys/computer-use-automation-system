"""
Mock "core banking back-office" web app.

Design intent (see /REPORT.md section 4 for full reasoning):
- Server-rendered, table-heavy, no test IDs, no semantic ARIA beyond
  defaults -> forces the agent/replay to rely on role + accessible name,
  which is the realistic condition in legacy enterprise UIs.
- A handful of special member IDs deterministically trigger the runtime
  conditions the assignment calls out (not-found, permission-denied,
  validation error, session timeout, slow load, confirmation dialog) so
  we can demonstrate the replay engine's error taxonomy on demand.
"""
from flask import Flask, request, render_template, redirect, url_for, session
import time
import secrets

from . import data

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# Special member IDs used to deterministically exercise runtime conditions.
TIMEOUT_TRIGGER_ID = "00000"
SLOW_TRIGGER_ID = "00001"
# Not a condition our recorded flow/replay engine knows about in advance --
# used to demonstrate the hard-failure -> human-escalation -> resume path.
UNEXPECTED_INTERSTITIAL_ID = "55555"


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/search")
def search():
    member_id = request.args.get("member_id", "").strip()

    if member_id == TIMEOUT_TRIGGER_ID:
        # Simulate an expired back-office session -> app bounces to a
        # re-authentication interstitial instead of the expected page.
        session.clear()
        return render_template("session_expired.html"), 200

    if member_id == SLOW_TRIGGER_ID:
        time.sleep(4)  # transient slowness the replay must tolerate/wait out

    if member_id == UNEXPECTED_INTERSTITIAL_ID and not session.get("verified"):
        # An out-of-band identity check the recorded flow never saw during
        # discovery -- simulates a vendor app rolling out a new interstitial
        # for a subset of records. Deliberately NOT in the replay engine's
        # known-recoverable list.
        return render_template("verify_identity.html", member_id=member_id), 200

    if not member_id:
        return render_template("home.html", error="Please enter a member ID."), 400

    member = data.find_member(member_id)
    if member is None:
        # Legitimate business outcome, not a crash.
        return render_template("not_found.html", member_id=member_id), 200

    if member.status == "restricted":
        return render_template("forbidden.html", member_id=member_id), 200

    return render_template("member_detail.html", member=member)


@app.route("/member/<member_id>/open-account", methods=["GET"])
def open_account_form(member_id):
    member = data.find_member(member_id)
    if member is None:
        return render_template("not_found.html", member_id=member_id), 200
    if member.status == "restricted":
        return render_template("forbidden.html", member_id=member_id), 200
    return render_template("open_account.html", member=member, errors=None)


@app.route("/member/<member_id>/open-account", methods=["POST"])
def open_account_submit(member_id):
    member = data.find_member(member_id)
    if member is None:
        return render_template("not_found.html", member_id=member_id), 200
    if member.status == "restricted":
        return render_template("forbidden.html", member_id=member_id), 200

    account_type = request.form.get("account_type", "").strip()
    deposit_raw = request.form.get("initial_deposit", "").strip()

    errors = []
    if account_type not in ("Savings", "Checking", "Money Market"):
        errors.append("Choose a valid sub-account type.")
    try:
        deposit_cents = int(round(float(deposit_raw) * 100))
        if deposit_cents < 500:
            errors.append("Initial deposit must be at least $5.00.")
    except (ValueError, TypeError):
        deposit_cents = None
        errors.append("Initial deposit must be a valid dollar amount.")

    if errors:
        return render_template("open_account.html", member=member, errors=errors), 200

    # Irreversible/risky step -> require an explicit confirmation click
    # rather than committing on the first submit.
    if request.form.get("confirmed") != "yes":
        return render_template(
            "confirm_open_account.html",
            member=member,
            account_type=account_type,
            initial_deposit=deposit_raw,
        )

    acct = data.open_sub_account(member_id, account_type, deposit_cents)
    return render_template("open_account_success.html", member=member, account=acct)


@app.route("/reauth", methods=["POST"])
def reauth():
    # Minimal "recoverable condition" handler: dismiss the timeout
    # interstitial and land back on the home/search screen.
    return redirect(url_for("home"))


@app.route("/verify-identity", methods=["POST"])
def verify_identity():
    session["verified"] = True
    member_id = request.form.get("member_id", "")
    return redirect(url_for("search", member_id=member_id))


if __name__ == "__main__":
    app.run(port=5055, debug=True)
