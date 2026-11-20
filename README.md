# Secret Santa Name Picker

## Features

- Guided first-run setup in the browser
- Random name allocation via a private link per person (shown on the admin page at `/ssadmin`, protected by your admin password)
- Prevents being allocated yourself
- Multiple independent games (e.g. `family`, `friends`) on one server, each with its own people, groups, budget and draw
- Optional groups (e.g. families) who avoid drawing each other where possible
- Admin page to add/remove people, edit groups, copy private links and reset draws
- Reroll functionality
- Rules list
- Configurable budget
- QR code sticker generation (read more [here](#qr-code-stickers))
- Music on QR code activation (read more [here](#songs-on-qr-code-activation))

## Deploy using Docker Compose

```
services:
  web:
    image: ghcr.io/hwalker928/secretsanta:latest
    environment:
      VALKEY_HOST: valkey
      # Everything else (admin password, URL, budget, rules, people, ...) is set up
      # in your browser the first time you open the site. See the README for
      # optional environment variables that override those settings.
    volumes:
      - ./songs:/app/static/songs # Only needed if you enable songs
    depends_on:
      - valkey
    ports:
      - "8000:8000"
    restart: always

  valkey:
    image: valkey/valkey:9
    volumes:
      - valkey_data:/data
    restart: always

volumes:
  valkey_data:
```

## First-run setup

The first time you open the site you are taken to a setup page (nothing is hard-coded). It asks for:

- an admin password and your name
- the public URL, default budget, rules, number of rerolls and giving day
- a random QR activation path (suggested for you)
- your first game and its people (optionally with groups)

To stop a stranger finishing setup before you, it also asks for a one-time **setup code** printed in the server log:

```
docker compose logs web
```

Everything can be changed later under **Settings** on `/ssadmin`.

### Environment variable overrides (optional)

Setting one of these locks that option in the app, so use them only if you prefer config-as-code: `ADMIN_PASSWORD`, `ADMIN_USER`, `URL`, `BUDGET`, `RULES` (comma separated), `REROLL_COUNT`, `GIVING_DAY`, `GIVING_MONTH`, `QR_TOGGLE_URL`, `USE_SONGS`.

Setting `VALID_NAMES` (and optionally `NAME_GROUPS`, `GAME_ID`) seeds a first game on an empty database and skips the setup page, which is how installs worked before setup existed. Existing installs skip setup automatically.

## Upgrading from Redis to Valkey

The app stores its data in [Valkey](https://valkey.io) (a drop-in, Redis-compatible store). `REDIS_HOST`/`REDIS_PORT` are still accepted, so you can swap just the image in an existing compose file.

Valkey can load data files written by Redis up to 7.2, but **not** newer Redis (e.g. `redis:latest` is 8.x). If your existing volume came from a newer Redis, Valkey will refuse to start on it. Back up the volume first, and if that happens either keep running Redis until after giving day or start with a fresh volume and re-create your games.

## Multiple games

Create and manage games on `/ssadmin` (e.g. `family`, `friends`). Each game has its own people, groups, budget, draws and QR codes. Everyone still just uses their personal link, which already belongs to one game.

`VALID_NAMES`/`NAME_GROUPS` only seed a first game on a brand-new install, named by `GAME_ID` (default `default`). Existing installs are migrated automatically into a single game with that ID, and existing private links keep working. QR codes are per-game: `/qrcodes/<game>` and `/<QR_TOGGLE_URL>/<game>`.

## QR Code Stickers

A QR code is automatically generated per-person from their private link token (so it can't be guessed or forged), which when scanned on the day that presents are given (configurable in the docker-compose.yml), will reveal who the present is for.

You can view all of the QR codes (admin login required, since they contain the private link tokens) here: http://localhost:8000/qrcodes/<game>

QR codes printed before this change (they used the person's name rather than their token) no longer work and need reprinting.

The QR codes will not be active until both of the following conditions are true:

1. It is the day of giving the presents
2. The QR codes have been activated by visiting http://localhost:8000/qr-toggle (configurable in the docker-compose.yml)

## Songs on QR Code Activation

If you set `USE_SONGS` to true in the docker-compose.yml, a song will play per person when the QR codes are activated. The songs are stored in the `static/songs` directory.

Save a song in the `static/songs` directory and name it `firstname.mp3` (e.g. `harry.mp3`), and it will play when the QR code is activated for that person.

The song should be around 30 seconds long, but there is no set limit.
