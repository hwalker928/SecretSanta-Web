# Secret Santa Name Picker

## Deploy using Docker Compose

```
services:
  web:
    image: ghcr.io/hwalker928/secretsanta:latest
    environment:
      ADMIN_USER: Harry
      VALID_NAMES: alice,ben,charlie,david
      BUDGET: 10

      URL: "http://localhost:5000"

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
