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


# Connect to Valkey (wire-compatible with Redis, so the redis client library is used).
# REDIS_HOST / REDIS_PORT are still accepted for existing deployments.
def connect_redis():
    try:
        return redis.StrictRedis(
            host=os.getenv("VALKEY_HOST") or os.getenv("REDIS_HOST", "valkey"),
            port=int(os.getenv("VALKEY_PORT") or os.getenv("REDIS_PORT", 6379)),
            db=0,
            decode_responses=True,
        )
    except redis.ConnectionError as e:
        logging.error(f"Redis connection failed: {e}")
        raise


redis_client = connect_redis()

ASSIGN_LOCK = "assign-lock"
GAME_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def gk(game, *parts):
    """Redis key for something belonging to a game, e.g. g:family:recipient:alice."""
    return ":".join(("g", game) + parts)


def get_int(key, default=0):
    """Read an integer from Redis, tolerating missing/invalid values."""
    try:
        return int(redis_client.get(key))
    except (TypeError, ValueError):
        return default


def clean_game_id(raw):
    game = (raw or "").strip().lower()
    return game if GAME_ID_RE.fullmatch(game) else None


DEFAULT_GAME = clean_game_id(os.getenv("GAME_ID", "default")) or "default"


def require_game(game):
    if not redis_client.sismember("games", game):
        abort(404)
    return game


def only_game():
    """The game's id if exactly one exists (keeps pre-multi-game URLs working)."""
    games = redis_client.smembers("games")
    return next(iter(games)) if len(games) == 1 else None


def get_budget(game):
    return redis_client.get(gk(game, "budget")) or config["budget"]


def get_people(game):
    """A game's participants as {lowercase name: lowercase group or ''}."""
    return dict(redis_client.hgetall(gk(game, "people")))


def ensure_person_state(game, name):
    """Create per-person state (recipient, reroll count, private link token)."""
    redis_client.setnx(gk(game, "recipient", name), "")
    redis_client.setnx(gk(game, "rerolls", name), 0)
    redis_client.setnx(gk(game, "token", name), secrets.token_urlsafe(12))
    token = redis_client.get(gk(game, "token", name))
    redis_client.set(f"tokenowner:{token}", f"{game}|{name}")


def pick_recipient(game, giver, exclude=()):
    """Choose a recipient for `giver` in `game`, or None if no safe choice exists.

    Must be called while holding the assignment lock. If the giver already has
    a recipient it is treated as released (reroll) and may be picked again;
    `exclude` can block specific names. A candidate is rejected if taking it
    would leave another waiting giver with nobody but themselves to give to.
    """
    people = get_people(game)
    names = set(people)
    assigned = set(redis_client.smembers(gk(game, "assigned")))
    current = redis_client.get(gk(game, "recipient", giver))
    if current:
        assigned.discard(current)

    pool = names - assigned - {giver} - set(exclude)
    waiting = [
        n for n in names - {giver} if not redis_client.get(gk(game, "recipient", n))
    ]

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


def migrate_legacy_data():
    """Move pre-multi-game data (un-namespaced keys) into a single game."""
    game = DEFAULT_GAME
    with redis_client.lock("migrate-lock", timeout=30, blocking_timeout=30):
        if redis_client.exists("games") or not redis_client.exists("people"):
            return

        def move(old, new):
            if redis_client.exists(old):
                redis_client.rename(old, new)

        people = dict(redis_client.hgetall("people"))
        for name in people:
            for part in ("recipient", "rerolls", "token"):
                move(f"{part}:{name}", gk(game, part, name))
            token = redis_client.get(gk(game, "token", name))
            if token:
                redis_client.set(f"tokenowner:{token}", f"{game}|{name}")
        move("assigned_recipients", gk(game, "assigned"))
        move("qr-active", gk(game, "qr-active"))
        move("setting:budget", gk(game, "budget"))
        move("people", gk(game, "people"))
        redis_client.sadd("games", game)
        logging.info(f"Migrated existing data into game '{game}'")


@app.context_processor
def inject_config():
    return {"config": config}


@app.route("/")
def main():
    return render_template("index.html")


