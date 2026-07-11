# scripts/

Maintainer / tester helpers. Not part of the plugin runtime.

| Script | Purpose |
|---|---|
| `create-test-workstation.sh` | Spin up a throwaway GCP Cloud Workstation to try `agy` end-to-end. |
| `bump-version.sh` | Sync the version across all pinned files before a release. |

---

## Try `agy` in a throwaway GCP Cloud Workstation

> ⚠️ **Billable.** This creates real GCP infrastructure. The workstation **cluster**
> alone is ~$0.20/hr (~$144/month) and bills 24/7 until you delete it. Read the cost
> notes at the top of `create-test-workstation.sh` and run the teardown when done.

### 1. Create it (from your machine)

Authenticate first, then run:

```bash
gcloud auth login
gcloud auth application-default login
PROJECT=your-project REGION=us-central1 ./scripts/create-test-workstation.sh
```

It enables the API and creates a cluster + config + workstation, then prints the
browser/SSH access. Cluster creation can take up to ~20 minutes.

### 2. On the workstation — install Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash   # or: npm i -g @anthropic-ai/claude-code
```

### 3. On the workstation — point Claude Code at Claude on Vertex AI

Add to `~/.bashrc` (or the `env` block of `~/.claude/settings.json`):

```bash
export CLAUDE_CODE_USE_VERTEX=1
export ANTHROPIC_VERTEX_PROJECT_ID="$(gcloud config get-value project)"
export CLOUD_ML_REGION=global
export ANTHROPIC_MODEL=claude-sonnet-4-6   # any Claude model served on Vertex
```

Then `gcloud auth application-default login` so Vertex has credentials.

### 4. Install and use agy

```
/plugin marketplace add jinseo-jang/antigravity-plugin-cc
/plugin install agy@agy
```

The plugin's **worker** uses Gemini via Vertex ADC — with `gcloud auth application-default login`
done, no extra env vars are needed (see the README's Auth section). Then:

```
/agy:implement "add a hello-world endpoint"
```

### 5. Teardown (stops all charges)

Delete the cluster with the **same PROJECT / CLUSTER / REGION you created it with**
(deleting the cluster removes the config, workstation, and disks, and stops the 24/7
control-plane fee):

```bash
gcloud workstations clusters delete agy-test-cluster \
  --project=your-project --region=us-central1
```
