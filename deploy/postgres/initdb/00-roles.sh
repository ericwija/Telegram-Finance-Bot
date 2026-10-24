#!/usr/bin/env bash
# Telegram-Finance-Bot V2: create the login roles and the environment database.
#
# Normally run by the official postgres image entrypoint ONLY when the data volume is empty, as
# the container user, over the Unix socket, against a temporary server that listens on no TCP
# address. Shared by all environments; the environment comes from FINBOT_ENV / FINBOT_DB_NAME.
#
# Idempotent: roles and the database are created only if missing, and their attributes and
# passwords are then (re)applied from the secret files every run. If a first initialisation
# failed half-way, the entrypoint will not run this script again (the cluster already exists),
# and the healthcheck stays unhealthy because the marker file is missing. Repair by running it
# by hand against the live server (runbook §3):
#     docker exec finbot-staging-postgres /docker-entrypoint-initdb.d/00-roles.sh
#
# Creates (doc 05 §roles, doc 09 §4.4):
#   finbot_owner     LOGIN, BYPASSRLS, owns the database; runs Alembic (all schema and grants)
#   finbot_app       LOGIN, NO superuser / BYPASSRLS / CREATEDB / CREATEROLE / replication
#   finbot_backup    LOGIN, BYPASSRLS (complete pg_dump), SELECT grants come from Alembic
#   finbot_readonly  NOLOGIN (break-glass only, doc 09 §8)
# No schema objects and no grants are created here: they belong to Alembic, as finbot_owner.
#
# Secrets are read by psql itself from /run/secrets (never via argv, environment or logs).
# Statement logging is disabled for this session so CREATE/ALTER ROLE ... PASSWORD is never
# logged, and psql runs quietly with terse errors (no statement echo).
set -Eeuo pipefail

die() {
	printf '00-roles.sh: %s\n' "$*" >&2
	exit 1
}

: "${FINBOT_ENV:?FINBOT_ENV is required}"
: "${FINBOT_DB_NAME:?FINBOT_DB_NAME is required}"
: "${FINBOT_INIT_MARKER:?FINBOT_INIT_MARKER is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"

case "$FINBOT_ENV" in
	production) expected_db=finbot_prod ;;
	dev | test | staging | drill) expected_db="finbot_${FINBOT_ENV}" ;;
	*) die "unsupported FINBOT_ENV" ;;
esac
[ "$FINBOT_DB_NAME" = "$expected_db" ] || die "FINBOT_DB_NAME must be $expected_db for $FINBOT_ENV (G-ENV-2)"

secrets_dir=/run/secrets
names=(db_superuser_password db_owner_password db_app_password db_backup_password)
for name in "${names[@]}"; do
	file="$secrets_dir/$name"
	[ -f "$file" ] || die "secret $name is missing"
	[ -r "$file" ] || die "secret $name is not readable by uid $(id -u)"
	[ -s "$file" ] || die "secret $name is empty"
	[ "$(wc -l <"$file")" -le 1 ] || die "secret $name must be a single line"
	[ "$(wc -c <"$file")" -ge 25 ] || die "secret $name is too short (min 24 characters)"
done
# Every role gets its own password (compared without printing anything).
for ((i = 0; i < ${#names[@]}; i++)); do
	for ((j = i + 1; j < ${#names[@]}; j++)); do
		if cmp -s "$secrets_dir/${names[i]}" "$secrets_dir/${names[j]}"; then
			die "secrets ${names[i]} and ${names[j]} must differ"
		fi
	done
done

# The entrypoint exports PGPASSWORD for init scripts; a manual repair run reads it here.
# Either way it lives only in this process's environment, never in argv.
if [ -z "${PGPASSWORD:-}" ]; then
	PGPASSWORD="$(cat "$secrets_dir/db_superuser_password")"
	export PGPASSWORD
fi

rm -f "$FINBOT_INIT_MARKER"

psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 --set VERBOSITY=terse --set SHOW_CONTEXT=never \
	--username "$POSTGRES_USER" --no-password --dbname postgres \
	--set dbname="$FINBOT_DB_NAME" <<'SQL'
-- Never log this session's statements: they contain role passwords.
SET log_statement = 'none';
SET log_min_duration_statement = -1;
SET log_min_error_statement = 'panic';

-- Create missing roles with no attributes; everything is applied by ALTER ROLE below.
SELECT format('CREATE ROLE %I NOLOGIN', r)
FROM unnest(ARRAY['finbot_owner', 'finbot_app', 'finbot_backup', 'finbot_readonly']) AS r
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
\gexec

\set owner_pw `cat /run/secrets/db_owner_password`
\set app_pw `cat /run/secrets/db_app_password`
\set backup_pw `cat /run/secrets/db_backup_password`

ALTER ROLE finbot_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    BYPASSRLS CONNECTION LIMIT 5 PASSWORD :'owner_pw';
ALTER ROLE finbot_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    NOBYPASSRLS CONNECTION LIMIT 20 PASSWORD :'app_pw';
ALTER ROLE finbot_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    BYPASSRLS CONNECTION LIMIT 2 PASSWORD :'backup_pw';
ALTER ROLE finbot_readonly NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    NOBYPASSRLS PASSWORD NULL;

\unset owner_pw
\unset app_pw
\unset backup_pw

SELECT format('CREATE DATABASE %I OWNER finbot_owner TEMPLATE template0 ENCODING %L',
              :'dbname', 'UTF8')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname')
\gexec
ALTER DATABASE :"dbname" OWNER TO finbot_owner;

-- Fail (and leave no marker) if any role or the database is mis-provisioned. Memberships are
-- never revoked automatically: an unexpected one is reported for a human to investigate.
SELECT
    (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :'dbname') = 'finbot_owner'
    AND (SELECT count(*) FROM pg_roles WHERE rolname IN
            ('finbot_owner', 'finbot_app', 'finbot_backup', 'finbot_readonly')) = 4
    AND NOT EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname LIKE 'finbot\_%'
          AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolinherit))
    AND NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname IN ('finbot_app', 'finbot_readonly') AND rolbypassrls)
    AND NOT (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'finbot_readonly')
    AND NOT EXISTS (
        SELECT 1 FROM pg_auth_members m
        JOIN pg_roles mem ON mem.oid = m.member
        JOIN pg_roles grp ON grp.oid = m.roleid
        WHERE mem.rolname LIKE 'finbot\_%' OR grp.rolname LIKE 'finbot\_%')
    AS roles_ok
\gset
\if :roles_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'finbot role or database provisioning check failed'; END $$;
\endif
SQL

: >"$FINBOT_INIT_MARKER"
printf '00-roles.sh: roles and database %s verified for %s\n' "$FINBOT_DB_NAME" "$FINBOT_ENV"
