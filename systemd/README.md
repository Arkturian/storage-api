# systemd units (arkserver)

These files mirror what runs on arkserver under `/etc/systemd/system/`,
including the drop-ins in `<unit>.service.d/`. Taken from the host on
25.11.2026 (#3902): the host was the valid state, the repo copies were an old
installation (`/opt/repos/storage-api`, `TRANSCODING_RESPONSE_MODE=zip`,
tenant `arkturian`).

No deploy step installs them; `deploy.yml` writes only the pdrei storage-api
unit. A change on the host goes into this folder in the same step, and a
change here is rolled out by hand (copy, `systemctl daemon-reload`, restart).

Secrets stay out: units reference `EnvironmentFile=` paths only
(`/etc/storage-api/secrets.env`, `/etc/cloud-secrets/*.env`,
`/etc/cloud-api-federation.env`).

Not mirrored here: `storage-api.service` (main service), `storage-safety-worker`,
`storage-embeddings-worker`.
