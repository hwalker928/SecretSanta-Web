# Secret Santa Name Picker

## Features

- Random name allocation via a private link per person (shown on the admin page at `/ssadmin`, protected by `ADMIN_PASSWORD`)
- Prevents being allocated yourself
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
      ADMIN_USER: Harry
      ADMIN_PASSWORD: change-me # Protects /ssadmin, which lists everyone's private links (admin page is disabled if unset)
      # Initial people only: used the first time the app starts with an empty database.
      # After that, add/remove people and set groups on /ssadmin.
      VALID_NAMES: alice,ben,charlie,david
      NAME_GROUPS: "family1:alice,ben;family2:charlie,david" # Optional: people in a group avoid drawing each other where possible

      BUDGET: £10 # Default only; can be changed on /ssadmin

      GIVING_DAY: 25
      GIVING_MONTH: 12

      REROLL_COUNT: 1 # Set to 0 to disable rerolls

      RULES: Keep your recipient a secret,Multiple presents are allowed,Spend as close to the budget as possible

      QR_TOGGLE_URL: qr-toggle # Set this to something random to prevent people from guessing the URL
      USE_SONGS: false # Set to true to play a song per person when the QR codes are activated

      URL: "http://localhost:8000" # This must be correct to generate QR codes
      REDIS_HOST: redis
    volumes:
      - ./songs:/usr/src/app/static/songs # Only required if USE_SONGS is true
    depends_on:
      - redis
    ports:
      - "8000:8000"
    restart: always

  redis:
    image: redis:latest
    volumes:
      - redis_data:/data
    restart: always

volumes:
  redis_data:
```

## QR Code Stickers

A QR code is automatically generated per-person, which when scanned on the day that presents are given (configurable in the docker-compose.yml), will reveal who the present is for.

You can view all of the QR codes here: http://localhost:8000/qrcodes

The QR codes will not be active until both of the following conditions are true:

1. It is the day of giving the presents
2. The QR codes have been activated by visiting http://localhost:8000/qr-toggle (configurable in the docker-compose.yml)

## Songs on QR Code Activation

If you set `USE_SONGS` to true in the docker-compose.yml, a song will play per person when the QR codes are activated. The songs are stored in the `static/songs` directory.

Save a song in the `static/songs` directory and name it `firstname.mp3` (e.g. `harry.mp3`), and it will play when the QR code is activated for that person.

The song should be around 30 seconds long, but there is no set limit.
