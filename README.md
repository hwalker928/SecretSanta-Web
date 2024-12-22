# Secret Santa Name Picker

## Features

- Random name allocation
- Prevents being allocated yourself
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
      VALID_NAMES: alice,ben,charlie,david

      BUDGET: £10

      GIVING_DAY: 25
      GIVING_MONTH: 12

      REROLL_COUNT: 1 # Set to 0 to disable rerolls

      RULES: Keep your recipient a secret,Multiple presents are allowed,Spend as close to the budget as possible

      QR_TOGGLE_URL: qr-toggle # Set this to something random to prevent people from guessing the URL
      USE_SONGS: false # Set to true to play a song per person when the QR codes are activated

      URL: "http://localhost:5000" # This must be correct to generate QR codes
      REDIS_HOST: redis
    depends_on:
      - redis
    ports:
      - "5000:5000"
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

You can view all of the QR codes here: http://localhost:5000/qrcodes

The QR codes will not be active until both of the following conditions are true:

1. It is the day of giving the presents
2. The QR codes have been activated by visiting http://localhost:5000/qr-toggle (configurable in the docker-compose.yml)

## Songs on QR Code Activation

If you set `USE_SONGS` to true in the docker-compose.yml, a song will play per person when the QR codes are activated. The songs are stored in the `static/songs` directory.

Save a song in the `static/songs` directory and name it `firstname.mp3` (e.g. `harry.mp3`), and it will play when the QR code is activated for that person.

The song should be around 30 seconds long, but there is no set limit.
