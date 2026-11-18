"""Buffer GraphQL API (https://api.buffer.com, Bearer personal API key).
YouTube-specific post fields aren't in the public guides, so we discover them by introspecting the schema."""
import json

import requests

from . import config

API = "https://api.buffer.com"
_meta_fields: dict | None = None


def gql(query: str, variables: dict | None = None) -> dict:
    if not config.BUFFER_API_KEY:
        raise RuntimeError("BUFFER_API_KEY not set")
    r = requests.post(API, timeout=60, json={"query": query, "variables": variables or {}},
                      headers={"Authorization": f"Bearer {config.BUFFER_API_KEY}", "Content-Type": "application/json"})
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"Buffer: {json.dumps(data['errors'])[:500]}")
    return data["data"]


def organizations() -> list[dict]:
    return gql("query { account { organizations { id name } } }")["account"]["organizations"]


def channels(org_id: str) -> list[dict]:
    return gql("query($o: OrganizationId!) { channels(input: {organizationId: $o}) "
               "{ id name displayName service isQueuePaused } }", {"o": org_id})["channels"]


def _type_fields(name: str) -> list[dict]:
    t = gql("query($n: String!) { __type(name: $n) { inputFields { name type { name kind ofType { name kind "
            "ofType { name } } } } } }", {"n": name})["__type"]
    return (t or {}).get("inputFields") or []


_input_type: str | None = None


def _create_post_input_type() -> str:
    global _input_type
    if _input_type is None:
        _input_type = "CreatePostInput"
        try:
            fields = gql("{ __schema { mutationType { fields { name args { name type { name kind "
                         "ofType { name } } } } } } }")["__schema"]["mutationType"]["fields"]
            for f in fields:
                if f["name"] == "createPost":
                    t = f["args"][0]["type"]
                    _input_type = t["name"] or t["ofType"]["name"]
        except Exception:
            pass
    return _input_type


def youtube_meta_fields() -> dict:
    """{'field': 'youtube', 'type': 'YoutubePostMetadataInput', 'fields': {...name: typeName}}"""
    global _meta_fields
    if _meta_fields is None:
        _meta_fields = {}
        for f in _type_fields("PostInputMetaData"):
            if "youtube" in f["name"].lower():
                tname = f["type"]["name"] or (f["type"].get("ofType") or {}).get("name")
                sub = {g["name"]: (g["type"]["name"] or (g["type"].get("ofType") or {}).get("name"))
                       for g in _type_fields(tname)}
                _meta_fields = {"field": f["name"], "type": tname, "fields": sub}
    return _meta_fields


def create_video_post(channel_id: str, title: str, description: str, video_url: str, due_at_iso: str) -> dict:
    meta = {}
    yf = youtube_meta_fields()
    if yf:
        f, want = yf["fields"], {}
        if "title" in f:
            want["title"] = title[:100]
        for k in ("privacy", "privacyStatus"):
            if k in f:
                want[k] = "public"
        for k in ("madeForKids", "isMadeForKids"):
            if k in f:
                want[k] = False
        if "notifySubscribers" in f:
            want["notifySubscribers"] = True
        if "categoryId" in f:
            want["categoryId"] = "20"  # Gaming
        meta = {yf["field"]: want}
    inp = {"text": description, "channelId": channel_id, "schedulingType": "automatic",
           "mode": "customScheduled", "dueAt": due_at_iso,
           "assets": [{"video": {"url": video_url, "metadata": {"thumbnailOffset": 1000}}}]}
    if meta:
        inp["metadata"] = meta
    q = f"""mutation($input: {_create_post_input_type()}!) {{ createPost(input: $input) {{
             ... on PostActionSuccess {{ post {{ id }} }}
             ... on MutationError {{ message }} }} }}"""
    res = gql(q, {"input": inp})["createPost"]
    if res.get("message"):
        raise RuntimeError(f"Buffer: {res['message']}")
    return res["post"]
