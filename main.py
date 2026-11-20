from flask import Flask, render_template, redirect, jsonify, abort, request, Response, url_for, g
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
from werkzeug.security import check_password_hash, generate_password_hash

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Fixed (non-interactive) configuration. Everything else is configured on the
# first-run setup page and in the admin settings; see get_config().
config = {
    "port": int(os.getenv("APP_PORT", 8000)),
    "year": os.getenv("YEAR", datetime.datetime.now().year),
    "use_qr_checks": os.getenv("DISABLE_QR_CHECKS", "false") == "false",
}

# Settings that can be configured in the app. An environment variable, if set,
# overrides the stored value (and locks the field in the UI).
SETTING_ENV = {
    "admin_user": "ADMIN_USER",
    "url": "URL",
    "budget": "BUDGET",
    "rules": "RULES",
    "reroll_count": "REROLL_COUNT",
    "giving_day": "GIVING_DAY",
    "giving_month": "GIVING_MONTH",
    "qr_toggle_url": "QR_TOGGLE_URL",
    "use_songs": "USE_SONGS",
}
SETTING_DEFAULTS = {
    "admin_user": "admin",
    "url": "",
    "budget": "10",
    "rules": "",
    "reroll_count": "0",
    "giving_day": "25",
    "giving_month": "12",
    "qr_toggle_url": "qr-toggle",
    "use_songs": "false",
}
RESERVED_PATHS = {"ssadmin", "qrcodes", "qrscan", "qrscan-test", "u", "static", "setup"}

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
    return game if GAME_ID_RE.fullmatch(game) and game != "settings" else None


DEFAULT_GAME = clean_game_id(os.getenv("GAME_ID", "default")) or "default"


def require_game(game):
    if not redis_client.sismember("games", game):
        abort(404)
    return game


def only_game():
    """The game's id if exactly one exists (keeps pre-multi-game URLs working)."""
    games = redis_client.smembers("games")
    return next(iter(games)) if len(games) == 1 else None


def to_int(raw, default):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def locked_settings():
    """Settings currently forced by an environment variable."""
    return {k for k, env in SETTING_ENV.items() if os.getenv(env)}


def raw_settings():
    """Effective raw (string) settings: environment, else stored, else default."""
    stored = redis_client.hgetall("settings")
    return {
        key: os.getenv(env) or stored.get(key, SETTING_DEFAULTS[key])
        for key, env in SETTING_ENV.items()
    }


def get_config():
    """Effective configuration for this request (cached on flask.g)."""
    if "cfg" in g:
        return g.cfg
    raw = raw_settings()
    rules_sep = "," if os.getenv(SETTING_ENV["rules"]) else "\n"
    cfg = dict(config)
    cfg.update(
        admin_user=raw["admin_user"],
        url=(raw["url"] or request.url_root).rstrip("/"),
        budget=raw["budget"],
        rules=[r.strip() for r in raw["rules"].split(rules_sep) if r.strip()],
        reroll_count=to_int(raw["reroll_count"], 0),
        giving_day=to_int(raw["giving_day"], 25),
        giving_month=to_int(raw["giving_month"], 12),
        qr_toggle_url=raw["qr_toggle_url"],
        use_songs=raw["use_songs"] == "true",
    )
    g.cfg = cfg
    return cfg


def get_budget(game):
    return redis_client.get(gk(game, "budget")) or get_config()["budget"]


def setup_done():
    return bool(redis_client.exists("setup:done"))


def admin_password_set():
    return bool(os.getenv("ADMIN_PASSWORD") or redis_client.get("admin:password_hash"))


def check_admin_password(candidate):
    env_password = os.getenv("ADMIN_PASSWORD")
    if env_password:
        return hmac.compare_digest(candidate.encode("utf-8"), env_password.encode("utf-8"))
    stored = redis_client.get("admin:password_hash")
    return bool(stored) and check_password_hash(stored, candidate)


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


def same_origin():
    """Reject cross-site POSTs (browsers resend basic-auth credentials)."""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True
    host = urlparse(source).netloc
    return host in {request.host, urlparse(get_config()["url"]).netloc}


