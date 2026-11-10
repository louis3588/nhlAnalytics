-- Runs only when Docker initializes an empty PostgreSQL data directory.
\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE SCHEMA IF NOT EXISTS analytics;

-- Application tables and hypertables will be introduced with versioned migrations.
-- This bootstrap does not import CSV files or load the Python JSON snapshots.
