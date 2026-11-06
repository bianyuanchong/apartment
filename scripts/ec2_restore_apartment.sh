#!/bin/bash
set -eu
SQL=/tmp/apartment_schema_pg15.sql
sudo -u postgres psql -d postgres -v ON_ERROR_STOP=1 -c "DROP SCHEMA IF EXISTS apartment CASCADE;"
sudo -u postgres psql -d postgres -v ON_ERROR_STOP=1 -f "$SQL"
sudo -u postgres psql -d postgres -c "SELECT COUNT(*) AS apartment_count FROM apartment.apartment;"
sudo -u postgres psql -d postgres -c "SELECT MAX(as_of_date) AS latest_feed FROM apartment.daily_feed;"
