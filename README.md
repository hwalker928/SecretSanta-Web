# Secret Santa Name Picker

## Features

- Random name allocation
- Prevents being allocated yourself
- Reroll functionality
- Rules list
- QR code sticker generation (read more [here](#qr-code-stickers))

## Deploy using Docker Compose

```
services:
  web:
    image: ghcr.io/hwalker928/secretsanta:latest
    environment:
      ADMIN_USER: Harry
      VALID_NAMES: alice,ben,charlie,david
      BUDGET: £10

      URL: "http://localhost:5000"

      RULES: Keep your recipient a secret,Multiple presents are allowed,Spend as close to the budget as possible

      REROLL_COUNT: 1 # Set to 0 to disable rerolls

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
