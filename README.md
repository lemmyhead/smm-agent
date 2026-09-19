# SMM drafting agent

Brief in → per-platform drafts + cover images → Telegram notification for a human reviewer. No auto-publishing.

```
POST /briefs ─► brief-intake (Cloud Run) ─► Firestore jobs/{id}
                      │                          ▲   │ status change
                      ▼                          │   ▼
             orchestrator-agent ──► render-service   notify-function ─► Telegram
             (Agent Engine, ADK)    (Cloud Run, Chromium)
```

- Deploy: see [DEPLOY.md](DEPLOY.md) (one script per service in `scripts/`).
- Brand context (voice guide, example posts, visual defaults) is loaded through the Telegram bot (`telegram-intake`) or `scripts/brand-context.sh`; see DEPLOY.md step 5.
- Platform sizes, copy limits and template names live once in `shared/platform_specs.yaml`.
  `scripts/sync-shared.sh` copies `shared/` into the services before each deploy.
- Tests (Python 3.12): `pip install -r <service>/requirements.txt pytest pytest-asyncio` and run `pytest` in each service folder.
  The render-service tests need `playwright install chromium`.
