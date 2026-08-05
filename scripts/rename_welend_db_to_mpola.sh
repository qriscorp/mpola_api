#!/usr/bin/env bash
set -euo pipefail

# One-time cutover: renames the production database from `welend` to `mpola`.
# Uses RENAME TABLE (same-server table moves are metadata-only, near-instant,
# no data copy) instead of mysqldump, since MySQL has no RENAME DATABASE.
#
# Run order:
#   1. Back up first regardless: mysqldump -h $DB_HOST -u $DB_USER -p welend > welend_backup_$(date +%F).sql
#   2. Stop the API (docker-compose stop / systemctl stop) so nothing writes mid-rename.
#   3. Run this script:  DB_USER=... DB_PASS=... ./scripts/rename_welend_db_to_mpola.sh
#   4. Update DATABASE_URL in .env to point at the `mpola` database.
#   5. Restart the API and spot-check (login, wallet balance, a loan record).
#   6. Once confident (a day or two), drop the now-empty `welend` database.

DB_HOST="${DB_HOST:-95.111.239.122}"
DB_USER="${DB_USER:?set DB_USER}"
DB_PASS="${DB_PASS:?set DB_PASS}"
OLD_DB="welend"
NEW_DB="mpola"

MYSQL=(mysql -h "$DB_HOST" -u "$DB_USER" -p"$DB_PASS")

echo "1) Creating target database '$NEW_DB' if missing..."
"${MYSQL[@]}" -e "CREATE DATABASE IF NOT EXISTS \`$NEW_DB\`;"

echo "2) Listing tables in '$OLD_DB'..."
TABLES=$("${MYSQL[@]}" -N -e "SELECT table_name FROM information_schema.tables WHERE table_schema='$OLD_DB';")

if [ -z "$TABLES" ]; then
  echo "No tables found in '$OLD_DB' - nothing to do."
  exit 0
fi

RENAME_SQL=""
for t in $TABLES; do
  RENAME_SQL+="RENAME TABLE \`$OLD_DB\`.\`$t\` TO \`$NEW_DB\`.\`$t\`;"
done

echo "3) Renaming tables into '$NEW_DB' (make sure the API is stopped first)..."
"${MYSQL[@]}" -e "$RENAME_SQL"

echo "4) Row counts in '$NEW_DB' after rename:"
"${MYSQL[@]}" -e "SELECT table_name, table_rows FROM information_schema.tables WHERE table_schema='$NEW_DB';"

echo "Done. '$OLD_DB' should now be empty (verify before dropping it)."
echo "Next: point DATABASE_URL at '$NEW_DB', restart the API, and verify."
