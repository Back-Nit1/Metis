# Environments

Two environments: local development (no cloud account) and the hosted GCP VM (`METIS_MODE=server`).

## Local (no cloud account needed) - RAN

```bash
make bootstrap        # python + node deps
cp .env.example .env  # METIS_MODE=local, fixture identity, SQLite, fixture transcriber and inference
make dev              # API + local worker loop + desktop shell
make test && make check
```

- `METIS_MODE=local` uses SQLite (`.metis/metis.sqlite`), local object dir, the fixture transcriber and the fixture inference client. Fixture output is labelled as fixture and is never production output.
- `METIS_ALLOW_FIXTURE_IDENTITY=true` is local-only. Server mode refuses it.
- No credentials belong in `apps/web` or a desktop build.

## Hosted (GCP VM, `METIS_MODE=server`)

One Compute Engine VM: SQLite and the object store on its disk, faster-whisper
and Ollama on loopback, Caddy proxying `/v1/*`. Deploy, backups, monitoring
and cutover: `ops/gcp/README.md` and `ops/gcp/hosted-desktop.md`. Server
config is rendered by `ops/gcp/render-hosted-env.py`.
