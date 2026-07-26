# Encrypted backup and restore verification

Verified on 2026-07-26 before the production gate was enabled.

## Safety properties

- The routine Docket service receives only an `age` public recipient.
- The private restore identity is ignored by Git and is not mounted into
  Docket, PostgreSQL, or Hermes.
- Backup output is written atomically as a mode-`0600` encrypted artifact,
  checksum, and manifest.
- One durable run is claimed per local calendar date, with a lease and bounded
  retry state.
- Retention preserves the union of seven daily and four weekly recovery
  points.
- Backup and restore failures produce deduplicated `#docket-system` alerts.
- Restore verification uses a separate PostgreSQL container, network, and
  volume, then destroys all three.

## Automated gates

```text
scripts/docket check
204 passed
Ruff passed
mypy passed (66 source files)

git diff --check
passed

bash -n scripts/backup.sh scripts/setup-backup-age.sh \
  scripts/verify-restore.sh scripts/docket
passed
```

## Disposable end-to-end proof

The production Docket image and a fresh PostgreSQL 16.9 database were used.
The image deliberately carries PostgreSQL 16.9 dump/restore binaries so a
newer client cannot emit settings that the pinned server cannot restore.

```text
pg_dump (PostgreSQL) 16.9
Encrypted backup created
Encrypted restore verified
Restored schema: 0014 (Alembic head)
Encrypted artifacts: 1
Manifests: 1
```

The proof checked the ciphertext SHA-256 against the manifest, decrypted only
inside the one-off verification container, restored with
`--exit-on-error --no-owner --no-privileges`, confirmed the recorded Alembic
revision, and ran bounded integrity queries over command, audit, and
in-progress operation state.

## Production activation

The rollout is intentionally two stage:

1. deploy revision `0014` while encrypted backups remain disabled;
2. run `scripts/setup-backup-age.sh` using the new image;
3. redeploy with the public recipient enabled;
4. run `scripts/docket backup`;
5. run `scripts/docket verify-restore` with the private identity supplied only
   through `DOCKET_BACKUP_AGE_IDENTITY_FILE`.

Keep an offline copy of the private restore identity. A backup without that
identity is intentionally unrecoverable.
