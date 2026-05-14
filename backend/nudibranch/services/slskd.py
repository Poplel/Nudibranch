import time
from typing import Any

import httpx


def slskd_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key} if api_key else {}


def search_slskd(slskd_url: str, api_key: str, query: str, limit: int = 4) -> list[dict[str, Any]]:
    return search_slskd_detailed(slskd_url, api_key, query, limit)["candidates"]


def search_slskd_detailed(slskd_url: str, api_key: str, query: str, limit: int = 4, poll_count: int = 3, poll_interval: float = 0.5) -> dict[str, Any]:
    if not slskd_url:
        raise ValueError("slskd URL is required")
    if not api_key:
        raise ValueError("slskd API key is required")

    with httpx.Client(base_url=slskd_url.rstrip("/"), headers=slskd_headers(api_key), timeout=10) as client:
        created = client.post("/api/v0/searches", json={"searchText": query})
        created.raise_for_status()
        search_id = search_identifier(created.json())
        if not search_id:
            raise ValueError("slskd did not return a search id")

        payload: dict[str, Any] = {}
        diagnostics: dict[str, Any] = {"search_id": search_id, "polls": 0, "responses": 0, "files": 0, "candidates": 0}
        for _ in range(poll_count):
            diagnostics["polls"] += 1
            payload = search_payload(client, search_id)
            diagnostics.update(search_diagnostics(payload))
            candidates = extract_candidates(payload, query)
            if candidates:
                ranked = rank_candidates(candidates)[:limit]
                diagnostics["candidates"] = len(ranked)
                return {"candidates": ranked, "diagnostics": diagnostics}
            time.sleep(poll_interval)
        ranked = rank_candidates(extract_candidates(payload, query))[:limit]
        diagnostics["candidates"] = len(ranked)
        return {"candidates": ranked, "diagnostics": diagnostics}


def search_payload(client: httpx.Client, search_id: str) -> Any:
    state_response = client.get(f"/api/v0/searches/{search_id}", params={"includeResponses": "true"})
    state_response.raise_for_status()
    state_payload = state_response.json()
    responses = response_list(state_payload)
    if responses:
        return state_payload

    responses_response = client.get(f"/api/v0/searches/{search_id}/responses")
    if responses_response.status_code in {404, 405}:
        return state_payload
    responses_response.raise_for_status()
    responses_payload = responses_response.json()
    if isinstance(state_payload, dict):
        state_payload = {**state_payload, "responses": response_list(responses_payload)}
        return state_payload
    return responses_payload


def queue_slskd_download(slskd_url: str, api_key: str, candidate: dict[str, Any]) -> dict[str, Any]:
    if not slskd_url:
        raise ValueError("slskd URL is required")
    if not api_key:
        raise ValueError("slskd API key is required")
    username = candidate.get("username")
    files = candidate.get("files") or []
    if not username or not files:
        raise ValueError("Download candidate is missing a username or files")

    with httpx.Client(base_url=slskd_url.rstrip("/"), headers=slskd_headers(api_key), timeout=20) as client:
        endpoint = f"/api/v0/transfers/downloads/{username}"
        response = client.post(endpoint, json={"files": files})
        if response.status_code in {400, 415, 422}:
            response = client.post(endpoint, json=files)
        response.raise_for_status()
        return {"username": username, "files": len(files), "response": response.json() if response.content else {}}


def search_identifier(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("id", "searchId", "search_id"):
            if payload.get(key):
                return str(payload[key])
    return None


def extract_candidates(payload: Any, query: str) -> list[dict[str, Any]]:
    responses = response_list(payload)

    candidates: list[dict[str, Any]] = []
    for response in responses:
        if not isinstance(response, dict):
            continue
        username = response.get("username") or response.get("user") or response.get("UserName")
        files = response.get("files") or response.get("Files") or []
        for file_info in files:
            normalized = normalize_file(file_info)
            if not normalized:
                continue
            candidates.append(
                {
                    "username": username,
                    "query": query,
                    "filename": normalized["filename"],
                    "size": normalized.get("size"),
                    "quality": quality_label(normalized),
                    "files": [normalized],
                }
            )
    return candidates


def response_list(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        for key in ("responses", "Responses", "results", "Results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if isinstance(payload.get("data"), list):
            return payload["data"]
    if isinstance(payload, list):
        return payload
    return []


def search_diagnostics(payload: Any) -> dict[str, int]:
    responses = response_list(payload)
    file_count = 0
    for response in responses:
        if not isinstance(response, dict):
            continue
        files = response.get("files") or response.get("Files") or []
        if isinstance(files, list):
            file_count += len(files)
    return {"responses": len(responses), "files": file_count}


def normalize_file(file_info: Any) -> dict[str, Any] | None:
    if isinstance(file_info, str):
        return {"filename": file_info}
    if not isinstance(file_info, dict):
        return None
    filename = file_info.get("filename") or file_info.get("fileName") or file_info.get("Filename") or file_info.get("name")
    if not filename:
        return None
    return {
        **file_info,
        "filename": filename,
        "size": file_info.get("size") or file_info.get("Size"),
    }


def quality_label(file_info: dict[str, Any]) -> str:
    filename = str(file_info.get("filename") or "").lower()
    if filename.endswith(".flac"):
        return "lossless"
    if filename.endswith((".wav", ".aiff", ".alac")):
        return "lossless"
    if filename.endswith((".mp3", ".m4a", ".ogg", ".opus")):
        return "lossy"
    return "unknown"


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def score(candidate: dict[str, Any]) -> tuple[int, int]:
        quality = candidate.get("quality")
        quality_score = 2 if quality == "lossless" else 1 if quality == "lossy" else 0
        size = int(candidate.get("size") or 0)
        return quality_score, size

    return sorted(candidates, key=score, reverse=True)
