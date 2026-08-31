"""Thin wrapper around the Instagram Platform Content Publishing API
(Instagram API with Instagram Login, graph.instagram.com).

Docs verified 2026-08-31:
https://developers.facebook.com/docs/instagram-platform/content-publishing/
"""
import time

import requests

from src.config import Config

API_VERSION = "v23.0"
GRAPH_BASE = f"https://graph.instagram.com/{API_VERSION}"


class InstagramAPIError(RuntimeError):
    """Raised with a human-readable reason whenever a call to the Instagram API fails."""


def _raise_for_response(resp: requests.Response, context: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        err = data.get("error", {})
        message = err.get("error_user_msg") or err.get("message") or resp.text
        code = err.get("code")
        subcode = err.get("error_subcode")
        raise InstagramAPIError(
            f"{context} failed (HTTP {resp.status_code}, code={code}, subcode={subcode}): {message}"
        )
    return data


class InstagramClient:
    def __init__(self, config: Config):
        self.config = config

    def _post(self, path: str, params: dict) -> dict:
        params = {**params, "access_token": self.config.access_token}
        resp = requests.post(f"{GRAPH_BASE}/{path}", data=params, timeout=60)
        return _raise_for_response(resp, f"POST {path}")

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "access_token": self.config.access_token}
        resp = requests.get(f"{GRAPH_BASE}/{path}", params=params, timeout=30)
        return _raise_for_response(resp, f"GET {path}")

    # ---- rate limit -------------------------------------------------

    def get_publishing_limit(self) -> dict:
        """Returns {'quota_usage': int, 'config': {'quota_total': int, 'quota_duration': int}}."""
        data = self._get(
            f"{self.config.ig_user_id}/content_publishing_limit",
            {"fields": "config,quota_usage"},
        )
        items = data.get("data", [])
        return items[0] if items else {"quota_usage": 0, "config": {"quota_total": 100, "quota_duration": 86400}}

    # ---- container creation ------------------------------------------

    def create_image_container(self, image_url: str, caption: str | None = None, is_carousel_item: bool = False) -> str:
        params = {"image_url": image_url}
        if caption and not is_carousel_item:
            params["caption"] = caption
        if is_carousel_item:
            params["is_carousel_item"] = "true"
        data = self._post(f"{self.config.ig_user_id}/media", params)
        return data["id"]

    def create_video_container(
        self,
        video_url: str,
        media_type: str = "REELS",
        caption: str | None = None,
        is_carousel_item: bool = False,
    ) -> str:
        params = {"video_url": video_url, "media_type": media_type}
        if caption and not is_carousel_item:
            params["caption"] = caption
        if is_carousel_item:
            params["is_carousel_item"] = "true"
            # carousel video children must not carry media_type=REELS
            params.pop("media_type", None)
        data = self._post(f"{self.config.ig_user_id}/media", params)
        return data["id"]

    def create_carousel_container(self, children_ids: list[str], caption: str | None = None) -> str:
        params = {
            "media_type": "CAROUSEL",
            "children": ",".join(children_ids),
        }
        if caption:
            params["caption"] = caption
        data = self._post(f"{self.config.ig_user_id}/media", params)
        return data["id"]

    # ---- status polling (required for video/reels/carousel-with-video) ----

    def wait_until_ready(self, container_id: str, timeout_s: int = 300, interval_s: int = 5) -> None:
        elapsed = 0
        while elapsed < timeout_s:
            data = self._get(container_id, {"fields": "status_code,status"})
            status = data.get("status_code")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise InstagramAPIError(f"Media container {container_id} failed processing: {data.get('status')}")
            time.sleep(interval_s)
            elapsed += interval_s
        raise InstagramAPIError(f"Media container {container_id} did not finish processing within {timeout_s}s")

    # ---- publish -------------------------------------------------------

    def publish(self, creation_id: str) -> str:
        data = self._post(f"{self.config.ig_user_id}/media_publish", {"creation_id": creation_id})
        return data["id"]
