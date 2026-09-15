# Private viewer preview

Use this path for quick UI edits before a deployment. It reads an existing,
authorized **L0 worker-produced candidate SQLite snapshot**. It does not acquire
documents, create a sample corpus, run workers, migrate, seed, or write records.
An absent, incompatible or unmarked snapshot fails closed. The snapshot must
contain the worker manifest and L1 evidence tables from authoritative PostgreSQL.
Its metadata is provenance evidence, not a cryptographic signature; obtain the
file through your authorized snapshot delivery process and keep it private.

## Local edit and reload

Install the repository's locked Python dependencies. Set these nonsecret values
for the existing Cognito application and the approved reviewer list:

```sh
export CLHEAR_REVIEWER_EMAILS='your-approved-reviewer@example.com'
export CLHEAR_COGNITO_REGION='us-east-1'
export CLHEAR_COGNITO_USER_POOL_ID='your-existing-pool-id'
export CLHEAR_COGNITO_CLIENT_ID='your-existing-public-client-id'
export CLHEAR_COGNITO_DOMAIN='https://your-existing-hosted-ui.auth.us-east-1.amazoncognito.com'
python scripts/preview.py --snapshot /absolute/private/path/candidate.db
```

Open **http://localhost:8000**, then use **Organization sign in**. The existing
Terraform Cognito callback list includes
`http://localhost:8000/auth/cognito/callback`; that callback must also exist on
the actual configured client. This command performs no infrastructure setup.
Do not paste production secrets or database credentials into the preview.
The launcher binds only `127.0.0.1`, generates a separate unpredictable session
secret for this process, and does not inherit AWS credentials, queue settings,
Infer credentials or production signing keys. Cognito sign-in still needs a
network connection. It is the corpus that is offline.

Python edits reload the server; refresh the browser after HTML/CSS edits (those
files are read per request). The per-process signing secret survives reloads;
restarting the launcher requires sign-in again. The cookie is HttpOnly and
SameSite=Lax; only this exact loopback preview permits HTTP. Hosted cookies
remain Secure. Preview tokens cannot be used as production sessions.

The snapshot is opened with SQLite `mode=ro` and `query_only`. Startup never
runs migrations or seeding. Preview does not enqueue account updates or licensed
text access audits. Permission checks still apply, including expiration.
Keep the snapshot outside the repository and restrict filesystem access.
No file is downloaded or copied by this command.

For automatic private snapshot refresh instead, select an explicitly read-only
AWS profile and use the existing viewer synchronizer:

```sh
AWS_PROFILE=reg42 python scripts/preview.py \
  --snapshot-s3 s3://clhear-deploy-730649732189/webui/l1/candidate.db
```

This performs only the synchronizer's S3 HEAD/GET operations, using a private
temporary directory and a 0600 snapshot file. It checks the latest snapshot at
most 300 seconds after a successful check. A due check failure blocks responses
and does not extend cached permission freshness. The selected profile must have
authorized read access; the launcher neither grants nor validates its entire
IAM policy. Static AWS credentials are not inherited. No queue or worker command
is sent. The temporary cache is removed when the launcher exits normally.

An ETag sidecar is saved only after worker-snapshot validation, with mode 0600.
Reload always checks S3 again but avoids redownloading an unchanged snapshot;
no freshness timestamp is persisted or trusted across processes.

## What can be reviewed

The eight-layer overview, L1 Sources/Fleet/Evals/Changes, original text and clause
inspection, job steps and durations use the existing read APIs. Their displayed
results belong to the snapshot. Every HTML page has a preview banner with its
generation time and age; `/api/clhear/preview` gives the same evidence after
sign-in. Snapshot generation time is separate from publisher-check timestamps.

All operational writes are blocked, including votes, submissions, reruns,
permission changes, exports and model calls. Search uses stored ref/FTS/lexical
indexes and skips the model embedding leg. Email-link sending is disabled;
the existing verified Cognito or configured Google OAuth flow signs in without
writing a user row. The older general Eval Studio GET endpoint creates sampling
tasks and is therefore unavailable here; the L1 persisted evaluation evidence
remains available. Unreviewed API surfaces return an explicit 405.

Preview is not an accepted release and cannot establish current completeness or
publisher freshness. A static local file cannot observe later permission
revocations and is refused after 24 hours from generation; replace it or use the
S3 reader. Even the refreshed reader can observe only the latest worker snapshot,
with its 300-second cache window. Existing expiry checks never extend grants.
Do not use preview where per-read audit recording is a condition of permission.

## Hosted private preview

Set `CLHEAR_PREVIEW_MODE=true`, `CLHEAR_RESTRICTED_ACCESS=true`,
`CLHEAR_AUTH_DEBUG=false`, explicit reviewer emails, a separate session secret,
the preview Cognito client and the preview HTTPS base URL. The usual Lambda
snapshot synchronizer supplies `DATABASE_URL` from `CLHEAR_DB_S3_URI`; it retains
its existing failed-refresh access checks. The application reopens every
replacement in read-only mode and validates its worker manifest.

Provision this viewer with only read access to its private candidate snapshot,
normal logs, and its configured sign-in flow. It needs no SQS, SES, database,
importer, model, or secret-discovery permissions. The app performs no hosted
deployment or permission setup. Production behavior is unchanged when
`CLHEAR_PREVIEW_MODE` is absent or false.