@app.route("/qrcodes", defaults={"game": None})
@app.route("/qrcodes/<game>")
def qrcodes(game):
    game = require_game(game or only_game() or "")
    people = sorted(get_people(game))
    people_dict = []

    for person in people:
        person_base64_reversed = person.encode("utf-8").hex()[::-1]

        qr = qrcode.make(
            f"{config['url']}/qrscan/{game}/{person_base64_reversed}", border=1
        )
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
        game=game,
        people_dict=people_dict,
        active=get_int(gk(game, "qr-active")) == 1,
    )


@app.route(f"/{config['qr_toggle_url']}", defaults={"game": None})
@app.route(f"/{config['qr_toggle_url']}/<game>")
def qr_toggle(game):
    game = require_game(game or only_game() or "")
    key = gk(game, "qr-active")
    redis_client.set(key, 0 if get_int(key) == 1 else 1)
    return redirect(url_for("qrcodes", game=game))


def render_qr_reveal(game, recipient):
    names = [name.capitalize() for name in sorted(get_people(game))]
    recipient = recipient.capitalize() if recipient else "Not assigned yet"

    return render_template(
        "qr.html",
        recipient=recipient,
        names=names,
        song=config["use_songs"]
        and recipient != "Not assigned yet"
        and os.path.isfile(f"static/songs/{recipient.lower()}.mp3"),
    )


@app.route("/qrscan/<person_base64>", defaults={"game": None})
@app.route("/qrscan/<game>/<person_base64>")
def qrscan(game, person_base64):
    game = require_game(game or only_game() or "")

    if get_int(gk(game, "qr-active")) == 0 and config["use_qr_checks"]:
        return redirect("https://cdn.mtdv.me/video/last_rickmas.mp4")

    if (
        datetime.datetime.now().month != config["giving_month"]
        or datetime.datetime.now().day != config["giving_day"]
    ) and config["use_qr_checks"]:
        return redirect("https://cdn.mtdv.me/video/feliz_navidad.mp4")

    try:
        person = bytes.fromhex(person_base64[::-1]).decode("utf-8")
    except ValueError:
        abort(404)

    return render_qr_reveal(game, redis_client.get(gk(game, "recipient", person.lower())))


@app.route("/qrscan-test/<game>/<recipient>")
def qrscantest(game, recipient):
    if config["use_qr_checks"]:
        abort(404)  # test route only available with DISABLE_QR_CHECKS=true

    return render_qr_reveal(require_game(game), recipient)


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


def admin_redirect(game, message, ok=True):
    return redirect(url_for("admin_game", game=game, msg=message, ok=1 if ok else 0))


def clean_name(raw):
    name = (raw or "").strip().lower()
    return name if re.fullmatch(r"[\w' -]{1,32}", name) else None


def clean_group(raw):
    return (raw or "").strip().lower()[:32]


@app.route("/ssadmin")
@require_admin
def admin():
    games = []
    for game in sorted(redis_client.smembers("games")):
        people = get_people(game)
        games.append(
            {
                "id": game,
                "people": len(people),
                "drawn": sum(
                    1 for n in people if redis_client.get(gk(game, "recipient", n))
                ),
            }
        )

    return render_template(
        "admin_games.html",
        games=games,
        message=request.args.get("msg"),
        message_ok=request.args.get("ok") == "1",
    )


@app.route("/ssadmin", methods=["POST"])
@require_admin
def admin_create_game():
    game = clean_game_id(request.form.get("game"))
    if not game:
        return redirect(
            url_for(
                "admin",
                msg="Game IDs are 1-32 characters: lowercase letters, numbers and hyphens (e.g. family).",
                ok=0,
            )
        )
    if not redis_client.sadd("games", game):
        return redirect(url_for("admin", msg=f"'{game}' already exists.", ok=0))
    return admin_redirect(game, f"Created game '{game}'. Add some people to get started.")


@app.route("/ssadmin/<game>")
@require_admin
def admin_game(game):
    require_game(game)
    people = []

    try:
        for name, group in sorted(get_people(game).items()):
            people.append(
                {
                    "name": name,
                    "display": name.capitalize(),
                    "group": group,
                    "assigned": bool(redis_client.get(gk(game, "recipient", name))),
                    "link": f"{config['url'].rstrip('/')}/u/{redis_client.get(gk(game, 'token', name))}",
                }
            )
    except redis.RedisError as e:
        logging.error(f"Redis error while building admin list: {e}")
        return render_template(
            "error.html", message="Internal server error. Please try again later."
        )

    return render_template(
        "admin.html",
        game=game,
        budget=get_budget(game),
        people=people,
        groups=sorted({p["group"] for p in people if p["group"]}),
        count=sum(1 for p in people if not p["assigned"]),
        any_drawn=any(p["assigned"] for p in people),
        message=request.args.get("msg"),
        message_ok=request.args.get("ok") == "1",
    )


