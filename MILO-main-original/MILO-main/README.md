# Kimi Vehicle Swarm Streamlit Prototype

This repository contains a single-file Streamlit prototype that maps all vehicle models of a manufacturer in a target market and period using the Moonshot/Kimi K2.6 OpenAI-compatible API.

Default prototype inputs:

- Manufacturer: `Hyundai`
- Market: `Israel`
- Period: `2010 to June 2026`

The app intentionally contains **zero hardcoded vehicle model data**. Vehicle models and specs are discovered at runtime with Kimi's `$web_search` builtin tool.

## Architecture

The pipeline is fixed to four sequential phases:

1. **Phase 1A — Discovery**: one serial web-search agent discovers the model list. If this phase fails or returns no valid model list, the pipeline aborts.
2. **Phase 1B — Enrichment**: five web-search agents run in parallel with `ThreadPoolExecutor`, each receiving only the discovered model list and researching a specific slice of data.
3. **Phase 2 — Consolidation**: one non-search Kimi agent merges all outputs into one JSON object with `response_format={"type":"json_object"}`.
4. **Phase 3 — Summary**: one non-search Kimi agent writes a Hebrew summary with counts, body-type breakdown, notable models, prices if found, and period trends.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set your Moonshot API key:

```bash
export MOONSHOT_API_KEY="your-key-here"
```

Alternatively, paste the key into the sidebar password field at runtime.

## Run locally

```bash
streamlit run app.py
```

Open the displayed local URL in your browser, review the default inputs, and click **Run**.

## Deployment notes

- Configure `MOONSHOT_API_KEY` as a secret/environment variable in your Streamlit hosting provider.
- The app uses Moonshot's OpenAI-compatible endpoint: `https://api.moonshot.ai/v1`.
- Model: `kimi-k2.6`.
- Search agents declare the builtin `$web_search` tool and implement Moonshot's tool-call echo loop.
- Consolidation and summary phases intentionally run without web search.

## Cost estimate

The UI tracks prompt and completion tokens reported by the API and estimates cost with:

- Input: `$0.95 / 1M tokens`
- Output: `$4.00 / 1M tokens`
