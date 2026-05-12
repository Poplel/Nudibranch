# Publishing Docker Images From GitHub

Nudibranch includes a GitHub Actions workflow that builds and publishes Docker
images to GitHub Container Registry.

## Image Names

For a repository at:

```txt
github.com/example/nudibranch
```

the workflow publishes:

```txt
ghcr.io/example/nudibranch-api:latest
ghcr.io/example/nudibranch-web:latest
```

It also publishes immutable commit tags:

```txt
ghcr.io/example/nudibranch-api:sha-<commit>
ghcr.io/example/nudibranch-web:sha-<commit>
```

Version tags like `v0.1.0` publish matching image tags.

## GitHub Setup

1. Push this project to a GitHub repository.
2. Make sure GitHub Actions are enabled.
3. Push to `main`, or run `Publish Docker Images` manually from the Actions tab.
4. In the repository package settings, make the images public if you want
   unauthenticated Docker pulls.

The workflow uses GitHub's built-in `GITHUB_TOKEN`; no extra registry token is
required for publishing to GHCR from the same repository.

## Production Compose

Set this in `.env`:

```txt
NUDIBRANCH_IMAGE_PREFIX=ghcr.io/example/nudibranch
NUDIBRANCH_IMAGE_TAG=latest
```

Then deploy with:

```sh
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

To pin to an immutable commit image:

```txt
NUDIBRANCH_IMAGE_TAG=sha-abc1234
```

## Private Package Pulls

If the GHCR packages are private, log in on the Docker host:

```sh
docker login ghcr.io
```

Use a GitHub personal access token with package read access as the password.

## Development vs Production Compose

- `docker-compose.yml` builds local images from source.
- `docker-compose.prod.yml` pulls published images from GHCR.

