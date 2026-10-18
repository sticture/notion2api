"""OpenAI Responses protocol adapter. Local tools still execute in the client."""
from __future__ import annotations

import json
import time
import uuid

from .protocol import ProtocolError, prepare_request, parse_answer, estimate_tokens

PICKER_MODEL = "notion-claude-opus-5-5"
LOCAL_TOOLS = {"exec_command", "write_stdin", "shell_command", "shell", "apply_patch", "request_user_input",
               "get_goal", "create_goal", "update_goal"}


def convert_request(body):
    if body.get("previous_response_id"):
        raise ProtocolError("Send the complete input history; previous_response_id is not stored")
    if body.get("background"):
        raise ProtocolError("Background Responses are not supported")
    tools, mapping = [], {}

    def add(tool, namespace=None):
        kind = tool.get("type")
        if kind == "namespace":
            if tool.get("name") not in {"functions"}:
                return
            for child in tool.get("tools", []):
                add(child, tool["name"])
            return
        if tool.get("name") not in LOCAL_TOOLS:
            # Model-scoped local-coding capability. Do not change the client's other models/plugins.
            return
        if kind not in {"function", "custom"}:
            raise ProtocolError("Notion supports local function/custom tools, not hosted tool type " + str(kind))
        name = tool["name"]
        full = f"{namespace}.{name}" if namespace else name
        mapping[full] = {"kind": kind, "name": name, "namespace": namespace}
        schema = tool.get("parameters", {"type": "object"}) if kind == "function" else {
            "type": "object", "properties": {"input": {"type": "string"}},
            "required": ["input"], "additionalProperties": False,
        }
        description = tool.get("description", "")
        if kind == "custom":
            description += "\nReturn raw tool text in input.input. Tool format: " + json.dumps(tool.get("format", {}))
        tools.append({"name": full, "description": description, "input_schema": schema})

    for tool in body.get("tools", []):
        add(tool)
    messages = []
    input_items = body.get("input", [])
    if isinstance(input_items, str):
        input_items = [{"type": "message", "role": "user", "content": input_items}]
    for item in input_items:
        kind = item.get("type", "message")
        if kind == "reasoning":
            # Opaque reasoning/signature items are not usable by a different upstream.
            continue
        if kind == "message":
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            raw = item.get("content", "")
            if isinstance(raw, str):
                content = raw
            else:
                content = []
                for block in raw:
                    if block.get("type") not in {"input_text", "output_text", "text"}:
                        raise ProtocolError("Notion Responses currently supports text inputs only")
                    content.append({"type": "text", "text": block["text"]})
            messages.append({"role": role, "content": content})
        elif kind in {"function_call", "custom_tool_call"}:
            full = f"{item['namespace']}.{item['name']}" if item.get("namespace") else item["name"]
            args = json.loads(item["arguments"]) if kind == "function_call" else {"input": item["input"]}
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": item["call_id"],
                                                                  "name": full, "input": args}]})
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": item["call_id"],
                                                             "content": output}]})
        else:
            raise ProtocolError("Unsupported Responses input item: " + str(kind))
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, str):
        choice = {"type": "any" if choice == "required" else choice}
    elif choice.get("type") == "function":
        name = f"{choice['namespace']}.{choice['name']}" if choice.get("namespace") else choice["name"]
        choice = {"type": "tool", "name": name}
    effort = (body.get("reasoning") or {}).get("effort") or "medium"
    # Existing global client defaults can request ultra; Notion's highest verified level is max.
    effort = {"minimal": "low", "none": "low", "ultra": "max"}.get(effort, effort)
    converted = {"model": "claude-opus-5-5", "max_tokens": body.get("max_output_tokens") or 16000,
                 "system": body.get("instructions") or "", "messages": messages, "tools": tools,
                 "tool_choice": choice, "output_config": {"effort": effort}}
    return (*prepare_request(converted), mapping)


def response_object(response_id, model, output=None, usage=None, status="in_progress"):
    return {"id": response_id, "object": "response", "created_at": int(time.time()), "status": status,
            "model": model, "output": output or [], "error": None, "incomplete_details": None,
            "parallel_tool_calls": True, "usage": usage}


def convert_answer(text, tools, choice, mapping):
    content, _ = parse_answer(text, tools, choice)
    output = []
    for block in content:
        if block["type"] == "text":
            output.append({"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": block["text"], "annotations": []}]})
        else:
            tool = mapping[block["name"]]
            item = {"id": "fc_" + uuid.uuid4().hex, "type": "function_call" if tool["kind"] == "function" else "custom_tool_call",
                    "call_id": block["id"], "name": tool["name"], "status": "completed"}
            if tool["namespace"]:
                item["namespace"] = tool["namespace"]
            if tool["kind"] == "function":
                item["arguments"] = json.dumps(block["input"], ensure_ascii=False)
            else:
                item["input"] = block["input"]["input"]
            output.append(item)
    return output


def output_events(output):
    for index, item in enumerate(output):
        base = {"output_index": index, "item_id": item["id"]}
        initial = {**item, "status": "in_progress"}
        if item["type"] == "message":
            initial["content"] = []
        elif item["type"] == "function_call":
            initial["arguments"] = ""
        else:
            initial["input"] = ""
        yield {"type": "response.output_item.added", "output_index": index, "item": initial}
        if item["type"] == "message":
            part = item["content"][0]
            yield {"type": "response.content_part.added", **base, "content_index": 0, "part": {**part, "text": ""}}
            yield {"type": "response.output_text.delta", **base, "content_index": 0, "delta": part["text"]}
            yield {"type": "response.output_text.done", **base, "content_index": 0, "text": part["text"]}
            yield {"type": "response.content_part.done", **base, "content_index": 0, "part": part}
        elif item["type"] == "function_call":
            yield {"type": "response.function_call_arguments.delta", **base, "delta": item["arguments"]}
            yield {"type": "response.function_call_arguments.done", **base, "arguments": item["arguments"]}
        else:
            yield {"type": "response.custom_tool_call_input.delta", **base, "delta": item["input"]}
            yield {"type": "response.custom_tool_call_input.done", **base, "input": item["input"]}
        yield {"type": "response.output_item.done", "output_index": index, "item": item}


def response_usage(prompt, output):
    inp, out = estimate_tokens(prompt), estimate_tokens(output)
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
