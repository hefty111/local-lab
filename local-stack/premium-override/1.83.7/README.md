# Premium Override Image

Builds `litellm-local:v1.83.7-premium-override` from the official
`ghcr.io/berriai/litellm-database:v1.83.7-stable` image. The build replaces only
the `LicenseCheck.is_premium()` body with `return True` in both the installed
package and the image's `/app/litellm` source copy. It runs `test_override.py`
in a fresh process for each import path. Repository license source is unchanged.

This is a local modified image, not an official enterprise license. It does not
populate license metadata or guarantee that every enterprise feature works.

From the repository root:

```bash
docker compose -f local-setup/docker-compose.yml build litellm
docker compose -f local-setup/docker-compose.yml up -d --no-deps --wait litellm
bash local-setup/verify.sh
```

The migration service retains the official image. Recreating only the runtime
does not rerun migrations or reset Postgres data.

Run the focused test against the running container:

```bash
docker compose -f local-setup/docker-compose.yml exec -T litellm python < local-setup/premium-override/test_override.py
```