def require_admin(view):
    """HTTP basic auth using ADMIN_PASSWORD (any username). Disabled if unset."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not admin_password_set():
            return (
                "The admin page is disabled because no admin password is set. "
                "Set the ADMIN_PASSWORD environment variable to enable it.",
                403,
            )
        if request.method == "POST" and not same_origin():
            abort(403)
        auth = request.authorization
        if not auth or not check_admin_password(auth.password or ""):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Secret Santa admin"'},
            )
        return view(*args, **kwargs)

    return wrapper


@app.context_processor
def inject_config():
    return {"config": get_config()}


@app.before_request
def require_setup():
    """Until first-run setup is finished, everything redirects to /setup."""
    if request.endpoint not in ("setup", "static") and not setup_done():
        return redirect(url_for("setup"))


@app.route("/")
def main():
    return render_template("index.html")


@app.route("/qrcodes", defaults={"game": None})
@app.route("/qrcodes/<game>")
@require_admin  # the QR codes contain each person's private link token
def qrcodes(game):
    game = require_game(game or only_game() or "")
    people = sorted(get_people(game))
    people_dict = []

    for person in people:
        token = redis_client.get(gk(game, "token", person))

        qr = qrcode.make(f"{get_config()['url']}/qrscan/{token}", border=1)
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


@app.route("/<toggle>", defaults={"game": None})
@app.route("/<toggle>/<game>")
def qr_toggle(toggle, game):
    if toggle != get_config()["qr_toggle_url"]:
        abort(404)
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
        song=get_config()["use_songs"]
        and recipient != "Not assigned yet"
        and os.path.isfile(f"static/songs/{recipient.lower()}.mp3"),
    )


@app.route("/qrscan/<token>")
def qrscan(token):
    game, person = person_for_token(token)

    cfg = get_config()
    if get_int(gk(game, "qr-active")) == 0 and cfg["use_qr_checks"]:
        return redirect("https://cdn.mtdv.me/video/last_rickmas.mp4")

    if (
        datetime.datetime.now().month != cfg["giving_month"]
        or datetime.datetime.now().day != cfg["giving_day"]
    ) and cfg["use_qr_checks"]:
        return redirect("https://cdn.mtdv.me/video/feliz_navidad.mp4")

    return render_qr_reveal(game, redis_client.get(gk(game, "recipient", person)))


@app.route("/qrscan-test/<game>/<recipient>")
def qrscantest(game, recipient):
    if config["use_qr_checks"]:
        abort(404)  # test route only available with DISABLE_QR_CHECKS=true

    return render_qr_reveal(require_game(game), recipient)


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
                    "link": f"{get_config()['url']}/u/{redis_client.get(gk(game, 'token', name))}",
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
    return admin_redirect(game, f"Budget reset to the default ({get_config()['budget']}).")


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


def parse_settings_form(form, locked):
    """Validate the settings fields. Returns (values to store, error messages)."""
    values, errors = {}, []

    def field(key):
        return (form.get(key) or "").strip()

    def want(key):
        return key not in locked

    if want("admin_user"):
        value = field("admin_user")
        if 1 <= len(value) <= 32:
            values["admin_user"] = value
        else:
            errors.append("Admin name must be 1-32 characters.")

    if want("url"):
        value = field("url").rstrip("/")
        if re.fullmatch(r"https?://[^\s/]+(/\S*)?", value):
            values["url"] = value
        else:
            errors.append("The public URL must start with http:// or https://.")

    if want("budget"):
        value = field("budget")
        if 1 <= len(value) <= 20:
            values["budget"] = value
        else:
            errors.append("Budget must be 1-20 characters.")

    if want("rules"):
        lines = [r.strip() for r in (form.get("rules") or "").splitlines() if r.strip()]
        if any(len(r) > 200 for r in lines):
            errors.append("Each rule must be 200 characters or fewer.")
        else:
            values["rules"] = "\n".join(lines)

    if want("reroll_count"):
        value = to_int(field("reroll_count"), -1)
        if 0 <= value <= 10:
            values["reroll_count"] = str(value)
        else:
            errors.append("Rerolls must be a number from 0 to 10.")

    day, month = to_int(field("giving_day"), 0), to_int(field("giving_month"), 0)
    if want("giving_day") and want("giving_month"):
        try:
            datetime.date(2000, month, day)  # 2000 is a leap year
            values["giving_day"], values["giving_month"] = str(day), str(month)
        except ValueError:
            errors.append("Giving day isn't a valid date.")

    if want("qr_toggle_url"):
        value = field("qr_toggle_url")
        if re.fullmatch(r"[A-Za-z0-9_-]{3,40}", value) and value not in RESERVED_PATHS:
            values["qr_toggle_url"] = value
        else:
            errors.append(
                "The QR toggle URL must be 3-40 letters, numbers, - or _ (and not a name the app already uses)."
            )

    if want("use_songs"):
        values["use_songs"] = "true" if form.get("use_songs") else "false"

    return values, errors


def settings_for_form(form=None):
    """Values to show in the settings form: submitted ones, else current ones."""
    raw = raw_settings()
    values = {
        "admin_user": raw["admin_user"],
        "url": raw["url"] or request.url_root.rstrip("/"),
        "budget": raw["budget"],
        "rules": raw["rules"].replace(",", "\n") if os.getenv("RULES") else raw["rules"],
        "reroll_count": raw["reroll_count"],
        "giving_day": raw["giving_day"],
        "giving_month": raw["giving_month"],
        "qr_toggle_url": raw["qr_toggle_url"],
        "use_songs": raw["use_songs"] == "true",
    }
    if form is not None:
        for key in values:
            if key == "use_songs":
                values[key] = bool(form.get(key))
            elif key in form:
                values[key] = form.get(key)
    return values


def parse_people_lines(text):
    """'name' or 'name, group' per line -> ({name: group}, errors)."""
    people, errors = {}, []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        name_part, _, group_part = line.partition(",")
        name = clean_name(name_part)
        if not name:
            errors.append(f"'{line.strip()}' isn't a valid name.")
        elif name in people:
            errors.append(f"{name.capitalize()} is listed twice.")
        else:
            people[name] = clean_group(group_part)
    return people, errors


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run setup, protected by a one-time code printed in the server log."""
    if setup_done():
        return redirect(url_for("admin"))

    context = dict(
        errors=[],
        game="",
        people="",
        locked=locked_settings(),
        needs_password=not os.getenv("ADMIN_PASSWORD"),
        values=settings_for_form(),
    )
    if request.method == "GET":
        # Suggest a random, hard-to-guess QR toggle URL unless one is already set
        if "qr_toggle_url" not in redis_client.hgetall("settings"):
            context["values"]["qr_toggle_url"] = (
                os.getenv("QR_TOGGLE_URL") or secrets.token_urlsafe(6).replace("_", "x")
            )
        return render_template("setup.html", **context)

    if not same_origin():
        abort(403)

    form = request.form
    values, errors = parse_settings_form(form, context["locked"])
    context["values"] = settings_for_form(form)
    context["game"], context["people"] = form.get("game", ""), form.get("people", "")

    code = redis_client.get("setup:code") or ""
    if not code or not hmac.compare_digest(
        (form.get("code") or "").strip().encode("utf-8"), code.encode("utf-8")
    ):
        errors.append("The setup code is wrong. It is printed in the server log.")

    game = clean_game_id(form.get("game"))
    if not game:
        errors.append(
            "Game ID must be 1-32 characters: lowercase letters, numbers and hyphens (e.g. family)."
        )
    people, people_errors = parse_people_lines(form.get("people"))
    errors += people_errors

    password = form.get("password") or ""
    if context["needs_password"]:
        if len(password) < 8:
            errors.append("The admin password must be at least 8 characters.")
        elif password != form.get("password2"):
            errors.append("The two passwords don't match.")

    if errors:
        context["errors"] = errors
        return render_template("setup.html", **context), 400

    with redis_client.lock("setup-lock", timeout=10, blocking_timeout=10):
        if setup_done():
            return redirect(url_for("admin"))
        if values:
            redis_client.hset("settings", mapping=values)
        if context["needs_password"]:
            redis_client.set("admin:password_hash", generate_password_hash(password))
        redis_client.sadd("games", game)
        for name, group in people.items():
            redis_client.hsetnx(gk(game, "people"), name, group)
            ensure_person_state(game, name)
        redis_client.setnx(gk(game, "qr-active"), 0)
        redis_client.set("setup:done", 1)
        redis_client.delete("setup:code")

    return redirect(
        url_for(
            "admin_game",
            game=game,
            msg="Setup complete! You're signed in to the admin page with the password you just chose.",
            ok=1,
        )
    )


