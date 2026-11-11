"""Shared helpers for the applications tests."""

API = "/api/v1"


async def put_status(client, job_id: int, key: str, **body):
    return await client.put(f"{API}/jobs/{job_id}/status", json={"status": key, **body})


async def get_application(client, job_id: int) -> dict:
    r = await client.get(f"{API}/jobs/{job_id}/application")
    assert r.status_code == 200, r.text
    return r.json()


async def get_timeline(client, job_id: int) -> list[dict]:
    r = await client.get(f"{API}/jobs/{job_id}/timeline")
    assert r.status_code == 200, r.text
    return r.json()
