import time
from typing import Any
from urllib.parse import quote

import httpx


def slskd_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key} if api_key else {}


def search_slskd(slskd_url: str, api_key: str, query: str, limit: int = 4) -> list[dict[str, Any]]:
    return search_slskd_detailed(slskd_url, api_key, query, limit)["candidates"]


def search_slskd_detailed(
    slskd_url: str,
    api_key: str,
    query: str,
    limit: int = 4,
    poll_count: int | None = None,
    poll_interval: float = 1.0,
    timeout_seconds: int = 12,
    timeout_buffer_seconds: int = 3,
) -> dict[str, Any]:
    if not slskd_url:
        raise ValueError("slskd URL is required")
    if not api_key:
        raise ValueError("slskd API key is required")

    with httpx.Client(base_url=slskd_url.rstrip("/"), headers=slskd_headers(api_key), timeout=10) as client:
        created = create_search(client, query, timeout_seconds)
        created.raise_for_status()
        search_id = search_identifier(created.json())
        if not search_id:
            raise ValueError("slskd did not return a search id")

        payload: dict[str, Any] = {}
        responses: list[Any] = []
        diagnostics: dict[str, Any] = {
            "search_id": search_id,
            "polls": 0,
            "responses": 0,
            "files": 0,
            "candidates": 0,
            "timeout_seconds": timeout_seconds,
            "timeout_buffer_seconds": timeout_buffer_seconds,
            "response_growth": 0,
            "state": "created",
        }
        max_polls = poll_count or max(1, int((timeout_seconds + timeout_buffer_seconds) / poll_interval))
        settled_polls = 0
        last_response_count = 0
        for _ in range(max_polls):
            diagnostics["polls"] += 1
            payload = search_payload(client, search_id)
            current_responses = response_list(payload)
            if len(current_responses) > len(responses):
                responses = current_responses
            if isinstance(payload, dict):
                diagnostics["state"] = str(payload.get("state") or payload.get("State") or payload.get("status") or payload.get("Status") or "")
            diagnostics.update(search_diagnostics(payload))
            diagnostics["response_growth"] = len(responses) - last_response_count
            candidates = extract_candidates(responses, query)
            folder_candidates = extract_folder_candidates(responses, query)
            if len(responses) == last_response_count:
                settled_polls += 1
            else:
                settled_polls = 0
            last_response_count = len(responses)
            if candidates and limit == 1:
                ranked = rank_candidates(candidates)[:limit]
                diagnostics["candidates"] = len(ranked)
                return {"candidates": ranked, "folder_candidates": folder_candidates, "diagnostics": diagnostics}
            if candidates and (diagnostics["polls"] >= 3 or len(responses) >= 30 or settled_polls >= 1):
                ranked = rank_candidates(candidates)[:limit]
                diagnostics["candidates"] = len(ranked)
                return {"candidates": ranked, "folder_candidates": folder_candidates, "diagnostics": diagnostics}
            if folder_candidates and (diagnostics["polls"] >= 3 or len(responses) >= 30 or settled_polls >= 1):
                diagnostics["candidates"] = 0
                return {"candidates": [], "folder_candidates": folder_candidates, "diagnostics": diagnostics}
            time.sleep(poll_interval)
        ranked = rank_candidates(extract_candidates(responses or payload, query))[:limit]
        folder_candidates = extract_folder_candidates(responses or payload, query)
        diagnostics["candidates"] = len(ranked)
        return {"candidates": ranked, "folder_candidates": folder_candidates, "diagnostics": diagnostics}


def create_search(client: httpx.Client, query: str, timeout_seconds: int) -> httpx.Response:
    payload = {
        "searchText": query,
        "timeout": timeout_seconds * 1000,
        "filterResponses": True,
        "minimumResponseFileCount": 1,
        "minimumPeerUploadSpeed": 0,
    }
    response = client.post("/api/v0/searches", json=payload)
    if response.status_code in {400, 415, 422}:
        return client.post("/api/v0/searches", json={"searchText": query})
    return response


