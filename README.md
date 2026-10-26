# data-quality-pipeline-tests

[![CI](https://github.com/mustafaautomation/data-quality-pipeline-tests/actions/workflows/ci.yml/badge.svg)](https://github.com/mustafaautomation/data-quality-pipeline-tests/actions/workflows/ci.yml)

A small orders ETL (CSV to DuckDB) with the kind of testing a data pipeline needs: unit and
property-based tests for the transformation, and SQL data-quality checks that gate each load.
Each check is proven to catch its own defect.

Personal portfolio project, not production or client work. All data is synthetic, made by
`scripts/generate.py`.

## The pipeline

```
customers.csv, orders.csv, fx_rates.csv
        |  transform (pure Python): validate, normalise, de-duplicate, quarantine
        v
DuckDB: customers, orders, fx_rates, quarantine, load_audit
        |  13 SQL checks (11 error, 2 warn)
        v
gate: exit 0 (load is good) / 1 (an error-level check failed) / 2 (bad input)
```

- **Transform.** Amounts are parsed as exact decimals into minor units, using each currency's
  exponent (JPY has none). Timestamps must carry a UTC offset and are stored in UTC. USD values use
  banker's rounding. Emails and codes are normalised. A later update to the same `order_id` wins,
  whatever order the rows arrive in.
- **Quarantine.** Rows that fail validation are kept with a reason and the raw row, instead of
  being dropped or loaded as-is. Examples: bad amounts, unknown currency or status, offset-less
  timestamps, orphan customers.
- **Load.** Upsert by id inside a transaction, so re-running the same batch changes nothing and a
  failure rolls back. Every run writes an audit row: source rows, loaded, quarantined and
  duplicates dropped.
- **No database constraints, on purpose.** BigQuery, Snowflake and Redshift do not enforce primary
  or foreign keys, so uniqueness and referential integrity have to be checked. That is what the
  checks are for.

## The checks

| Check | Severity | Catches |
|---|---|---|
| orders_order_id_unique | error | Duplicate orders |
| orders_required_fields | error | Nulls in required columns |
| orders_status_accepted | error | Status outside the known lifecycle |
| orders_amount_non_negative | error | Negative amounts |
| orders_customer_exists | error | Orders whose customer does not exist |
| orders_usd_matches_fx | error | USD value that does not match the stored FX rate, or a missing rate |
| orders_not_in_future | error | Orders timestamped after the run (clock or timezone bugs) |
| customers_id_unique / customers_email_unique | error | Duplicate customers or shared emails |
| run_rows_reconcile | error | Rows lost between source and target (source = loaded + quarantined + duplicates) |
| run_quarantine_rate | error | More than 5% of a source quarantined (configurable), i.e. a broken upstream batch |
| orders_fresh | warn | Newest order older than 48 hours |
| run_volume_stable | warn | Loaded volume more than 50% off the previous runs' average |

## How the tests prove the checks work

- **Every error check has a defect test.** `tests/test_checks.py` loads the sample, plants one
  defect straight into the tables (as a buggy upstream job would), and asserts that **exactly** the
  expected checks fail and no others. A meta-test fails if an error-level check has no defect test.
- **Warnings** (stale data, a volume drop across runs) are tested to warn without blocking the gate.
- **Property-based tests (Hypothesis)** on random mixes of valid and junk rows assert that every
  input row is accounted for, output ids are unique, amounts are non-negative and in UTC, and the
  transform is deterministic.
- **Load tests** cover idempotent reloads, upserts in a later run and rollback on failure.
- **CLI tests** cover exit codes, the JSON output and the GitHub job summary.
- Two mutations were tried by hand: flipping the "latest update wins" comparison, and weakening the
  FX check. The tests caught both.

51 tests, 99% line coverage, about 20 seconds locally.

## CI

| Job | What it does |
|---|---|
| Lint, types and tests | ruff (with the bandit `S` rules), mypy strict, pytest with at least 90% coverage |
| Pipeline run | Runs the CLI on the committed sample: it must pass. Then generates a batch with 15% bad rows: the gate must exit 1, and `run_quarantine_rate` must be the only failing check. Results are uploaded and the markdown report goes to the job summary |

## Run it

```
uv sync
uv run pytest
uv run dqpipe data/sample --run-at 2026-10-26T00:00:00+00:00          # in-memory DuckDB
uv run dqpipe data/sample --db warehouse.duckdb --json result.json      # keep the warehouse file
uv run python scripts/generate.py out/broken --bad-rate 0.15            # make a batch that fails
```

The sample covers the week up to 25 October 2026, so pass `--run-at` near that date. Otherwise the
freshness check will warn, as it should.

## Limits

- Single-process and in-memory sized: thousands of rows, not billions. The checks are plain SQL and
  would carry over to a warehouse, but there is no orchestrator (Airflow, Dagster) or dbt here.
- Thresholds are fixed numbers. There is no statistical anomaly detection beyond the simple volume
  comparison.

## License

MIT
