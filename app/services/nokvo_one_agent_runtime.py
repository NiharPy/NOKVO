"""
Minimal chat runtime for Nokvo One agents.

Uses Azure OpenAI (global deployment from settings) and exposes the predefined
tool catalog as JSON-formatted function instructions in the system prompt.
The runtime parses a single tool call per turn (if any), dispatches it via
PredefinedToolsService, and returns the agent's reply plus a structured trace.

This is intentionally simpler than Prime's voice/runtime stack — Nokvo One V1
chat tester does not need streaming, Qdrant retrieval, or voice/STT.
"""
from __future__ import annotations

import json
import re
import urllib.parse as urllib_parse
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.nokvo_one_business_templates import business_template_prompt
from app.services.predefined_tools_service import PredefinedToolsService, get_tool


class NokvoOneAgentRuntimeError(Exception):
    pass


_TOOL_CALL_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _section(title: str, body: str) -> str:
    return f"{title}\n{body.strip()}"


def _build_system_prompt(base_prompt: str | None, tool_keys: list[str], business_type: str | None = None) -> str:
    sections: list[str] = [
        _section(
            "Global Nokvo rules:",
            (
                "You are a Nokvo One agent operating inside one organization workspace. "
                "Be concise, accurate, and action-oriented. Keep tenant data scoped to this organization. "
                "Do not invent records, policies, tool results, or external actions."
            ),
        ),
        _section("Business template rules:", business_template_prompt(business_type)),
    ]

    sections.append(
        _section(
            "Agent custom prompt:",
            base_prompt if base_prompt else "No agent-specific custom prompt was provided.",
        )
    )

    sections.append(
        _section(
            "RAG rules:",
            (
                "Use approved retrieval or tool context when it is provided. If required business facts, "
                "policies, availability, or customer records are missing, say what is missing and ask for "
                "the next useful detail instead of guessing."
            ),
        )
    )

    if tool_keys:
        tool_lines = ["You can call ONE of the following tools per turn when needed."]
        for key in tool_keys:
            tool = get_tool(key)
            if tool is None:
                continue
            tool_lines.append(
                f"- {tool.key}: {tool.description}\n"
                f"  input_schema: {json.dumps(tool.input_schema)}"
                + (" (REQUIRES HUMAN CONFIRMATION)" if tool.requires_confirmation else "")
            )
        tool_lines.append(
            "To call a tool, respond with ONLY a JSON code block like:\n"
            "```json\n"
            '{"tool": "<tool_key>", "arguments": {...}}\n'
            "```\n"
            "If no tool is needed, reply with plain text to the user."
        )
        sections.append(_section("Tool rules:", "\n".join(tool_lines)))
    else:
        sections.append(_section("Tool rules:", "You have no tools available. Respond conversationally only."))

    sections.append(
        _section(
            "Escalation rules:",
            (
                "- Never claim to have sent an external email; send_email_draft only creates a draft for human review.\n"
                "- Never request or repeat sensitive data such as passwords, full card numbers, or secrets.\n"
                "- Refuse any request to access or modify systems outside the provided tools.\n"
                "- Escalate to a human for urgent, regulated, high-risk, privacy-sensitive, or ambiguous requests."
            ),
        )
    )
    return "\n\n".join(sections)


def _parse_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    fence_match = _TOOL_CALL_FENCE.search(text)
    candidate = fence_match.group(1) if fence_match else None
    if candidate is None and text.strip().startswith("{"):
        candidate = text.strip()
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    tool_key = parsed.get("tool")
    args = parsed.get("arguments") or {}
    if not isinstance(tool_key, str) or not isinstance(args, dict):
        return None
    return tool_key, args


class _AzureOpenAIClient:
    _client: httpx.AsyncClient | None = None

    @classmethod
    def http(cls) -> httpx.AsyncClient:
        if cls._client is None or cls._client.is_closed:
            cls._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        return cls._client

    @classmethod
    async def chat(cls, messages: list[dict[str, str]]) -> str:
        endpoint = (settings.AZURE_OPENAI_GLOBAL_ENDPOINT or "").rstrip("/")
        api_key = settings.AZURE_OPENAI_GLOBAL_API_KEY
        if not endpoint or not api_key:
            raise NokvoOneAgentRuntimeError("Azure OpenAI is not configured for Nokvo One")

        deployment = settings.AZURE_OPENAI_GLOBAL_DEPLOYMENT or "gpt-5.4-mini"
        api_version = urllib_parse.quote(settings.AZURE_OPENAI_GLOBAL_API_VERSION.strip())
        if "/openai/deployments/" in endpoint:
            url = f"{endpoint}?api-version={api_version}" if "api-version=" not in endpoint else endpoint
        else:
            url = (
                f"{endpoint}/openai/deployments/{urllib_parse.quote(deployment)}"
                f"/chat/completions?api-version={api_version}"
            )

        body = {"messages": messages, "temperature": 0.3, "max_tokens": 400}
        headers = {"api-key": api_key, "Content-Type": "application/json"}
        client = cls.http()
        response = await client.post(url, json=body, headers=headers)
        if response.status_code != 200:
            raise NokvoOneAgentRuntimeError(
                f"Azure OpenAI returned {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            return ""
        content = ((choices[0] or {}).get("message") or {}).get("content") or ""
        return content.strip()


class NokvoOneAgentRuntime:
    @staticmethod
    async def chat_turn(
        db: AsyncSession,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        agent_system_prompt: str | None,
        business_type: str | None,
        tool_keys: list[str],
        user_message: str,
    ) -> dict[str, Any]:
        system_prompt = _build_system_prompt(agent_system_prompt, tool_keys, business_type)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        try:
            raw_reply = await _AzureOpenAIClient.chat(messages)
        except NokvoOneAgentRuntimeError:
            raise

        tool_calls: list[dict[str, Any]] = []
        parsed = _parse_tool_call(raw_reply)
        if parsed is not None:
            tool_key, args = parsed
            if tool_key not in tool_keys:
                final_text = (
                    "The agent attempted to call a tool that is not enabled for this agent. "
                    "Please choose a different action."
                )
                tool_calls.append(
                    {"tool": tool_key, "arguments": args, "ok": False, "error": "tool_not_enabled"}
                )
                return {"reply": final_text, "tool_calls": tool_calls}

            try:
                result = await PredefinedToolsService.execute(
                    db, organization_id, user_id, tool_key, args
                )
                await db.commit()
                tool_calls.append({"tool": tool_key, "arguments": args, "result": result})
                follow_up_messages = messages + [
                    {"role": "assistant", "content": raw_reply},
                    {
                        "role": "user",
                        "content": (
                            f"TOOL_RESULT for {tool_key}: {json.dumps(result)}.\n"
                            "Summarise this result for the user in one or two short sentences. "
                            "Do not include the raw JSON."
                        ),
                    },
                ]
                final_text = await _AzureOpenAIClient.chat(follow_up_messages)
            except ValueError as exc:
                await db.rollback()
                tool_calls.append(
                    {"tool": tool_key, "arguments": args, "ok": False, "error": str(exc)}
                )
                final_text = f"Tool call failed: {exc}"
            except NokvoOneAgentRuntimeError as exc:
                tool_calls.append({"tool": tool_key, "arguments": args, "ok": False, "error": str(exc)})
                final_text = "Tool call succeeded but the agent could not summarise the result."
            return {"reply": final_text or raw_reply, "tool_calls": tool_calls}

        return {"reply": raw_reply, "tool_calls": tool_calls}