def search_payload(client: httpx.Client, search_id: str) -> Any:
    responses_response = client.get(f"/api/v0/searches/{search_id}/responses")
    if responses_response.status_code not in {404, 405}:
        responses_response.raise_for_status()
        responses_payload = responses_response.json()
        responses = response_list(responses_payload)
        if responses:
            return {"responses": responses}

    state_response = client.get(f"/api/v0/searches/{search_id}", params={"includeResponses": "true"})
    state_response.raise_for_status()
    state_payload = state_response.json()
    responses = response_list(state_payload)
    if responses:
        return state_payload
    if isinstance(state_payload, dict):
        return {**state_payload, "responses": responses}
    return state_payload


def queue_slskd_download(slskd_url: str, api_key: str, candidate: dict[str, Any]) -> dict[str, Any]:
    if not slskd_url:
        raise ValueError("slskd URL is required")
    if not api_key:
        raise ValueError("slskd API key is required")
    username = candidate.get("username")
    files = candidate.get("files") or []
    if not username or not files:
        raise ValueError("Download candidate is missing a username or files")

    errors: list[str] = []
    with httpx.Client(base_url=slskd_url.rstrip("/"), headers=slskd_headers(api_key), timeout=20) as client:
        endpoint = f"/api/v0/transfers/downloads/{username}"
        for payload in (files, {"files": files}):
            response = client.post(endpoint, json=payload)
            if response.is_success:
                return {"username": username, "files": len(files), "response": response.json() if response.content else {}}
            errors.append(f"{response.status_code}: {response.text[-500:]}")
    raise RuntimeError("; ".join(errors))


def cancel_slskd_download(slskd_url: str, api_key: str, username: str, transfer_id: str, remove: bool = True) -> bool:
    if not slskd_url or not api_key or not username or not transfer_id:
        return False
    with httpx.Client(base_url=slskd_url.rstrip("/"), headers=slskd_headers(api_key), timeout=10) as client:
        response = client.delete(f"/api/v0/transfers/downloads/{quote(username, safe='')}/{quote(transfer_id, safe='')}", params={"remove": remove})
        if response.status_code in {404, 410}:
            return False
        response.raise_for_status()
        return True


def download_transfers(slskd_url: str, api_key: str) -> list[dict[str, Any]]:
    if not slskd_url or not api_key:
        return []
    with httpx.Client(base_url=slskd_url.rstrip("/"), headers=slskd_headers(api_key), timeout=10) as client:
        response = client.get("/api/v0/transfers/downloads")
        if response.status_code in {404, 405}:
            return []
        response.raise_for_status()
        return flatten_download_transfers(response.json())


def flatten_download_transfers(payload: Any) -> list[dict[str, Any]]:
    transfers: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_transfer(value: dict[str, Any], username: str | None) -> None:
        normalized = normalize_file(value)
        if not normalized:
            return
        transfer = {
            **value,
            "id": transfer_id(value),
            "username": username or response_username(value),
            "filename": normalized["filename"],
            "size": normalized.get("size"),
            "status": transfer_status(value),
            "percent": transfer_percent(value),
            "bytes_transferred": transfer_bytes_transferred(value),
            "bytes_remaining": transfer_bytes_remaining(value),
            "average_speed": transfer_average_speed(value),
            "local_path": transfer_local_path(value),
            "error": transfer_error(value),
        }
        key = (str(transfer.get("username") or ""), str(transfer.get("filename") or ""), str(transfer.get("local_path") or ""))
        if key in seen:
            return
        seen.add(key)
        transfers.append(transfer)

    def walk(value: Any, username: str | None = None) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, username)
            return
        if not isinstance(value, dict):
            return

        current_username = response_username(value) or username
        if looks_like_transfer_file(value):
            add_transfer(value, current_username)

        for key in ("files", "Files", "downloads", "Downloads", "items", "Items"):
            child = value.get(key)
            if isinstance(child, dict):
                walk(list(child.values()), current_username)
            elif isinstance(child, list):
                walk(child, current_username)

        for key in ("directories", "Directories", "folders", "Folders"):
            child = value.get(key)
            if isinstance(child, list):
                walk(child, current_username)
            elif isinstance(child, dict):
                walk(list(child.values()), current_username)

        for key in ("users", "Users", "data", "Data", "transfers", "Transfers"):
            child = value.get(key)
            if isinstance(child, dict):
                for mapped_username, mapped_value in child.items():
                    walk(mapped_value, current_username or str(mapped_username))
            elif isinstance(child, list):
                walk(child, current_username)

        if not current_username:
            for mapped_username, mapped_value in value.items():
                if isinstance(mapped_value, (dict, list)):
                    walk(mapped_value, str(mapped_username))

    walk(payload)
    return transfers


