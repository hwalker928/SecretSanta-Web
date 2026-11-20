from flask import Flask, render_template, redirect, jsonify, abort, request, Response, url_for
import redis
import random
import logging
import os
import datetime
import qrcode
import io
import base64
import functools
import hmac
import re
import secrets
from urllib.parse import urlparse

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Load configuration
config = {
    "port": int(os.getenv("APP_PORT", 8000)),
    "reroll_count": int(os.getenv("REROLL_COUNT", 0)),
    "admin_user": os.getenv("ADMIN_USER", "admin"),
    "admin_password": os.getenv("ADMIN_PASSWORD", ""),
    "year": os.getenv("YEAR", datetime.datetime.now().year),
    "budget": os.getenv("BUDGET", "10"),
    "url": os.getenv("URL", "http://localhost:8000"),
    "rules": os.getenv("RULES", "").split(","),
    "qr_toggle_url": os.getenv("QR_TOGGLE_URL", "qr-toggle"),
    "giving_day": int(os.getenv("GIVING_DAY", 25)),
    "giving_month": int(os.getenv("GIVING_MONTH", 12)),
    "use_songs": os.getenv("USE_SONGS", "false") == "true",
    "use_qr_checks": os.getenv("DISABLE_QR_CHECKS", "false") == "false",
}

# VALID_NAMES / NAME_GROUPS only seed the people list the first time the app
# starts with an empty database. After that, people and groups are managed on
# the admin page. NAME_GROUPS format: "group1:alice,bob;group2:carol,dave"
seed_people = {
    n.strip().lower(): ""
    for n in os.getenv("VALID_NAMES", "").split(",")
    if n.strip()
}
for group_spec in [g.strip() for g in os.getenv("NAME_GROUPS", "").split(";") if g.strip()]:
    if ":" in group_spec:
        group_name, members = group_spec.split(":", 1)
        for member in [m.strip().lower() for m in members.split(",") if m.strip()]:
            if member in seed_people:
                seed_people[member] = group_name.strip().lower()

app = Flask(__name__)


# Connect to Redis
def connect_redis():
    try:
        return redis.StrictRedis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=0,
            decode_responses=True,
        )
    except redis.ConnectionError as e:
        logging.error(f"Redis connection failed: {e}")
        raise


redis_client = connect_redis()

ASSIGN_LOCK = "assign-lock"


def get_int(key, default=0):
    """Read an integer from Redis, tolerating missing/invalid values."""
    try:
        return int(redis_client.get(key))
    except (TypeError, ValueError):
        return default


def get_people():
    """All participants as {lowercase name: lowercase group or ''}."""
    return dict(redis_client.hgetall("people"))


def ensure_person_state(name):
    """Create per-person state (recipient, reroll count, private link token)."""
    redis_client.setnx(f"recipient:{name}", "")
    redis_client.setnx(f"rerolls:{name}", 0)
    redis_client.setnx(f"token:{name}", secrets.token_urlsafe(12))
    redis_client.set(f"tokenowner:{redis_client.get(f'token:{name}')}", name)


def pick_recipient(giver, exclude=()):
    """Choose a recipient for `giver`, or None if no safe choice exists.

    Must be called while holding the assignment lock. If the giver already has
    a recipient it is treated as released (reroll) and may be picked again;
    `exclude` can block specific names. A candidate is rejected if taking it would leave another
    waiting giver with nobody but themselves to give to.
    """
    people = get_people()
    names = set(people)
    assigned = set(redis_client.smembers("assigned_recipients"))
    current = redis_client.get(f"recipient:{giver}")
    if current:
        assigned.discard(current)

    pool = names - assigned - {giver} - set(exclude)
    waiting = [n for n in names - {giver} if not redis_client.get(f"recipient:{n}")]

    def leaves_someone_stuck(candidate):
        remaining = names - assigned - {candidate}
        return any(remaining <= {w} for w in waiting)

    candidates = [c for c in pool if not leaves_someone_stuck(c)]

    # Prefer recipients from a different group where possible
    giver_group = people.get(giver)
    if giver_group:
        different = [c for c in candidates if people.get(c) != giver_group]
        if different:
            candidates = different

    return random.choice(candidates) if candidates else None


