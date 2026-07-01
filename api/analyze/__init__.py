"""
POST /api/analyze
Runs one or more Azure AI Language actions against a piece of text:

  - language     -> Language Detection
  - sentiment    -> Sentiment Analysis
  - keyPhrases   -> Key Phrase Extraction
  - entities     -> Named Entity Recognition
  - pii          -> PII Entity Recognition / redaction
  - summary      -> Abstractive Summarization (async job, polled to completion)

Request body:
{
  "text": "...",
  "actions": ["language", "sentiment", "keyPhrases", "entities", "pii", "summary"],
  "language": "en",              // optional fallback / hint, default "en"
  "useDetectedLanguage": true    // optional, default true
}

If "actions" is omitted, all six actions run.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

import azure.functions as func

API_VERSION = "2023-04-01"
MAX_CHARS = 5120  # single-document sync limit for Azure AI Language


def _credentials():
    endpoint = os.environ.get("LANGUAGE_ENDPOINT", "").rstrip("/")
    key = os.environ.get("LANGUAGE_KEY", "")
    return endpoint, key


def _post(url, key, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Ocp-Apim-Subscription-Key", key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        headers = dict(resp.headers)
        payload = json.loads(resp.read().decode("utf-8")) if resp.length != 0 else {}
        return resp.status, payload, headers


def _get(url, key):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Ocp-Apim-Subscription-Key", key)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _sync_call(endpoint, key, kind, text, language, parameters=None):
    url = f"{endpoint}/language/:analyze-text?api-version={API_VERSION}"
    document = {"id": "1", "text": text}
    if kind != "LanguageDetection":
        document["language"] = language
    body = {"kind": kind, "analysisInput": {"documents": [document]}}
    if parameters:
        body["parameters"] = parameters
    _, payload, _ = _post(url, key, body)
    return payload["results"]["documents"][0]


# ---- individual actions -----------------------------------------------

def language_detection(endpoint, key, text, language):
    doc = _sync_call(endpoint, key, "LanguageDetection", text, language)
    detected = doc["detectedLanguage"]
    return {
        "name": detected["name"],
        "iso6391Name": detected["iso6391Name"],
        "confidenceScore": detected["confidenceScore"],
    }


def sentiment_analysis(endpoint, key, text, language):
    doc = _sync_call(
        endpoint, key, "SentimentAnalysis", text, language,
        parameters={"opinionMining": False},
    )
    return {
        "sentiment": doc["sentiment"],
        "confidenceScores": doc["confidenceScores"],
        "sentences": [
            {
                "text": s["text"],
                "sentiment": s["sentiment"],
                "confidenceScores": s["confidenceScores"],
            }
            for s in doc["sentences"]
        ],
    }


def key_phrase_extraction(endpoint, key, text, language):
    doc = _sync_call(endpoint, key, "KeyPhraseExtraction", text, language)
    return {"keyPhrases": doc["keyPhrases"]}


def entity_recognition(endpoint, key, text, language):
    doc = _sync_call(endpoint, key, "EntityRecognition", text, language)
    return {
        "entities": [
            {
                "text": e["text"],
                "category": e["category"],
                "subcategory": e.get("subcategory"),
                "confidenceScore": e["confidenceScore"],
                "offset": e["offset"],
                "length": e["length"],
            }
            for e in doc["entities"]
        ]
    }


def pii_redaction(endpoint, key, text, language):
    doc = _sync_call(endpoint, key, "PiiEntityRecognition", text, language)
    return {
        "redactedText": doc["redactedText"],
        "entities": [
            {
                "text": e["text"],
                "category": e["category"],
                "subcategory": e.get("subcategory"),
                "confidenceScore": e["confidenceScore"],
            }
            for e in doc["entities"]
        ],
    }


def abstractive_summary(endpoint, key, text, language, sentence_count=3, max_wait_seconds=55):
    """Abstractive summarization only exists as an async job in Azure AI
    Language, so this kicks the job off and polls operation-location until
    it succeeds, fails, or we run out of patience (Functions HTTP timeout)."""
    url = f"{endpoint}/language/analyze-text/jobs?api-version={API_VERSION}"
    body = {
        "displayName": "AbstractiveSummarization",
        "analysisInput": {"documents": [{"id": "1", "language": language, "text": text}]},
        "tasks": [
            {
                "kind": "AbstractiveSummarization",
                "parameters": {"sentenceCount": sentence_count},
            }
        ],
    }
    _, _, headers = _post(url, key, body)
    job_url = headers.get("operation-location") or headers.get("Operation-Location")
    if not job_url:
        raise RuntimeError("Azure did not return an operation-location header for the summarization job")

    waited = 0.0
    delay = 1.5
    while waited < max_wait_seconds:
        _, payload = _get(job_url, key)
        job_status = (payload.get("status") or "").lower()
        if job_status == "succeeded":
            task = payload["tasks"]["items"][0]
            summaries = task["results"]["documents"][0]["summaries"]
            return {"summaries": [s["text"] for s in summaries]}
        if job_status in ("failed", "partiallyfailed"):
            raise RuntimeError(f"Summarization job did not succeed: {json.dumps(payload)[:500]}")
        time.sleep(delay)
        waited += delay
        delay = min(delay * 1.4, 4)
    raise TimeoutError("Summarization job is still running; try a shorter passage or try again")


ACTIONS = {
    "sentiment": sentiment_analysis,
    "keyPhrases": key_phrase_extraction,
    "entities": entity_recognition,
    "pii": pii_redaction,
    "summary": abstractive_summary,
}

ALL_ACTIONS = ["language", "sentiment", "keyPhrases", "entities", "pii", "summary"]


def main(req: func.HttpRequest) -> func.HttpResponse:
    endpoint, key = _credentials()
    if not endpoint or not key:
        return func.HttpResponse(
            json.dumps({"error": "missing LANGUAGE_ENDPOINT / LANGUAGE_KEY environment variables"}),
            status_code=500,
            mimetype="application/json",
        )

    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "invalid JSON body"}), status_code=400, mimetype="application/json"
        )

    text = (req_body.get("text") or "").strip()
    if not text:
        return func.HttpResponse(
            json.dumps({"error": "text is required"}), status_code=400, mimetype="application/json"
        )
    truncated = len(text) > MAX_CHARS
    text = text[:MAX_CHARS]

    fallback_language = req_body.get("language", "en")
    use_detected = req_body.get("useDetectedLanguage", True)
    requested = req_body.get("actions") or ALL_ACTIONS
    requested = [a for a in requested if a in ALL_ACTIONS]

    results = {}
    errors = {}
    working_language = fallback_language

    if "language" in requested:
        try:
            results["language"] = language_detection(endpoint, key, text, fallback_language)
            iso = results["language"].get("iso6391Name")
            if use_detected and iso and iso != "(Unknown)":
                working_language = iso
        except urllib.error.HTTPError as exc:
            errors["language"] = _http_error(exc)
        except Exception as exc:  # noqa: BLE001
            logging.exception("language detection failed")
            errors["language"] = str(exc)

    for action in requested:
        if action == "language":
            continue
        fn = ACTIONS[action]
        try:
            results[action] = fn(endpoint, key, text, working_language)
        except urllib.error.HTTPError as exc:
            errors[action] = _http_error(exc)
        except Exception as exc:  # noqa: BLE001
            logging.exception("action %s failed", action)
            errors[action] = str(exc)

    response_body = {"results": results, "detectedLanguage": working_language, "truncated": truncated}
    if errors:
        response_body["errors"] = errors

    return func.HttpResponse(json.dumps(response_body), status_code=200, mimetype="application/json")


def _http_error(exc: urllib.error.HTTPError) -> str:
    try:
        detail = exc.read().decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        detail = ""
    return f"HTTP {exc.code}: {detail[:500]}"
