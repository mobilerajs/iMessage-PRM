# First-Run Onboarding Screen Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When no index exists, the app shows a Setup screen that bootstraps from the live Messages DB (one click, Full Disk Access aware) or from a user-provided folder (no FDA) — so a new user never hand-copies files.

**Architecture:** Three new Flask endpoints (`/api/setup/status`, `/api/setup/from-mac`, `/api/setup/from-folder`) reuse the existing `snapshot_live_db()`, `sync_contacts_live()`, `_run_build()`, and `job_set`/`job_get` background-job machinery. The decision logic (does setup proceed? is FDA the blocker? is a chosen folder valid?) is factored into **pure functions** that are unit-tested; the live read / AppleScript / browser flow are verified manually. The frontend gates its existing `load()` on `/api/setup/status` and renders a Setup screen instead of the table when `needs_setup`.

**Tech Stack:** Python 3.14 (arm64 venv), Flask (use `app.test_client()` for endpoint tests), SQLite backup API, vanilla JS, pytest.

**Conventions (every task):**
- Run from project root `~/Dev/iMessage CRM/`. Python = `.venv/bin/python`; tests = `.venv/bin/python -m pytest -q`; JS = `node --check app.js`.
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- PUBLIC repo: no real names/phones/secrets in code or tests; tests use tmp dirs + synthetic files.
- Baseline: **106 tests passing.**
- Key existing anchors (verified): `CHAT_DB` (server.py:23), `LIVE_CHAT_DB` (server.py:26), `job_set`/`job_get` (server.py:161/170), `_run_build()` (server.py:175, returns `(ok, error)`), `sync_contacts_live()` (server.py:782, returns `{"synced", "contacts"}` or `{"synced": False, "reason"}`), `snapshot_live_db()` (server.py:937, returns `{"snapshotted": bool, "reason": str}`, never raises), the `/api/refresh` job shape to mirror (server.py:994+), and frontend `load()` (app.js:280) called at the bottom of app.js.

---

### Task 1: Pure setup decision helpers

**Files:**
- Modify: `server.py` (add three module-level functions near the other helpers, e.g. after `job_get`/`_run_build` ~server.py:190)
- Test: `tests/test_setup.py`

**Step 1: Write the failing tests**

```python
# tests/test_setup.py
import os, importlib

def _server():
    import server; return server

def test_needs_setup_true_when_people_missing(tmp_path, monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "PEOPLE_OUT", str(tmp_path / "nope.json"))
    assert s.needs_setup() is True

def test_needs_setup_false_when_people_present(tmp_path, monkeypatch):
    s = _server()
    p = tmp_path / "people.json"; p.write_text("[]")
    monkeypatch.setattr(s, "PEOPLE_OUT", str(p))
    assert s.needs_setup() is False

def test_classify_snapshot_proceeds_when_snapshotted():
    s = _server()
    d = s.classify_snapshot_for_setup({"snapshotted": True, "reason": "ok"}, chat_db_exists=False)
    assert d["proceed"] is True and d["fda_needed"] is False

def test_classify_snapshot_proceeds_on_existing_copy_when_not_snapshotted():
    s = _server()
    d = s.classify_snapshot_for_setup({"snapshotted": False, "reason": "x"}, chat_db_exists=True)
    assert d["proceed"] is True and d["fda_needed"] is False

def test_classify_snapshot_fda_needed_when_unreadable_and_no_copy():
    s = _server()
    d = s.classify_snapshot_for_setup(
        {"snapshotted": False, "reason": "live Messages DB not readable (grant Full Disk Access)"},
        chat_db_exists=False)
    assert d["proceed"] is False and d["fda_needed"] is True

def test_classify_snapshot_not_found_is_blocked_but_not_fda():
    s = _server()
    d = s.classify_snapshot_for_setup(
        {"snapshotted": False, "reason": "live Messages DB not found"}, chat_db_exists=False)
    assert d["proceed"] is False and d["fda_needed"] is False  # use the folder path instead

def test_validate_folder_ok(tmp_path):
    s = _server()
    (tmp_path / "chat.db").write_text("x")
    ok, info, err = s.validate_setup_folder(str(tmp_path))
    assert ok and info["chat_db"].endswith("chat.db") and info["contacts"] is None and err == ""

def test_validate_folder_picks_up_contacts(tmp_path):
    s = _server()
    (tmp_path / "chat.db").write_text("x"); (tmp_path / "contacts.vcf").write_text("y")
    ok, info, _ = s.validate_setup_folder(str(tmp_path))
    assert ok and info["contacts"].endswith("contacts.vcf")

def test_validate_folder_missing_dir(tmp_path):
    s = _server()
    ok, info, err = s.validate_setup_folder(str(tmp_path / "ghost"))
    assert ok is False and "not found" in err.lower()

def test_validate_folder_no_chatdb(tmp_path):
    s = _server()
    ok, info, err = s.validate_setup_folder(str(tmp_path))
    assert ok is False and "chat.db" in err
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_setup.py -v`
Expected: FAIL (`needs_setup`/`classify_snapshot_for_setup`/`validate_setup_folder`/`PEOPLE_OUT` not defined).

