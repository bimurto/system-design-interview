#!/bin/bash
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE replicator WITH REPLICATION PASSWORD 'replicator' LOGIN;
EOSQL

# Allow replication connections in pg_hba.conf
echo "host replication replicator all md5" >> "$PGDATA/pg_hba.conf"
echo "host replication postgres all trust" >> "$PGDATA/pg_hba.conf"
