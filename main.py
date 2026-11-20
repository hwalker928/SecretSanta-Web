from flask import Flask, render_template, redirect, jsonify, abort
import redis
import random
import logging
import os
import datetime
import qrcode
import io
import base64

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Load configuration
config = {
    "port": int(os.getenv("APP_PORT", 8000)),
    "reroll_count": int(os.getenv("REROLL_COUNT", 0)),
    "admin_user": os.getenv("ADMIN_USER", "admin"),
    "names": [
        n.strip().lower() for n in os.getenv("VALID_NAMES", "").split(",") if n.strip()
    ],
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

# Optional: group definitions. Format: "group1:alice,bob;group2:carol,dave"
name_groups_raw = os.getenv("NAME_GROUPS", "")
name_groups = {}
if name_groups_raw:
    for group_spec in [g.strip() for g in name_groups_raw.split(";") if g.strip()]:
        if ":" in group_spec:
            group_name, members = group_spec.split(":", 1)
            for member in [m.strip() for m in members.split(",") if m.strip()]:
                name_groups[member.lower()] = group_name.strip()

# store in config for template access
config["name_groups"] = name_groups


print("Name groups loaded:", name_groups)

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


def pick_recipient(giver, exclude=()):
    """Choose a recipient for `giver`, or None if no safe choice exists.

    Must be called while holding the assignment lock. If the giver already has
    a recipient it is treated as released (reroll) and may be picked again;
    `exclude` can block specific names. A candidate is rejected if taking it would leave another
    waiting giver with nobody but themselves to give to.
    """
    names = set(config["names"])
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
    groups = config["name_groups"]
    giver_group = groups.get(giver)
    if giver_group:
        different = [c for c in candidates if groups.get(c) != giver_group]
        if different:
            candidates = different

    return random.choice(candidates) if candidates else None


@app.route("/")
def main():
    return render_template("index.html")


@app.route("/qrcodes")
def qrcodes():
    people = config["names"]
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
        config=config,
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

    names = [name.capitalize() for name in config["names"]]

    return render_template(
        "qr.html",
        recipient=recipient.capitalize(),
        names=names,
        config=config,
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

    names = [name.capitalize() for name in config["names"]]

    return render_template(
        "qr.html",
        recipient=recipient.capitalize(),
        names=names,
        config=config,
        song=config["use_songs"]
        and recipient != "Not assigned yet"
        and os.path.isfile(f"static/songs/{recipient.lower()}.mp3"),
    )


@app.route("/ssadmin")
def admin():
    names = [name.lower() for name in config["names"]]
    unassigned = []

    try:
        for name in names:
            recipient = redis_client.get(f"recipient:{name}")
            # Treat empty string or None as unassigned
            if not recipient:
                unassigned.append(name.capitalize())
    except redis.RedisError as e:
        logging.error(f"Redis error while building admin list: {e}")
        return render_template(
            "error.html", message="Internal server error. Please try again later."
        )

    return render_template(
        "admin.html",
        unassigned=unassigned,
        count=len(unassigned),
        config=config,
    )


@app.route("/rules/<firstname_base64>/<recipient_base64>")
def rules(firstname_base64, recipient_base64):
    firstname = bytes.fromhex(firstname_base64[::-1]).decode("utf-8")
    recipient = bytes.fromhex(recipient_base64[::-1]).decode("utf-8")

    return render_template(
        "rules.html",
        chosen_recipient_capital=recipient.capitalize(),
        config=config,
        firstname=firstname,
        reroll_limit=config["reroll_count"],
    )


@app.route("/name/<firstname>")
def name(firstname: str):
    firstname_lower = firstname.strip().lower()
    if firstname_lower not in config["names"]:
        return render_template(
            "error.html",
            message=(
                "Please make sure that you have spelled your name correctly. "
                f"Contact {config['admin_user']} if you believe there is a mistake."
            ),
        )

    try:
        with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
            # Check if the user has already been assigned a recipient
            if redis_client.get(f"recipient:{firstname_lower}"):
                return render_template(
                    "error.html",
                    message=f"You have already been assigned a recipient. Contact {config['admin_user']} for assistance.",
                )

            chosen_recipient = pick_recipient(firstname_lower)
            if not chosen_recipient:
                return render_template(
                    "error.html",
                    message="No available recipients left. Contact admin for assistance.",
                )

            redis_client.set(f"recipient:{firstname_lower}", chosen_recipient)
            redis_client.sadd("assigned_recipients", chosen_recipient)

        firstname_lower_base64 = firstname_lower.encode("utf-8").hex()[::-1]
        chosen_recipient_base64 = chosen_recipient.encode("utf-8").hex()[::-1]

        return redirect(f"/rules/{firstname_lower_base64}/{chosen_recipient_base64}")

    except redis.RedisError as e:
        logging.error(f"Redis error: {e}")
        return render_template(
            "error.html", message="Internal server error. Please try again later."
        )


@app.route("/reroll/<firstname>", methods=["POST"])
def reroll(firstname):
    if config["reroll_count"] == 0:
        return jsonify({"error": "Rerolls are disabled."})

    firstname_lower = firstname.strip().lower()
    if firstname_lower not in config["names"]:
        return jsonify(
            {
                "error": (
                    "Please make sure that you have spelled your name correctly. "
                    f"Contact {config['admin_user']} if you believe there is a mistake."
                )
            }
        )

    try:
        with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
            if get_int(f"rerolls:{firstname_lower}") >= config["reroll_count"]:
                return jsonify(
                    {
                        "error": f"You have reached the maximum number of rerolls ({config['reroll_count']})."
                    }
                )

            old_recipient = redis_client.get(f"recipient:{firstname_lower}")
            if not old_recipient:
                return jsonify(
                    {
                        "error": "You have not been assigned a recipient yet. Contact admin for assistance."
                    }
                )

            # The old recipient is released and may be picked again
            chosen_recipient = pick_recipient(firstname_lower)
            if not chosen_recipient:
                return jsonify(
                    {
                        "error": "No available recipients left. Contact admin for assistance."
                    }
                )

            redis_client.srem("assigned_recipients", old_recipient)
            redis_client.set(f"recipient:{firstname_lower}", chosen_recipient)
            redis_client.sadd("assigned_recipients", chosen_recipient)
            redis_client.incr(f"rerolls:{firstname_lower}")

        return jsonify({"newName": chosen_recipient.capitalize()})

    except redis.RedisError as e:
        logging.error(f"Redis error: {e}")
        return jsonify({"error": "Internal server error. Please try again later."})


# Initialise state without clobbering anything already stored (safe across
# restarts and across multiple gunicorn workers booting at once).
for _name in config["names"]:
    redis_client.setnx(f"recipient:{_name}", "")
    redis_client.setnx(f"rerolls:{_name}", 0)
redis_client.setnx("qr-active", 0)

print(f"Loaded {len(config['names'])} names.")
print(f"Loaded songs: {os.listdir('static/songs') if os.path.isdir('static/songs') else []}")


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", debug=False, port=config["port"])
    except Exception as e:
        logging.critical(f"Failed to start the application: {e}")
