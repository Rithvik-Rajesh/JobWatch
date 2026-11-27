from app.sites import fetch_all


def test_live_progress_counts_rules_ai_and_storage():
    fetch_all._state = fetch_all._fresh_state()
    fetch_all._record_progress("Acme", {"stage": "listed", "count": 3})
    fetch_all._record_progress(
        "Acme",
        {"stage": "classified", "title": "Graduate Engineer", "provider": "rules:title-acceptance", "verdict": True},
    )
    fetch_all._record_progress(
        "Acme",
        {"stage": "classified", "title": "Engineer", "provider": "ollama:gemma3:4b", "verdict": None},
    )
    fetch_all._record_progress("Acme", {"stage": "stored", "title": "Engineer"})

    assert fetch_all._state["jobs_found"] == 3
    assert fetch_all._state["jobs_classified"] == 2
    assert fetch_all._state["rules_checked"] == 1
    assert fetch_all._state["ai_checked"] == 1
    assert fetch_all._state["stored_jobs"] == 1
    assert [item["stage"] for item in fetch_all._state["activity"]] == ["classified", "classified", "stored"]