@app.context_processor
def inject_config():
    # Budget can be changed on the admin page; fall back to the BUDGET env var
    try:
        budget = redis_client.get("setting:budget") or config["budget"]
    except redis.RedisError:
        budget = config["budget"]
    return {"config": {**config, "budget": budget}}


@app.route("/")
def main():
    return render_template("index.html")


@app.route("/qrcodes")
def qrcodes():
    people = sorted(get_people())
    people_dict = []

    for person in people:
        person_base64_reversed = person.encode("utf-8").hex()[::-1]

        qr = qrcode.make(f"{config['url']}/qrscan/{person_base64_reversed}", border=1)
        buffer = io.BytesIO()
        qr.save(buffer, format="PNG")
        qr_img_bytes = base64.b64encode(buffer.getvalue()).decode()
        buffer.close()

        people_dict.append(
            {
                "name": person.capitalize(),
                "qr": qr_img_bytes,
            }
        )

    return render_template(
        "qrcodes.html",
        people_dict=people_dict,
        active=get_int("qr-active") == 1,
    )


@app.route(f"/{config['qr_toggle_url']}")
def qr_toggle():
    if get_int("qr-active") == 1:
        redis_client.set("qr-active", 0)
    else:
        redis_client.set("qr-active", 1)
    return redirect(f"/qrcodes")


@app.route("/qrscan/<person_base64>")
def qrscan(person_base64):
    if get_int("qr-active") == 0 and config["use_qr_checks"]:
        return redirect("https://cdn.mtdv.me/video/last_rickmas.mp4")

    if (
        datetime.datetime.now().month != config["giving_month"]
        or datetime.datetime.now().day != config["giving_day"]
    ) and config["use_qr_checks"]:
        return redirect("https://cdn.mtdv.me/video/feliz_navidad.mp4")

    person = bytes.fromhex(person_base64[::-1]).decode("utf-8")

    recipient = redis_client.get(f"recipient:{person.lower()}")

    if recipient:
        recipient = recipient.capitalize()
    else:
        recipient = "Not assigned yet"

    names = [name.capitalize() for name in sorted(get_people())]

    return render_template(
        "qr.html",
        recipient=recipient.capitalize(),
        names=names,
        song=config["use_songs"]
        and recipient != "Not assigned yet"
        and os.path.isfile(f"static/songs/{recipient.lower()}.mp3"),
    )


@app.route("/qrscan-test/<recipient>")
def qrscantest(recipient):
    if config["use_qr_checks"]:
        abort(404)  # test route only available with DISABLE_QR_CHECKS=true

    if recipient:
        recipient = recipient.capitalize()
    else:
        recipient = "Not assigned yet"

    names = [name.capitalize() for name in sorted(get_people())]

    return render_template(
        "qr.html",
        recipient=recipient.capitalize(),
        names=names,
        song=config["use_songs"]
        and recipient != "Not assigned yet"
        and os.path.isfile(f"static/songs/{recipient.lower()}.mp3"),
    )


def same_origin():
    """Reject cross-site POSTs (browsers resend basic-auth credentials)."""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True
    host = urlparse(source).netloc
    return host in {request.host, urlparse(config["url"]).netloc}