def looks_like_transfer_file(value: dict[str, Any]) -> bool:
    if not normalize_file(value):
        return False
    transfer_keys = {
        "state",
        "State",
        "status",
        "Status",
        "percentComplete",
        "PercentComplete",
        "percentage",
        "Percentage",
        "bytesTransferred",
        "BytesTransferred",
        "localFilename",
        "LocalFilename",
        "localPath",
        "LocalPath",
    }
    return any(key in value for key in transfer_keys)


def transfer_status(value: dict[str, Any]) -> str | None:
    status = value.get("state") or value.get("State") or value.get("status") or value.get("Status")
    return str(status) if status is not None else None


def transfer_id(value: dict[str, Any]) -> str | None:
    raw = value.get("id") or value.get("Id") or value.get("ID")
    return str(raw) if raw is not None else None


def transfer_percent(value: dict[str, Any]) -> float | None:
    for key in ("percentComplete", "PercentComplete", "percentage", "Percentage", "percent", "Percent", "progress", "Progress"):
        raw = value.get(key)
        if raw is None:
            continue
        try:
            percent = float(raw)
        except (TypeError, ValueError):
            continue
        return percent * 100 if 0 < percent <= 1 else percent
    transferred = value.get("bytesTransferred") or value.get("BytesTransferred") or value.get("bytesDownloaded") or value.get("BytesDownloaded")
    size = value.get("size") or value.get("Size") or value.get("bytes") or value.get("Bytes")
    try:
        if transferred is not None and size:
            return (float(transferred) / float(size)) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def transfer_bytes_transferred(value: dict[str, Any]) -> int | None:
    for key in ("bytesTransferred", "BytesTransferred", "bytesDownloaded", "BytesDownloaded"):
        raw = value.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None


def transfer_bytes_remaining(value: dict[str, Any]) -> int | None:
    for key in ("bytesRemaining", "BytesRemaining"):
        raw = value.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None


def transfer_average_speed(value: dict[str, Any]) -> float | None:
    for key in ("averageSpeed", "AverageSpeed", "speed", "Speed"):
        raw = value.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


def transfer_local_path(value: dict[str, Any]) -> str | None:
    for key in ("localFilename", "LocalFilename", "localPath", "LocalPath", "path", "Path"):
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


def transfer_error(value: dict[str, Any]) -> str | None:
    for key in ("error", "Error", "message", "Message", "exception", "Exception"):
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


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
        username = response_username(response)
        if not username:
            continue
        files = [normalized for file_info in response_files(response) if (normalized := normalize_file(file_info))]
        audio_files = [file_info for file_info in files if is_audio_file(file_info["filename"])]
        for file_info in files:
            if not is_audio_file(file_info["filename"]):
                continue
            folder = remote_folder(file_info["filename"])
            folder_files = [audio_file for audio_file in audio_files if remote_folder(audio_file["filename"]) == folder]
            relevance = query_match_score(query, file_info["filename"])
            if relevance <= 0:
                continue
            candidates.append(
                {
                    "username": username,
                    "query": query,
                    "filename": file_info["filename"],
                    "folder": folder,
                    "size": file_info.get("size"),
                    "duration": file_info.get("duration"),
                    "bitrate": file_info.get("bitrate"),
                    "free_upload_slots": response.get("freeUploadSlots") or response.get("FreeUploadSlots"),
                    "upload_speed": response.get("uploadSpeed") or response.get("UploadSpeed"),
                    "queue_length": response.get("queueLength") or response.get("QueueLength"),
                    "quality": quality_label(file_info),
                    "relevance": relevance,
                    "files": [file_info],
                    "folder_files": folder_files,
                }
            )
    return candidates


