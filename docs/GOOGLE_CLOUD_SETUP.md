# Google Cloud Vertex AI Setup

This project uses Google Gemini via Vertex AI to automatically classify
conference track listings, filtering out workshops, tutorials, and secondary
tracks so that only main-conference papers are scraped.

---

## 1. Google Cloud Prerequisites

1. **Create a project** in the [Google Cloud Console](https://console.cloud.google.com/).
2. **Enable billing** — the project must be linked to an active billing account,
   even if you are using free credits (`Billing > Manage linked accounts`).
3. **Enable the Vertex AI API** — go to `APIs & Services > Library`, search for
   `Vertex AI API`, and click Enable.

---

## 2. Local Authentication

The scraper uses **Application Default Credentials (ADC)** — no API key is needed.

```bash
# 1. Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install

# 2. Log in to your Google account
gcloud auth login

# 3. Configure Application Default Credentials
gcloud auth application-default login

# 4. Set the quota project (required for correct billing/credit attribution)
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

---

## 3. Environment Variables

Create a `.env` file in the project root:

```env
GCP_PROJECT_ID=your-project-id
GCP_LOCATION=us-central1
GEMINI_MODEL=gemini-2.5-flash
```

- `GCP_LOCATION`: `us-central1` or `us-east1` are recommended.
- `GEMINI_MODEL`: `gemini-2.5-flash` is recommended for speed and cost.

---

## 4. How Track Labeling Works

Five scrapers use Gemini to filter conference proceedings down to main-track
papers only: **AAAI**, **ACL**, **EMNLP**, **NAACL**, and **IJCAI**.

On first run for a given conference year, each scraper:

1. Fetches the list of issues or track sections from the conference's
   proceedings page.
2. Sends the list to Gemini, which returns a JSON object identifying which
   entries are main-conference proceedings.
3. Caches the result in `data/cache/{conference}_tracks.json` (AAAI also uses
   a separate `data/cache/aaai_pages.json` for issue-level filtering).

On subsequent runs, the cached result is used directly — no API call is made.

**Manual overrides**: if Gemini misclassifies a track or issue, edit the
relevant cache file directly (set `"is_full_regular": false` → `true`, or
`"is_main_aaai": false` → `true` for AAAI) and rerun. The scraper will use
your correction without calling the API again.

**AAAI two-level filtering**: AAAI requires an extra level of classification.
`aaai_pages.json` labels whole issues (e.g. "is this a main AAAI issue for
year X?"), then `aaai_tracks.json` labels sections within each issue (e.g.
"is this section IAAI/EAAI or main AAAI?"). Both caches are incremental —
only new entries are sent to the API on each run.

---

## 5. Troubleshooting

| Issue | Solution |
|-------|----------|
| `404 Publisher Model Not Found` | Check that your project ID is correct and billing is enabled. Try changing `GCP_LOCATION` to `us-east1`. |
| `Permission Denied` | Rerun the `set-quota-project` command from Section 2. |
| `Listed 0 items` | The Vertex AI API may not be fully initialized in that region. Visit the [Vertex AI Model Garden](https://console.cloud.google.com/vertex-ai/model-garden) and enable any Gemini model. |