**Step 3: Implement** — add to `server.py`. First confirm/define a `PEOPLE_OUT` constant near the other `out/` paths (there's already `STATS_OUT`; add `PEOPLE_OUT = os.path.join(HERE, "out", "people.json")` if not present). Then:

```python
def needs_setup():
    """True on first run: the built index doesn't exist yet."""
    return not os.path.exists(PEOPLE_OUT)


def classify_snapshot_for_setup(snap, chat_db_exists):
    """Decide what the from-mac setup job should do, given a snapshot_live_db()
    result and whether a working copy already exists. Pure (no I/O).
      - snapshotted -> proceed.
      - not snapshotted but a working copy exists -> proceed (build that copy).
      - not snapshotted and no copy -> blocked; fda_needed when the reason looks
        like a permission/readability problem (vs the live DB simply not existing,
        in which case the user should use the folder path)."""
    if snap.get("snapshotted"):
        return {"proceed": True, "fda_needed": False, "reason": snap.get("reason", "")}
    if chat_db_exists:
        return {"proceed": True, "fda_needed": False, "reason": snap.get("reason", "")}
    reason = (snap.get("reason") or "").lower()
    fda = ("full disk access" in reason or "readable" in reason
           or "not permitted" in reason or "unable to open" in reason)
    return {"proceed": False, "fda_needed": fda, "reason": snap.get("reason", "")}


def validate_setup_folder(folder):
    """Validate a user-provided folder for the no-FDA setup path. Returns
    (ok, info|None, error). info = {chat_db, contacts|None}."""
    if not folder or not str(folder).strip():
        return False, None, "no folder given"
    path = os.path.abspath(os.path.expanduser(str(folder).strip()))
    if not os.path.isdir(path):
        return False, None, "folder not found"
    chat = os.path.join(path, "chat.db")
    if not os.path.isfile(chat):
        return False, None, "no chat.db in that folder"
    contacts = os.path.join(path, "contacts.vcf")
    return True, {"chat_db": chat, "contacts": contacts if os.path.isfile(contacts) else None}, ""
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_setup.py -v` → PASS (10 tests).
Run: `.venv/bin/python -m pytest -q` → 116 passing.

**Step 5: Commit**

```bash
git add server.py tests/test_setup.py
git commit -m "feat(setup): pure helpers — needs_setup, snapshot classifier, folder validator"
```

---

### Task 2: `GET /api/setup/status`

**Files:**
- Modify: `server.py` (new route; place near the other `/api/*` routes)
- Test: `tests/test_setup.py`

**Step 1: Write the failing test**

```python
def test_setup_status_endpoint(tmp_path, monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "PEOPLE_OUT", str(tmp_path / "nope.json"))
    client = s.app.test_client()
    r = client.get("/api/setup/status")
    body = r.get_json()
    assert r.status_code == 200
    assert body["needs_setup"] is True
    assert "fda_ok" in body and "chat_db_present" in body
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_setup.py::test_setup_status_endpoint -v` → FAIL (404).

**Step 3: Implement**

```python
@app.route("/api/setup/status")
def setup_status():
    """First-run probe for the onboarding screen. fda_ok is a cheap read check of
    the live DB (False usually means Full Disk Access isn't granted)."""
    fda_ok = os.path.exists(LIVE_CHAT_DB) and os.access(LIVE_CHAT_DB, os.R_OK)
    return jsonify(needs_setup=needs_setup(),
                   fda_ok=bool(fda_ok),
                   chat_db_present=os.path.exists(CHAT_DB))
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_setup.py -v` → PASS.

**Step 5: Commit**

```bash
git add server.py tests/test_setup.py
git commit -m "feat(setup): GET /api/setup/status"
```

---

### Task 3: `POST /api/setup/from-mac` (background job, FDA-aware)

**Files:**
- Modify: `server.py`
- Test: manual + the pure classifier already covers the decision logic

**Step 1: Implement** — mirror `/api/refresh`, but gate on `classify_snapshot_for_setup` so a fresh install with no FDA does NOT run a doomed build:

```python
@app.route("/api/setup/from-mac", methods=["POST"])
def setup_from_mac():
    """First-run setup from the live Messages DB on THIS Mac. Snapshot (read-only)
    -> if blocked by Full Disk Access (and no working copy yet), stop with
    fda_needed so the UI can guide the grant -> else sync Contacts + build."""
    job_id = uuid.uuid4().hex[:8]
    job_set(job_id, {"state": "running", "message": "checking access", "result": None})

    def run():
        try:
            snap = snapshot_live_db()  # never raises
            decision = classify_snapshot_for_setup(snap, os.path.exists(CHAT_DB))
            if not decision["proceed"]:
                JOBS[job_id].update(
                    state="error",
                    message=("fda_needed" if decision["fda_needed"] else "no_live_db"),
                    result={"fda_needed": decision["fda_needed"], "reason": decision["reason"]})
                return
            JOBS[job_id]["message"] = "syncing contacts"
            sync_contacts_live()  # best effort
            JOBS[job_id]["message"] = "building index"
            ok, err = _run_build()
            if not ok:
                JOBS[job_id].update(state="error", message=err or "build failed")
                return
            JOBS[job_id].update(state="done", message="done", result={"ok": True})
        except Exception as exc:  # belt and suspenders
            JOBS[job_id].update(state="error", message=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=job_id)
```

**Step 2: Verify**

Run: `.venv/bin/python -c "import ast; ast.parse(open('server.py').read()); print('ok')"`.
Run: `.venv/bin/python -m pytest -q` → still green (116).

**Step 3: Manual smoke (since out/ already exists here, this just confirms the route wires up):**
Restart server on :8001, then:
```bash
curl -s -XPOST localhost:8001/api/setup/from-mac | python3 -m json.tool   # -> {"job_id": "..."}
curl -s localhost:8001/api/job/<job_id> | python3 -m json.tool            # poll to done/error
```
(On this dev Mac with FDA + existing data it should proceed to done. Don't worry about re-running a full build here; the point is the route returns a job_id and the job transitions.)

**Step 4: Commit**

```bash
git add server.py
git commit -m "feat(setup): POST /api/setup/from-mac (FDA-aware live bootstrap job)"
```

---

### Task 4: `POST /api/setup/from-folder` (no-FDA path)

**Files:**
- Modify: `server.py`
- Test: `tests/test_setup.py`

**Step 1: Write the failing test** (validation path, via test_client — no build runs because validation fails first):

```python
def test_setup_from_folder_rejects_bad_folder(tmp_path):
    s = _server()
    client = s.app.test_client()
    r = client.post("/api/setup/from-folder", json={"folder": str(tmp_path / "ghost")})
    assert r.status_code == 400
    assert "not found" in r.get_json()["error"].lower()

def test_setup_from_folder_rejects_missing_chatdb(tmp_path):
    s = _server()
    client = s.app.test_client()
    r = client.post("/api/setup/from-folder", json={"folder": str(tmp_path)})
    assert r.status_code == 400 and "chat.db" in r.get_json()["error"]
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_setup.py -k from_folder -v` → FAIL (404).

**Step 3: Implement** — validate on the request thread (fast), copy, then background-build:

```python
@app.route("/api/setup/from-folder", methods=["POST"])
def setup_from_folder():
    """No-FDA setup: the user copied chat.db (and optionally contacts.vcf) into an
    unprotected folder. Validate, copy into data/, then background-build."""
    body = request.get_json(force=True, silent=True) or {}
    ok, info, err = validate_setup_folder(body.get("folder"))
    if not ok:
        return jsonify(error=err), 400
    import shutil
    os.makedirs(os.path.dirname(CHAT_DB), exist_ok=True)
    try:
        shutil.copy2(info["chat_db"], CHAT_DB)
        if info["contacts"]:
            shutil.copy2(info["contacts"], os.path.join(HERE, "data", "contacts.vcf"))
    except OSError as exc:
        return jsonify(error=f"copy failed: {exc}"), 500

    job_id = uuid.uuid4().hex[:8]
    job_set(job_id, {"state": "running", "message": "building index", "result": None})

    def run():
        ok2, err2 = _run_build()
        if ok2:
            JOBS[job_id].update(state="done", message="done", result={"ok": True})
        else:
            JOBS[job_id].update(state="error", message=err2 or "build failed")

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=job_id)
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_setup.py -v` → PASS (all setup tests).
Run: `.venv/bin/python -m pytest -q` → green (118).

**Step 5: Commit**

```bash
git add server.py tests/test_setup.py
git commit -m "feat(setup): POST /api/setup/from-folder (no-FDA folder bootstrap)"
```

---

### Task 5: Setup screen frontend

**Files:**
- Modify: `index.html` (add a hidden `#setup` screen section), `app.js` (gate `load()` on status; render + wire the screen), `styles.css` (setup screen styling)
- Test: `node --check app.js` + manual

**Step 1: Add the Setup screen markup** to `index.html` (inside `#app`, before `#table-scroll`, hidden by default):

```html
<section id="setup" hidden>
  <h2>Welcome to iMessage PRM</h2>
  <p>Everything runs on <strong>this Mac</strong>. Your messages and contacts never leave it.</p>
  <div class="setup-actions">
    <button id="setup-mac" class="primary">Set up from this Mac</button>
    <p class="setup-hint">Reads your Messages + Contacts on this Mac (needs Full Disk Access — we'll show you how).</p>
  </div>
  <details id="setup-folder-wrap">
    <summary>Or point at a folder (no Full Disk Access)</summary>
    <p class="setup-hint">Copy <code>chat.db</code> (and optionally <code>contacts.vcf</code>) into a folder
       like your Desktop, then give the path:</p>
    <input id="setup-folder" type="text" placeholder="/Users/you/Desktop/imsg" />
    <button id="setup-folder-go">Use this folder</button>
  </details>
  <div id="setup-fda" hidden>
    <h3>Grant Full Disk Access</h3>
    <ol>
      <li>Open <strong>System Settings → Privacy &amp; Security → Full Disk Access</strong>.</li>
      <li>Enable your terminal app (Terminal / iTerm), then quit &amp; reopen it.</li>
      <li>Come back and click <strong>Try again</strong>.</li>
    </ol>
    <button id="setup-retry">Try again</button>
  </div>
  <div id="setup-status" aria-live="polite"></div>
</section>
```

**Step 2: Gate `load()` on setup status** (app.js:280). At the top of `load()`, before fetching people.json:

```javascript
  // First run: no index yet -> show the Setup screen instead of the table.
  try {
    const st = await fetch("/api/setup/status").then((r) => r.json());
    if (st.needs_setup) { showSetup(); return; }
  } catch (e) { /* not the live server (static mode) — fall through to normal load */ }
```

**Step 3: Implement the setup controller** in app.js (reuse the existing `pollJob` helper):

```javascript
function showSetup() {
  document.getElementById("table-scroll").hidden = true;
  const head = document.getElementById("controls"); if (head) head.hidden = true;
  document.getElementById("setup").hidden = false;
  const ss = document.getElementById("setup-status");
  const fda = document.getElementById("setup-fda");

  function runJob(promise) {
    ss.textContent = "Working…"; fda.hidden = true;
    promise.then((r) => r.json()).then((j) => {
      if (j.error) { ss.textContent = j.error; return; }
      pollJob(j.job_id, {
        onProgress: (job) => { ss.textContent = job.message || "Working…"; },
        onDone: () => { ss.textContent = "Done — loading…"; location.reload(); },
        onError: (job) => {
          if (job.message === "fda_needed") { fda.hidden = false; ss.textContent = "Full Disk Access needed."; }
          else { ss.textContent = "Setup failed: " + (job.message || "unknown"); }
        },
      });
    }).catch((e) => { ss.textContent = "Setup failed: " + e; });
  }

  const go = () => runJob(fetch("/api/setup/from-mac", { method: "POST" }));
  document.getElementById("setup-mac").onclick = go;
  document.getElementById("setup-retry").onclick = go;
  document.getElementById("setup-folder-go").onclick = () => {
    const folder = document.getElementById("setup-folder").value.trim();
    if (!folder) { ss.textContent = "Enter a folder path."; return; }
    runJob(fetch("/api/setup/from-folder", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder }) }));
  };
}
```

> Confirm `pollJob`'s callback signature matches (it was added earlier with
> `{onProgress, onDone, onError}`). If the names differ, adapt to the real helper.

**Step 4: Style the setup screen** in styles.css — a centered card consistent with the existing palette (`--surface`, `--radius`, `--blue`); `.primary` button uses `--blue`; `.setup-hint` muted; `#setup-fda` a subtle bordered callout. Keep it simple.

**Step 5: Verify**

Run: `node --check app.js` → clean.
Manual (safe, reversible — temporarily simulate first run):
```bash
mv out/people.json out/people.json.keep    # simulate no index
# reload http://localhost:8001 -> Setup screen appears
mv out/people.json.keep out/people.json     # RESTORE immediately
```
Confirm the Setup screen renders, "Set up from this Mac" starts a job (and on this FDA-granted Mac, completes + reloads), and the folder field validates. **Always restore `out/people.json`.**

**Step 6: Commit**

```bash
git add index.html app.js styles.css
git commit -m "feat(setup): first-run Setup screen (from-Mac + folder, FDA guidance)"
```

---

### Task 6: Final pass — boot-with-no-index, docs, privacy gate

**Step 1: Confirm the server boots and serves the Setup path with no index.** Safely simulate:
```bash
mv out out._keep
.venv/bin/python -c "import ast; ast.parse(open('server.py').read())"   # sanity
# start server, curl /api/setup/status -> needs_setup true; curl / -> 200 (index.html)
mv out._keep out      # RESTORE
```
Confirm no endpoint 500s purely because `out/` is absent (status route, index.html, app.js, styles.css must all still serve). If any handler assumes `out/` files at import/boot, make it lazy/tolerant.

**Step 2: README** — replace the manual "two source files" framing in the quick-start with the new reality: *first run shows a Setup screen; click "Set up from this Mac" (grant Full Disk Access when prompted), or point it at a folder where you copied `chat.db`.* Keep the manual/env-var notes as the advanced path. Mention FDA is required for the live path and unavoidable (OS-level).

**Step 3: Run the full suite + JS check**

Run: `.venv/bin/python -m pytest -q` → green (target ~118+).
Run: `node --check app.js` → clean.

**Step 4: Privacy gate** (public repo): `git diff origin/main..HEAD` added lines have no real phones/names/secrets; `git status` clean; `data/`, `out/`, `config.json` still gitignored and untracked. Tests use only tmp dirs + synthetic files.

**Step 5: Commit the README**

```bash
git add README.md
git commit -m "docs: onboarding screen replaces manual file setup in quick-start"
```

**Step 6: Pause for explicit user approval before `git push`** (public repo).

---

## Task summary

| # | Task | Files | Tests |
|---|------|-------|-------|
| 1 | Pure setup helpers | server.py | test_setup.py (10) |
| 2 | GET /api/setup/status | server.py | test_setup.py |
| 3 | POST /api/setup/from-mac | server.py | manual + classifier |
| 4 | POST /api/setup/from-folder | server.py | test_setup.py |
| 5 | Setup screen frontend | index.html, app.js, styles.css | node --check + manual |
| 6 | Final pass + docs + privacy | README.md | full suite |

**Reused (already built):** `snapshot_live_db()`, `sync_contacts_live()`, `_run_build()`, `job_set`/`job_get`, `/api/job/<id>`, `pollJob`, blocking-overlay styles.
