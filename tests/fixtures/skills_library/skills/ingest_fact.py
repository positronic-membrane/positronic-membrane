def run(sdk, args):
    from src.epistemic import run_epistemic_pipeline
    fact_text = args.get("fact_text", "").strip()
    if not fact_text:
        return {"error": "fact_text is required"}
    source = args.get("source", "manual")
    source_url = args.get("source_url", None)
    raw_metadata = args.get("metadata", {})
    result = run_epistemic_pipeline(fact_text, source=source, source_url=source_url, raw_metadata=raw_metadata)
    return result
