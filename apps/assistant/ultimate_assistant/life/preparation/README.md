# Deterministic preparation

This package organizes explicitly registered local Markdown/text sources and
preserves durable task context. It never modifies original files, does
assignments, implements projects, or invokes a model.

```python
from ultimate_assistant.life.preparation import Store
from ultimate_assistant.life.preparation.service import Worker

store = Store()  # life_root()/preparation
source_id = store.register_source(r'C:\specific\notes', device='MSI')
context = store.save_context(
    'project', 'Unified Controller', next_action='Review mapping behavior',
    notes='Keep the controller running.', last_state='Awaiting user request')
store.save_context('project', 'Unified Controller', id=context['id'],
                   next_action='Discuss findings')
store.run_once()  # bounded source scans and at most three jobs
worker = Worker(store).start()  # optional in-process daemon thread
# At application shutdown:
worker.stop()
```

The worker polls queued jobs every 60 seconds and scans registered sources every
300 seconds. An empty source list generates no work. No startup task is installed.

## Records and queue

- `list_sources()` returns dictionaries with `id`, `path`, `device`, `enabled`,
  `state`, `error`, and `scanned` (Unix timestamp). Registering the same resolved
  directory updates its device/enabled fields. Missing directories can be
  registered and report `unavailable` until accessible.
- `scan_source(source_id, max_files=500, max_bytes=262144)` returns `changed`,
  `state`, and `files` or `error`. Partial scans preserve previously indexed
  files they did not reach. Complete scans retire deleted files. Unavailable
  sources retain their previously collected material.
- `list_materials(source_id=None)` returns active material dictionaries with
  `source`, relative `path`, SHA256 `hash`, bounded `excerpt`, `modified`, and
  `active`. Hidden/generated/credential directories, suspicious filenames,
  links outside the root, and oversized files are excluded. This is conservative
  filename filtering, not a guarantee that arbitrary notes contain no secrets.
- `save_context(kind,title,id=None,due=None,next_action='',notes='',last_state='')`
  accepts `task`, `project`, `appointment`, or `chore`, with optional ISO datetime.
  It returns the saved record including `revision` and `updated`.
  `list_context(kind=None)` returns current records;
  `context_history(id)` returns all revisions.
- `enqueue('source_digest', {'source': source_id}, dedupe='digest:'+source_id)`
  or `enqueue('scan_source', {'source': source_id}, dedupe='scan:'+source_id)`
  returns a job ID. Other job types are rejected. Active dedupe keys suppress
  duplicate jobs; changes arriving during execution schedule another pass.
- `claim(owner, lease_seconds=120)` returns a job dictionary with decoded
  `payload`, or `None`. Expired running jobs are recoverable by another worker.
  `complete(id,owner,result=None,error=None)` checks ownership and the unexpired
  lease. `cancel(id)` prevents subsequent completion. Both return booleans.
- `list_jobs()` returns up to 100 recent queue records with `state`, `attempts`,
  serialized `payload`/`result`, and `error` for visibility.
- `run_once(scan=True,max_jobs=3)` returns per-scan and per-job result dictionaries.
  A completed digest result contains `artifact` (absolute path) and `materials`.
  Digests reside at `artifacts/YYYY-MM-DD/<source-id>.md`, contain provenance and
  source hashes, and label excerpts/review prompts as unverified learning material.
  Repeated scans are idempotent; no model-generated flashcards are implied.

Source registration is local to the device running this store. An MSI label does
not transfer files from MSI or authenticate to a school portal.
