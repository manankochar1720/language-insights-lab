# Language Insights Lab — Runbook (HTML frontend + Python backend)

A single self-contained HTML page that runs pasted text through six Azure
AI Language capabilities, deployed on **Azure Static Web Apps** (free tier)
with a **Python** Azure Function as the backend.

Extends the original [language-app](https://github.com/manankochar1720/language-app)
(sentiment, key phrases, entities) with three new capabilities:

- **Language detection** — identifies the language and a confidence score
- **PII redaction** — finds and masks personal data (names, emails, phone
  numbers, etc.) and returns a redacted copy of the text
- **Abstractive summarization** — a short natural-language summary of the
  passage, generated as an async job (this is the one action Azure AI
  Language doesn't support synchronously)

```
language-insights-lab/
├── src/
│   └── index.html                 ← entire frontend: HTML + CSS + JS, one file
├── api/                            ← Python Azure Functions backend
│   ├── analyze/
│   │   ├── __init__.py             ← POST /api/analyze handler, all 6 actions
│   │   └── function.json           ← HTTP trigger binding
│   ├── host.json
│   ├── requirements.txt            ← just azure-functions
│   └── local.settings.json.example ← copy → local.settings.json for local dev
├── staticwebapp.config.json
├── .github/workflows/azure-static-web-apps.yml
└── .gitignore
```

No build step on either side: the frontend is plain HTML/CSS/JS in one
file, the backend is plain Python calling the Azure AI Language REST API
directly with `urllib` (no extra SDK dependency).

---

## 1. Create the Azure resources

### Resource group + Azure AI Language (free F0 tier)

```bash
az login
az account set --subscription "<your-subscription-name-or-id>"

RG="rg-language-insights"
LOCATION="eastus"
LANG_NAME="lang-insights-lab"
SWA_NAME="swa-language-insights"

az group create --name $RG --location $LOCATION

az cognitiveservices account create \
  --name $LANG_NAME \
  --resource-group $RG \
  --kind TextAnalytics \
  --sku F0 \
  --location $LOCATION \
  --yes

# Endpoint and key — paste these into env variables in step 3
az cognitiveservices account show \
  --name $LANG_NAME --resource-group $RG \
  --query "properties.endpoint" -o tsv

az cognitiveservices account keys list \
  --name $LANG_NAME --resource-group $RG \
  --query "key1" -o tsv
```

> F0 (free) allows one instance per subscription per region. If creation
> fails on quota, reuse an existing F0 resource or use the paid `S` tier.
> **Abstractive summarization requires the `S` (standard) tier or a region
> where it's supported on F0** — check availability before relying on F0
> for the summary action; the other five actions run fine on F0.

Portal equivalent: **Create a resource → "Language service"** → choose
Subscription/Resource group/Region → **Pricing tier** → Review + create →
open the resource → **Keys and Endpoint** to copy them.

### Static Web App (Python API)

```bash
az staticwebapp create \
  --name $SWA_NAME \
  --resource-group $RG \
  --location $LOCATION \
  --source https://github.com/<your-username>/language-insights-lab \
  --branch main \
  --app-location "/src" \
  --api-location "/api" \
  --output-location "" \
  --login-with-github
```

Portal equivalent: **Create a resource → "Static Web App"** → connect
GitHub/repo/branch → **App location**: `/src`, **Api location**: `/api`,
**Output location**: *(blank)* → Review + create.

---

## 2. Set your endpoint and key as environment variables

1. Open the **Static Web App** resource → left menu → **Settings →
   Environment variables** (sometimes labeled **Configuration**).
2. Add two **Application settings**:
   - `LANGUAGE_ENDPOINT` = `https://<your-language-resource>.cognitiveservices.azure.com`
   - `LANGUAGE_KEY` = `<key1 from your Language resource>`
3. **Save.** These become `os.environ["LANGUAGE_ENDPOINT"]` and
   `os.environ["LANGUAGE_KEY"]` inside `api/analyze/__init__.py` — no
   redeploy needed.

CLI equivalent:

```bash
az staticwebapp appsettings set \
  --name $SWA_NAME \
  --setting-names \
    LANGUAGE_ENDPOINT="https://<lang-name>.cognitiveservices.azure.com" \
    LANGUAGE_KEY="<key1>"
```

**Never commit the key to the repo.** It only ever lives in this
environment-variable setting (and, for local testing, in your
git-ignored `api/local.settings.json`).

---

## 3. Push the code to GitHub

```bash
cd language-insights-lab
git init
git add .
git commit -m "Language Insights Lab: language ID, sentiment, key phrases, entities, PII redaction, summarization"
git branch -M main
git remote add origin https://github.com/<your-username>/language-insights-lab.git
git push -u origin main
```

If you created the Static Web App via CLI/portal with GitHub login, it
already pushed its own workflow file. If you're using the one included
here instead, make sure a repo secret named
`AZURE_STATIC_WEB_APPS_API_TOKEN` exists:

```bash
az staticwebapp secrets list --name $SWA_NAME --query "properties.apiKey" -o tsv
```

Paste that value into **GitHub repo → Settings → Secrets and variables →
Actions → New repository secret** → name it
`AZURE_STATIC_WEB_APPS_API_TOKEN`.

Every push to `main` now triggers
`.github/workflows/azure-static-web-apps.yml`.

---

## 4. Verify the deployment

1. GitHub repo → **Actions** tab → confirm the run is green.
2. Azure Portal → Static Web App → **Overview** → open the URL.
3. Paste text, tick the actions you want, click **Analyze text**. You
   should see: a language-ID card, a sentiment stamp with confidence
   bars, key phrases highlighted in place, named-entity chips, a
   redacted-text card, and an abstractive summary.

If `/api/analyze` returns a 500 with a "missing LANGUAGE_ENDPOINT /
LANGUAGE_KEY" message, the environment variables from Step 2 aren't set
yet. If only the **summary** action errors while the other five work,
your Language resource's tier/region likely doesn't support abstractive
summarization — try the `S` tier.

---

## 5. Local development (optional)

```bash
# Frontend only — no API calls will work, but you can see the layout
cd src && npx serve .

# Full stack with the Functions emulator
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r api/requirements.txt
npm install -g azure-functions-core-tools@4 @azure/static-web-apps-cli

cp api/local.settings.json.example api/local.settings.json
# edit api/local.settings.json with your real endpoint/key

swa start src --api-location api
```

`swa start` serves the frontend and proxies `/api/*` to the local Python
Functions host, mirroring production routing.

---

## 6. How the API request/response works

Request:

```json
POST /api/analyze
{
  "text": "…up to 5,120 characters…",
  "actions": ["language", "sentiment", "keyPhrases", "entities", "pii", "summary"]
}
```

`actions` is optional — omit it to run all six. Language detection runs
first when requested; if it succeeds, its detected language is used as
the `language` hint for the remaining calls (falls back to `"en"`
otherwise).

Response:

```json
{
  "results": {
    "language": { "name": "English", "iso6391Name": "en", "confidenceScore": 1.0 },
    "sentiment": { "sentiment": "positive", "confidenceScores": {...}, "sentences": [...] },
    "keyPhrases": { "keyPhrases": ["wireless earbuds", "battery life", "..."] },
    "entities": { "entities": [{ "text": "Rohan Mehta", "category": "Person", "...": "..." }] },
    "pii": { "redactedText": "Hi, this is *** ***** from ***...", "entities": [...] },
    "summary": { "summaries": ["A customer praises the battery life and noise cancellation..."] }
  },
  "detectedLanguage": "en",
  "truncated": false,
  "errors": {}
}
```

Each action is called against Azure's synchronous
`/language/:analyze-text` endpoint, except **summary**, which uses the
async `/language/analyze-text/jobs` endpoint and is polled server-side
until it completes (or ~55s elapses, whichever comes first).

---

## 7. Cost / quota notes

- **Azure AI Language F0**: 5,000 text records/month free, then blocked
  until next cycle (no overage charge), or upgrade to `S` tier.
- **Static Web Apps Free tier**: 100 GB bandwidth/month, no SLA — fine
  for a demo or workshop.
- Each full "Analyze text" click with all six actions checked costs up
  to **6 records** against the monthly quota (5 sync calls + 1 summary
  job). Untick actions you don't need to conserve quota.

---

## 8. Cleanup

```bash
az group delete --name $RG --yes --no-wait
```

Removes the Language resource, the Static Web App, and everything else
in the resource group in one shot.