@app.route("/ssadmin/settings", methods=["GET", "POST"])
@require_admin
def admin_settings():
    locked = locked_settings()
    context = dict(
        errors=[],
        locked=locked,
        needs_password=not os.getenv("ADMIN_PASSWORD"),
        values=settings_for_form(),
    )
    if request.method == "GET":
        return render_template("admin_settings.html", **context)

    values, errors = parse_settings_form(request.form, locked)
    new_password = request.form.get("password") or ""
    if context["needs_password"] and new_password:
        if len(new_password) < 8:
            errors.append("The admin password must be at least 8 characters.")
        elif new_password != request.form.get("password2"):
            errors.append("The two passwords don't match.")
    if errors:
        context["errors"] = errors
        context["values"] = settings_for_form(request.form)
        return render_template("admin_settings.html", **context), 400

    if values:
        redis_client.hset("settings", mapping=values)
    if context["needs_password"] and new_password:
        redis_client.set("admin:password_hash", generate_password_hash(new_password))
    return redirect(url_for("admin", msg="Settings saved.", ok=1))


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
        reroll_limit=get_config()["reroll_count"],
        rerolls_left=max(0, get_config()["reroll_count"] - rerolls_used),
    )


@app.route("/u/<token>/reroll", methods=["POST"])
def reroll(token):
    game, name = person_for_token(token)

    reroll_count = get_config()["reroll_count"]
    if reroll_count == 0:
        return jsonify({"error": "Rerolls are disabled."})

    try:
        with redis_client.lock(ASSIGN_LOCK, timeout=10, blocking_timeout=10):
            if get_int(gk(game, "rerolls", name)) >= reroll_count:
                return jsonify(
                    {
                        "error": f"You have reached the maximum number of rerolls ({reroll_count})."
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

# Existing installs (or ones seeded from VALID_NAMES) don't need the setup page
if redis_client.exists("games"):
    redis_client.setnx("setup:done", 1)
if not setup_done():
    redis_client.setnx("setup:code", secrets.token_urlsafe(9))
    logging.warning(
        "FIRST RUN: open /setup in your browser to finish setting up. "
        f"Setup code: {redis_client.get('setup:code')}"
    )

print(f"Loaded games: {sorted(redis_client.smembers('games'))}")
print(f"Loaded songs: {os.listdir('static/songs') if os.path.isdir('static/songs') else []}")


if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", debug=False, port=config["port"])
    except Exception as e:
        logging.critical(f"Failed to start the application: {e}")
