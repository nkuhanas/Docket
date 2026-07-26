# Restore-only secrets

`scripts/setup-backup-age.sh` writes `backup_age_identity` here with mode
`0600`. This directory is not mounted into the routine Docket or Hermes
containers.

Keep an offline copy of the identity. To verify the latest encrypted backup:

```bash
DOCKET_BACKUP_AGE_IDENTITY_FILE=secrets/restore/backup_age_identity \
  scripts/docket verify-restore
```

Do not commit, paste, or place the private identity in `.env`.
