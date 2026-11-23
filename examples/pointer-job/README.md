# pointer-job — a versioned reference to data that lives outside r3

Some inputs are not files you can hold in `output/`: a Spiral table, a W&B artifact, a
checkpoint in an S3 bucket. They are too large, or they belong to another system. A
**pointer job** still puts them in the r3 provenance graph: a committed job whose
`output/pointer.yaml` names the external thing precisely enough to fetch the same bytes
again. Downstream jobs depend on the pointer job by `path`, read the manifest, and fetch
the data themselves.

The shape is the [`raw-data/`](../raw-data/) entry node (a README as the provenance record,
no `run.sh`, `output/` placed by hand after commit) with a manifest instead of data.

**There is deliberately no `run.sh`.** Because the output is a few hundred bytes,
`xr3-sky launch` pushes it to S3 like any other dependency output.

## Files

- `README.md` — this file: what is pointed at, and why that snapshot.
- `metadata.yaml` — `path` (what downstream jobs depend on) and tags.
- `output/pointer.yaml` — the manifest. Copy it into the committed job's `output/` after
  `r3 commit` / `xr3 commit` (r3 does not commit `output/`), then write `output/done`
  (`echo completed > output/done`) so tools treat the job as finished.

## `pointer.yaml` kinds

| `kind` | Fields |
|---|---|
| `spiral` | `project`, `dataset`, `table`, `asof_us` (snapshot time in µs), `content_sha256` of the scanned data if feasible |
| `wandb` | `artifact: entity/project/artifact:version`, or `run:` a run path |
| `s3` | `uri: s3://bucket/key`, plus `version_id` when the bucket is versioned (else `etag`) |

Pin the version every time (`asof_us`, `:version`, `version_id`). A pointer without one
names a moving target, and the provenance it records is a lie.

## Caveat: mutable stores

Spiral tables are mutable, and an `asof` read depends on Spiral keeping old versions.
**That retention is unconfirmed.** For anything that must stay reproducible, freeze a
copy (write the snapshot to its own table that nothing writes to again) and point at the
frozen copy. `content_sha256` lets a consumer detect that the data under a pointer
changed. The same caution applies to unversioned S3 buckets (an `etag` detects a change
but cannot recover the old bytes) and to W&B aliases such as `:latest`.

## Consuming it

```python
import yaml
ptr = yaml.safe_load(open("data/pointer.yaml"))   # dependency checked out at ./data
assert ptr["kind"] == "spiral"
# open ptr["project"]/ptr["dataset"]/ptr["table"] as of ptr["asof_us"] with the Spiral client,
# and check content_sha256 when it is set
```

---

## <TABLE> — pointer

Points at Spiral `<project>/<dataset>/<table>` as of `<asof_us>` (<human-readable date>).
Chosen because <why this snapshot>. Produced by <job / process that wrote the table>.
