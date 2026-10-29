# data/

Placeholder only. This folder must stay empty in the repository.

The application stores its database, logs and backups in an operating-system user-data
directory outside the source tree (see `docs/03_DATA_MODEL.md`), not here. Never place real
bank statements, databases or exports in this folder: `.gitignore` excludes everything except
this file and `.gitkeep`, but the project folder may live in a cloud-synced location (for
example OneDrive), which would upload anything stored here.