def require_admin(view):
    """HTTP basic auth using ADMIN_PASSWORD (any username). Disabled if unset."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        password = config["admin_password"]
        if not password:
            return (
                "The admin page is disabled. Set the ADMIN_PASSWORD environment "
                "variable to enable it.",
                403,
            )
        if request.method == "POST" and not same_origin():
            abort(403)
        auth = request.authorization
        if not auth or not hmac.compare_digest(
            (auth.password or "").encode("utf-8"), password.encode("utf-8")
        ):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Secret Santa admin"'},
            )
        return view(*args, **kwargs)

    return wrapper


def admin_redirect(message, ok=True):
    return redirect(url_for("admin", msg=message, ok=1 if ok else 0))


def clean_name(raw):
    name = (raw or "").strip().lower()
    return name if re.fullmatch(r"[\w' -]{1,32}", name) else None


def clean_group(raw):
    return (raw or "").strip().lower()[:32]


@app.route("/ssadmin")
@require_admin
def admin():
    people = []

    try:
        for name, group in sorted(get_people().items()):
            people.append(
                {
                    "name": name,
                    "display": name.capitalize(),
                    "group": group,
                    "assigned": bool(redis_client.get(f"recipient:{name}")),
                    "link": f"{config['url'].rstrip('/')}/u/{redis_client.get(f'token:{name}')}",
                }
            )
    except redis.RedisError as e:
        logging.error(f"Redis error while building admin list: {e}")
        return render_template(
            "error.html", message="Internal server error. Please try again later."
        )

    return render_template(
        "admin.html",
        people=people,
        groups=sorted({p["group"] for p in people if p["group"]}),
        count=sum(1 for p in people if not p["assigned"]),
        any_drawn=any(p["assigned"] for p in people),
        message=request.args.get("msg"),
        message_ok=request.args.get("ok") == "1",
    )


@app.route("/ssadmin/budget", methods=["POST"])
@require_admin
def admin_set_budget():
    budget = (request.form.get("budget") or "").strip()
    if len(budget) > 20:
        return admin_redirect("Budget must be 20 characters or fewer.", False)
    if budget:
        redis_client.set("setting:budget", budget)
        return admin_redirect(f"Budget set to {budget}.")
    redis_client.delete("setting:budget")  # blank = back to the BUDGET env default
    return admin_redirect(f"Budget reset to the default ({config['budget']}).")


@app.route("/ssadmin/people", methods=["POST"])
@require_admin
def admin_add_person():
    name = clean_name(request.form.get("name"))
    if not name:
        return admin_redirect(
            "Names must be 1-32 characters: letters, numbers, spaces, - or '.", False
        )

    with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
        if not redis_client.hsetnx("people", name, clean_group(request.form.get("group"))):
            return admin_redirect(f"{name.capitalize()} is already in the list.", False)
        ensure_person_state(name)

    message = f"Added {name.capitalize()}."
    if redis_client.scard("assigned_recipients"):
        message += " Draws have already started, so they may not be picked by anyone; reset the draws if that's a problem."
    return admin_redirect(message)


@app.route("/ssadmin/people/<name>/group", methods=["POST"])
@require_admin
def admin_set_group(name):
    if not redis_client.hexists("people", name):
        abort(404)
    redis_client.hset("people", name, clean_group(request.form.get("group")))
    return admin_redirect(f"Updated {name.capitalize()}'s group.")


@app.route("/ssadmin/people/<name>/remove", methods=["POST"])
@require_admin
def admin_remove_person(name):
    with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
        if not redis_client.hexists("people", name):
            abort(404)
        if redis_client.get(f"recipient:{name}") or name in redis_client.smembers(
            "assigned_recipients"
        ):
            return admin_redirect(
                f"{name.capitalize()} has already drawn or been drawn by someone. Reset the draws before removing them.",
                False,
            )
        token = redis_client.get(f"token:{name}")
        redis_client.hdel("people", name)
        redis_client.delete(f"recipient:{name}", f"rerolls:{name}", f"token:{name}")
        if token:
            redis_client.delete(f"tokenowner:{token}")
    return admin_redirect(f"Removed {name.capitalize()}.")


@app.route("/ssadmin/reset", methods=["POST"])
@require_admin
def admin_reset_draws():
    """Clear every draw and reroll count. People, groups and links are kept."""
    with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
        redis_client.delete("assigned_recipients")
        for name in get_people():
            redis_client.set(f"recipient:{name}", "")
            redis_client.set(f"rerolls:{name}", 0)
    return admin_redirect("All draws have been reset.")


def person_for_token(token):
    """Return the (lowercase) name that owns this private link, or abort(404)."""
    name = redis_client.get(f"tokenowner:{token}")
    if not name or name not in get_people():
        abort(404)
    return name


@app.route("/u/<token>")
def welcome(token):
    """Landing page for a private link. GET never assigns anything, so link
    previews/scrapers (WhatsApp etc.) can't trigger a draw."""
    name = person_for_token(token)
    return render_template(
        "welcome.html",
        token=token,
        person=name.capitalize(),
        already_drawn=bool(redis_client.get(f"recipient:{name}")),
    )


