"""Abacus.AI RouteLLM adapter: lets every AI feature run on an OpenAI-style endpoint.

The rest of the code talks to `client.messages.create(...)` in the Anthropic shape. This client accepts that shape,
calls Abacus (OpenAI chat completions with tools and images) and returns an Anthropic-shaped response, so the Review
Agent, image reading, clarity drafts and imports work unchanged. Used only when no Anthropic key is configured.
"""
import json
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

import anthropic
import httpx

URL = "https://routellm.abacus.ai/v1/chat/completions"
MIN_TOKENS = 1500  # the routed models may think before answering; a tiny limit can return an empty reply


def _to_openai(system, messages):
    out = []
    if system:
        text = system if isinstance(system, str) else "\n".join(b["text"] for b in system if b.get("type") == "text")
        out.append({"role": "system", "content": text})
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        blocks = [b if isinstance(b, dict) else vars(b) for b in content]
        if m["role"] == "assistant":
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        parts = []
        for b in blocks:
            if b.get("type") == "tool_result":
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(b.get("content", ""))})
            elif b.get("type") == "image":
                src = b["source"]
                parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
            elif b.get("type") == "text":
                parts.append({"type": "text", "text": b["text"]})
        if parts:
            out.append({"role": "user", "content": parts})
    return out


def _tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in tools or []]


class Messages:
    def __init__(self, key, model):
        self.key, self.model = key, model

    def create(self, **kw):
        body = {"model": self.model, "messages": _to_openai(kw.get("system"), kw["messages"]),
                "max_tokens": max(int(kw.get("max_tokens", MIN_TOKENS)), MIN_TOKENS)}
        if kw.get("tools"):
            body["tools"], body["tool_choice"] = _tools(kw["tools"]), "auto"
        req = urllib.request.Request(URL, json.dumps(body).encode(), {"Authorization": f"Bearer {self.key}",
                                                                      "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:500]
            try:
                err = json.loads(raw)
            except ValueError:
                err = {}
            msg = str(err.get("error", {}).get("message") if isinstance(err.get("error"), dict) else err.get("error") or raw)
            resp = httpx.Response(e.code, request=httpx.Request("POST", URL))
            raise anthropic.APIStatusError(msg, response=resp, body={"error": {"message": msg}})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise anthropic.APIConnectionError(request=httpx.Request("POST", URL)) from e
        choice = data["choices"][0]
        msg = choice["message"]
        content = []
        if msg.get("content"):
            content.append(NS(type="text", text=msg["content"]))
        for c in msg.get("tool_calls") or []:
            content.append(NS(type="tool_use", id=c["id"], name=c["function"]["name"],
                              input=json.loads(c["function"]["arguments"] or "{}")))
        reason = choice.get("finish_reason")
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "max_tokens" if reason == "length" else "end_turn"
        u = data.get("usage") or {}
        usage = NS(input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0),
                   cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return NS(content=content, stop_reason=stop, usage=usage)


class Client:
    def __init__(self, key, model="route-llm"):
        self.messages = Messages(key, model)