def extract_folder_candidates(payload: Any, query: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for response in response_list(payload):
        if not isinstance(response, dict):
            continue
        username = response_username(response)
        if not username:
            continue
        files = [normalized for file_info in response_files(response) if (normalized := normalize_file(file_info))]
        audio_files = [file_info for file_info in files if is_audio_file(file_info["filename"])]
        for file_info in audio_files:
            folder = remote_folder(file_info["filename"])
            key = (username, folder, file_info["filename"])
            if key in seen:
                continue
            seen.add(key)
            folder_files = [audio_file for audio_file in audio_files if remote_folder(audio_file["filename"]) == folder]
            candidates.append(
                {
                    "username": username,
                    "query": query,
                    "filename": file_info["filename"],
                    "folder": folder,
                    "size": file_info.get("size"),
                    "duration": file_info.get("duration"),
                    "bitrate": file_info.get("bitrate"),
                    "free_upload_slots": response.get("freeUploadSlots") or response.get("FreeUploadSlots"),
                    "upload_speed": response.get("uploadSpeed") or response.get("UploadSpeed"),
                    "queue_length": response.get("queueLength") or response.get("QueueLength"),
                    "quality": quality_label(file_info),
                    "relevance": query_match_score(query, file_info["filename"]),
                    "files": [file_info],
                    "folder_files": folder_files,
                }
            )
    return candidates


def response_list(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        for key in ("responses", "Responses", "results", "Results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return response_mapping_to_list(value)
    if isinstance(payload, list):
        return payload
    return []


def response_mapping_to_list(mapping: dict[str, Any]) -> list[Any]:
    responses: list[Any] = []
    for username, response in mapping.items():
        if isinstance(response, dict):
            if not response_username(response):
                response = {**response, "username": username}
            responses.append(response)
        elif isinstance(response, list):
            responses.append({"username": username, "files": response})
    return responses


def search_diagnostics(payload: Any) -> dict[str, int]:
    responses = response_list(payload)
    file_count = 0
    for response in responses:
        if not isinstance(response, dict):
            continue
        files = response_files(response)
        if isinstance(files, list):
            file_count += len(files)
    return {"responses": len(responses), "files": file_count}


def response_username(response: dict[str, Any]) -> str | None:
    user = response.get("username") or response.get("userName") or response.get("UserName")
    if isinstance(user, str):
        return user
    user = response.get("user") or response.get("User")
    if isinstance(user, str):
        return user
    if isinstance(user, dict):
        return user.get("username") or user.get("userName") or user.get("name") or user.get("Name")
    return None


def response_files(response: dict[str, Any]) -> list[Any]:
    for key in ("files", "Files", "results", "Results"):
        value = response.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return list(value.values())
    return []


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
        "duration": file_info.get("duration") or file_info.get("Duration") or file_info.get("length") or file_info.get("Length"),
        "bitrate": file_info.get("bitRate") or file_info.get("BitRate") or file_info.get("bitrate") or file_info.get("Bitrate"),
    }


def is_audio_file(filename: str) -> bool:
    return filename.lower().endswith((".flac", ".wav", ".aiff", ".alac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wma"))


def quality_label(file_info: dict[str, Any]) -> str:
    filename = str(file_info.get("filename") or "").lower()
    if filename.endswith(".flac"):
        return "lossless"
    if filename.endswith((".wav", ".aiff", ".alac")):
        return "lossless"
    if filename.endswith((".mp3", ".m4a", ".ogg", ".opus")):
        return "lossy"
    return "unknown"


def remote_folder(filename: str) -> str:
    normalized = str(filename or "").replace("\\", "/")
    if "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0]


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def score(candidate: dict[str, Any]) -> tuple[int, int, int, float, int]:
        relevance = int(candidate.get("relevance") or 0)
        quality = candidate.get("quality")
        quality_score = 2 if quality == "lossless" else 1 if quality == "lossy" else 0
        free_slots = 1 if candidate.get("free_upload_slots") else 0
        upload_speed = float(candidate.get("upload_speed") or 0)
        size = int(candidate.get("size") or 0)
        return relevance, quality_score, free_slots, upload_speed, size

    return sorted(candidates, key=score, reverse=True)


def query_match_score(query: str, filename: str) -> int:
    haystack = " ".join(str(filename or "").replace("\\", "/").replace("_", " ").replace("-", " ").casefold().split())
    tokens = [token for token in str(query or "").casefold().replace("-", " ").split() if len(token) > 2]
    return sum(1 for token in tokens if token in haystack)