@app.route("/u/<token>/draw", methods=["POST"])
def draw(token):
    name = person_for_token(token)

    try:
        with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
            # Idempotent: if already drawn just show the existing recipient
            if not redis_client.get(f"recipient:{name}"):
                chosen_recipient = pick_recipient(name)
                if not chosen_recipient:
                    return render_template(
                        "error.html",
                        message="No available recipients left. Contact admin for assistance.",
                    )
                redis_client.set(f"recipient:{name}", chosen_recipient)
                redis_client.sadd("assigned_recipients", chosen_recipient)
    except redis.RedisError as e:
        logging.error(f"Redis error: {e}")
        return render_template(
            "error.html", message="Internal server error. Please try again later."
        )

    return redirect(f"/u/{token}/rules", code=303)


@app.route("/u/<token>/rules")
def rules(token):
    name = person_for_token(token)

    try:
        recipient = redis_client.get(f"recipient:{name}")
        rerolls_used = get_int(f"rerolls:{name}")
    except redis.RedisError as e:
        logging.error(f"Redis error: {e}")
        return render_template(
            "error.html", message="Internal server error. Please try again later."
        )

    if not recipient:
        return redirect(f"/u/{token}")

    return render_template(
        "rules.html",
        chosen_recipient_capital=recipient.capitalize(),
        token=token,
        reroll_limit=config["reroll_count"],
        rerolls_left=max(0, config["reroll_count"] - rerolls_used),
    )


@app.route("/u/<token>/reroll", methods=["POST"])
def reroll(token):
    name = person_for_token(token)

    if config["reroll_count"] == 0:
        return jsonify({"error": "Rerolls are disabled."})

    try:
        with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
            if get_int(f"rerolls:{name}") >= config["reroll_count"]:
                return jsonify(
                    {
                        "error": f"You have reached the maximum number of rerolls ({config['reroll_count']})."
                    }
                )

            old_recipient = redis_client.get(f"recipient:{name}")
            if not old_recipient:
                return jsonify(
                    {
                        "error": "You have not been assigned a recipient yet. Contact admin for assistance."
                    }
                )

            # The old recipient is released and may be picked again
            chosen_recipient = pick_recipient(name)
            if not chosen_recipient:
                return jsonify(
                    {
                        "error": "No available recipients left. Contact admin for assistance."
                    }
                )

            redis_client.srem("assigned_recipients", old_recipient)
            redis_client.set(f"recipient:{name}", chosen_recipient)
            redis_client.sadd("assigned_recipients", chosen_recipient)
            redis_client.incr(f"rerolls:{name}")

        return jsonify({"newName": chosen_recipient.capitalize()})

    except redis.RedisError as e:
        logging.error(f"Redis error: {e}")
        return jsonify({"error": "Internal server error. Please try again later."})


# Initialise state without clobbering anything already stored (safe across
# restarts and across multiple gunicorn workers booting at once).
if not redis_client.exists("people"):
    for _name, _group in seed_people.items():
        redis_client.hsetnx("people", _name, _group)
for _name in get_people():
    ensure_person_state(_name)
redis_client.setnx("qr-active", 0)

print(f"Loaded {len(get_people())} names.")
print(f"Loaded songs: {os.listdir('static/songs') if os.path.isdir('static/songs') else []}")


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", debug=False, port=config["port"])
    except Exception as e:
        logging.critical(f"Failed to start the application: {e}")