@app.route("/ssadmin/<game>/delete", methods=["POST"])
@require_admin
def admin_delete_game(game):
    require_game(game)
    if (request.form.get("confirm") or "").strip().lower() != game:
        return admin_redirect(
            game, "Type the game's ID to confirm deleting it.", False
        )

    with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
        for name in get_people(game):
            token = redis_client.get(gk(game, "token", name))
            redis_client.delete(
                gk(game, "recipient", name),
                gk(game, "rerolls", name),
                gk(game, "token", name),
            )
            if token:
                redis_client.delete(f"tokenowner:{token}")
        redis_client.delete(
            gk(game, "people"),
            gk(game, "assigned"),
            gk(game, "qr-active"),
            gk(game, "budget"),
        )
        redis_client.srem("games", game)
    return redirect(url_for("admin", msg=f"Deleted game '{game}'.", ok=1))


@app.route("/ssadmin/<game>/budget", methods=["POST"])
@require_admin
def admin_set_budget(game):
    require_game(game)
    budget = (request.form.get("budget") or "").strip()
    if len(budget) > 20:
        return admin_redirect(game, "Budget must be 20 characters or fewer.", False)
    if budget:
        redis_client.set(gk(game, "budget"), budget)
        return admin_redirect(game, f"Budget set to {budget}.")
    redis_client.delete(gk(game, "budget"))  # blank = back to the BUDGET env default
    return admin_redirect(game, f"Budget reset to the default ({config['budget']}).")


@app.route("/ssadmin/<game>/people", methods=["POST"])
@require_admin
def admin_add_person(game):
    require_game(game)
    name = clean_name(request.form.get("name"))
    if not name:
        return admin_redirect(
            game,
            "Names must be 1-32 characters: letters, numbers, spaces, - or '.",
            False,
        )

    with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
        if not redis_client.hsetnx(
            gk(game, "people"), name, clean_group(request.form.get("group"))
        ):
            return admin_redirect(
                game, f"{name.capitalize()} is already in the list.", False
            )
        ensure_person_state(game, name)

    message = f"Added {name.capitalize()}."
    if redis_client.scard(gk(game, "assigned")):
        message += " Draws have already started, so they may not be picked by anyone; reset the draws if that's a problem."
    return admin_redirect(game, message)


@app.route("/ssadmin/<game>/people/<name>/group", methods=["POST"])
@require_admin
def admin_set_group(game, name):
    require_game(game)
    if not redis_client.hexists(gk(game, "people"), name):
        abort(404)
    redis_client.hset(gk(game, "people"), name, clean_group(request.form.get("group")))
    return admin_redirect(game, f"Updated {name.capitalize()}'s group.")


@app.route("/ssadmin/<game>/people/<name>/remove", methods=["POST"])
@require_admin
def admin_remove_person(game, name):
    require_game(game)
    with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
        if not redis_client.hexists(gk(game, "people"), name):
            abort(404)
        if redis_client.get(gk(game, "recipient", name)) or name in redis_client.smembers(
            gk(game, "assigned")
        ):
            return admin_redirect(
                game,
                f"{name.capitalize()} has already drawn or been drawn by someone. Reset the draws before removing them.",
                False,
            )
        token = redis_client.get(gk(game, "token", name))
        redis_client.hdel(gk(game, "people"), name)
        redis_client.delete(
            gk(game, "recipient", name),
            gk(game, "rerolls", name),
            gk(game, "token", name),
        )
        if token:
            redis_client.delete(f"tokenowner:{token}")
    return admin_redirect(game, f"Removed {name.capitalize()}.")


@app.route("/ssadmin/<game>/reset", methods=["POST"])
@require_admin
def admin_reset_draws(game):
    """Clear every draw and reroll count. People, groups and links are kept."""
    require_game(game)
    with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
        redis_client.delete(gk(game, "assigned"))
        for name in get_people(game):
            redis_client.set(gk(game, "recipient", name), "")
            redis_client.set(gk(game, "rerolls", name), 0)
    return admin_redirect(game, "All draws have been reset.")


