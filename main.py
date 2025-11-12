from flask import Flask, render_template, redirect, jsonify
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
    "names": os.getenv("VALID_NAMES", "").split(","),
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
        active=int(redis_client.get("qr-active")) == 1,
    )


@app.route(f"/{config['qr_toggle_url']}")
def qr_toggle():
    if int(redis_client.get("qr-active")) == 1:
        redis_client.set("qr-active", 0)
    else:
        redis_client.set("qr-active", 1)
    return redirect(f"/qrcodes")


@app.route("/qrscan/<person_base64>")
def qrscan(person_base64):
    if int(redis_client.get("qr-active")) == 0 and config["use_qr_checks"]:
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
    firstname_lower = firstname.lower()
    if firstname_lower not in config["names"]:
        return render_template(
            "error.html",
            message=(
                "Please make sure that you have spelled your name correctly. "
                f"Contact {config['admin_user']} if you believe there is a mistake."
            ),
        )

    try:
        # Check if the user has already been assigned a recipient
        recipient = redis_client.get(f"recipient:{firstname_lower}")
        if recipient:
            return render_template(
                "error.html",
                message=f"You have already been assigned a recipient. Contact {config['admin_user']} for assistance.",
            )

        # Get available recipients
        all_names = set(config["names"])
        assigned_recipients = set(redis_client.smembers("assigned_recipients"))
        possible_recipients = list(all_names - assigned_recipients - {firstname_lower})

        # Prefer recipients from different groups. NAME_GROUPS env format:
        # "group1:alice,bob;group2:carol,dave"
        first_group = config.get("name_groups", {}).get(firstname_lower)
        if first_group == "grouped":
            different_group = [n for n in possible_recipients if config.get("name_groups", {}).get(n) != first_group]
            if different_group:
                available_recipients = different_group
            else:
                # no other options, allow same-group
                available_recipients = possible_recipients
        else:
            # if user has no group defined, just use possible recipients
            available_recipients = possible_recipients

        if not available_recipients:
            return render_template(
                "error.html",
                message="No available recipients left. Contact admin for assistance.",
            )

        chosen_recipient = random.choice(available_recipients)

        # Assign the recipient
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

    firstname_lower = firstname.lower()
    if firstname_lower not in config["names"]:
        return jsonify(
            {
                "error": (
                    "Please make sure that you have spelled your name correctly. "
                    f"Contact {config['admin_user']} if you believe there is a mistake."
                )
            }
        )

    if int(redis_client.get(f"rerolls:{firstname_lower}")) >= config["reroll_count"]:
        return jsonify(
            {
                "error": f"You have reached the maximum number of rerolls ({config['reroll_count']})."
            }
        )

    try:
        # Remove the current assignment
        old_recipient = redis_client.get(f"recipient:{firstname_lower}")
        if old_recipient:
            redis_client.srem("assigned_recipients", old_recipient)
        else:
            return jsonify(
                {
                    "error": "You have not been assigned a recipient yet. Contact admin for assistance."
                }
            )

        # Get new available recipients
        all_names = set(config["names"])
        assigned_recipients = set(redis_client.smembers("assigned_recipients"))
        possible_recipients = list(all_names - assigned_recipients - {firstname_lower})

        # Prefer recipients from different groups where possible
        first_group = config.get("name_groups", {}).get(firstname_lower)
        if first_group == "grouped":
            different_group = [n for n in possible_recipients if config.get("name_groups", {}).get(n) != first_group]
            if different_group:
                available_recipients = different_group
            else:
                available_recipients = possible_recipients
        else:
            available_recipients = possible_recipients

        if not available_recipients:
            return jsonify(
                {"error": "No available recipients left. Contact admin for assistance."}
            )

        chosen_recipient = random.choice(available_recipients)

        # Assign the new recipient
        redis_client.set(f"recipient:{firstname_lower}", chosen_recipient)
        redis_client.sadd("assigned_recipients", chosen_recipient)
        redis_client.incr(f"rerolls:{firstname_lower}")

        return jsonify({"newName": chosen_recipient.capitalize()})

    except redis.RedisError as e:
        logging.error(f"Redis error: {e}")
        return render_template(
            "error.html", message="Internal server error. Please try again later."
        )

# Initialize the recipient assignments in Redis if not already done
if not redis_client.exists("assigned_recipients"):
    redis_client.delete("assigned_recipients")
    for name in config["names"]:
        redis_client.set(f"recipient:{name.lower()}", "")
        redis_client.set(f"rerolls:{name.lower()}", 0)

redis_client.set("qr-active", 0)

print(f"Loaded {len(config['names'])} names.")
print(f"Loaded songs: {os.listdir('static/songs')}")


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", debug=False, port=config["port"])
    except Exception as e:
        logging.critical(f"Failed to start the application: {e}")