def person_for_token(token):
    """Return (game, lowercase name) that owns this private link, or abort(404)."""
    owner = redis_client.get(f"tokenowner:{token}") or ""
    game, _, name = owner.partition("|")
    if not game or not redis_client.hexists(gk(game, "people"), name):
        abort(404)
    return game, name


@app.route("/u/<token>")
def welcome(token):
    """Landing page for a private link. GET never assigns anything, so link
    previews/scrapers (WhatsApp etc.) can't trigger a draw."""
    game, name = person_for_token(token)
    return render_template(
        "welcome.html",
        token=token,
        game=game,
        budget=get_budget(game),
        person=name.capitalize(),
        already_drawn=bool(redis_client.get(gk(game, "recipient", name))),
    )


@app.route("/u/<token>/draw", methods=["POST"])
def draw(token):
    game, name = person_for_token(token)

    try:
        with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
            # Idempotent: if already drawn just show the existing recipient
            if not redis_client.get(gk(game, "recipient", name)):
                chosen_recipient = pick_recipient(game, name)
                if not chosen_recipient:
                    return render_template(
                        "error.html",
                        message="No available recipients left. Contact admin for assistance.",
                    )
                redis_client.set(gk(game, "recipient", name), chosen_recipient)
                redis_client.sadd(gk(game, "assigned"), chosen_recipient)
    except redis.RedisError as e:
        logging.error(f"Redis error: {e}")
        return render_template(
            "error.html", message="Internal server error. Please try again later."
        )

    return redirect(f"/u/{token}/rules", code=303)


@app.route("/u/<token>/rules")
def rules(token):
    game, name = person_for_token(token)

    try:
        recipient = redis_client.get(gk(game, "recipient", name))
        rerolls_used = get_int(gk(game, "rerolls", name))
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
        budget=get_budget(game),
        reroll_limit=config["reroll_count"],
        rerolls_left=max(0, config["reroll_count"] - rerolls_used),
    )


@app.route("/u/<token>/reroll", methods=["POST"])
def reroll(token):
    game, name = person_for_token(token)

    if config["reroll_count"] == 0:
        return jsonify({"error": "Rerolls are disabled."})

    try:
        with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
            if get_int(gk(game, "rerolls", name)) >= config["reroll_count"]:
                return jsonify(
                    {
                        "error": f"You have reached the maximum number of rerolls ({config['reroll_count']})."
                    }
                )

            old_recipient = redis_client.get(gk(game, "recipient", name))
            if not old_recipient:
                return jsonify(
                    {
                        "error": "You have not been assigned a recipient yet. Contact admin for assistance."
                    }
                )

            # The old recipient is released and may be picked again
            chosen_recipient = pick_recipient(game, name)
            if not chosen_recipient:
                return jsonify(
                    {
                        "error": "No available recipients left. Contact admin for assistance."
                    }
                )

            redis_client.srem(gk(game, "assigned"), old_recipient)
            redis_client.set(gk(game, "recipient", name), chosen_recipient)
            redis_client.sadd(gk(game, "assigned"), chosen_recipient)
            redis_client.incr(gk(game, "rerolls", name))

        return jsonify({"newName": chosen_recipient.capitalize()})

    except redis.RedisError as e:
        logging.error(f"Redis error: {e}")
        return jsonify({"error": "Internal server error. Please try again later."})


# Initialise state without clobbering anything already stored (safe across
# restarts and across multiple gunicorn workers booting at once).
migrate_legacy_data()
if not redis_client.exists("games") and seed_people:
    # Fresh install: VALID_NAMES / NAME_GROUPS seed a first game
    redis_client.sadd("games", DEFAULT_GAME)
    for _name, _group in seed_people.items():
        redis_client.hsetnx(gk(DEFAULT_GAME, "people"), _name, _group)
for _game in redis_client.smembers("games"):
    for _name in get_people(_game):
        ensure_person_state(_game, _name)
    redis_client.setnx(gk(_game, "qr-active"), 0)

print(f"Loaded games: {sorted(redis_client.smembers('games'))}")
print(f"Loaded songs: {os.listdir('static/songs') if os.path.isdir('static/songs') else []}")


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", debug=False, port=config["port"])
    except Exception as e:
        logging.critical(f"Failed to start the application: {e}")
