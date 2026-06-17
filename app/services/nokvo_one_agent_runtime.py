"""
Minimal chat runtime for Nokvo One agents.

Uses Azure OpenAI (global deployment from settings) and exposes a per-org
**dynamic** tool catalog. The catalog is produced by
`dynamic_tool_resolver.resolve_catalog` from the org's business_type +
schema_overrides, then narrowed to the agent's enabled `tool_keys`.

The preferred path uses Azure/OpenAI native tool calls with a bounded loop, so
the model can create/search/update multiple records in one turn. The legacy
JSON-code-fence tool protocol remains as a fallback for older deployments.
Every side-effect still flows through PredefinedToolsService, which audits and
idempotency-guards each successful invocation.
"""
from __future__ import annotations

import dataclasses
import json
import re
import urllib.parse as urllib_parse
import uuid
from copy import deepcopy
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.dynamic_tool_resolver import resolve_index
from app.services.nokvo_one_business_templates import business_template_prompt
from app.services.predefined_tools_service import (
    PredefinedTool,
    PredefinedToolsService,
    resolve_legacy_key,
)


class NokvoOneAgentRuntimeError(Exception):
    pass


_TOOL_CALL_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _section(title: str, body: str) -> str:
    return f"{title}\n{body.strip()}"


def _build_system_prompt(
    base_prompt: str | None,
    enabled_tools: list[PredefinedTool],
    business_type: str | None,
    *,
    native_tool_calling: bool = False,
    projects_section: str | None = None,
    working_hours_section: str | None = None,
) -> str:
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

    if projects_section:
        sections.append(_section("Real-estate projects:", projects_section))

    if working_hours_section:
        sections.append(_section("Site-visit working hours:", working_hours_section))

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

    sections.append(
        _section(
            "Member and availability rules:",
            (
                "The workspace has assignable members. For clinics, members are doctors/staff; for real estate, "
                "members are agents. When booking appointments, site visits, callbacks tied to visits, or any "
                "member-owned work, use the enabled tool that creates the record so the backend can check member "
                "working hours, capacity, blocked slots, and current load. Never claim an appointment or visit is "
                "confirmed unless the tool result says a member was assigned or the status is confirmed. If no "
                "member is available, say the preferred time has been noted and the team must confirm another slot."
            ),
        )
    )

    if enabled_tools:
        tool_lines = [
            "Use the provided tools when a user asks you to create, update, search, assign, log, "
            "schedule, or escalate workspace work."
        ]
        for tool in enabled_tools:
            confirmation = " Requires human confirmation." if tool.requires_confirmation else ""
            if native_tool_calling:
                tool_lines.append(f"- {tool.key}: {tool.description}{confirmation}")
            else:
                tool_lines.append(
                    f"- {tool.key}: {tool.description}\n"
                    f"  input_schema: {json.dumps(tool.input_schema)}"
                    + (" (REQUIRES HUMAN CONFIRMATION)" if tool.requires_confirmation else "")
                )
        if native_tool_calling:
            tool_lines.append(
                "Tool schemas are provided through native tool calling. If a required field is missing, "
                "ask one short clarifying question. Do not guess values, and do not claim success until "
                "a tool result confirms the write."
            )
        else:
            tool_lines.append(
                "To call a tool, respond with ONLY a JSON code block like:\n"
                "```json\n"
                '{"tool": "<tool_key>", "arguments": {...}}\n'
                "```\n"
                "Only call a tool when every required field in its input_schema is known. "
                "If a required field is missing, ask the user for exactly that missing detail instead of calling the tool. "
                "Do not include unsupported fields, nulls, empty strings, or guessed values. "
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
                "- Escalate to a human (via escalate_to_human if enabled) for urgent, regulated, "
                "high-risk, privacy-sensitive, or ambiguous requests."
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


def _tool_error_reply(error: str) -> str:
    if error.startswith("Missing required fields:"):
        fields = error.split(":", 1)[1].strip()
        if fields:
            label = fields.replace("_", " ")
            return f"I need one more detail before I can do that: {label}."
    if error.startswith("Unsupported fields"):
        return "I tried to use a tool with details it does not accept. Let me ask for the needed fields clearly."
    return f"I could not complete that action: {error}"


def _with_project_choices(
    tools: list[PredefinedTool],
    projects: list[Any],
) -> list[PredefinedTool]:
    """Return ``tools`` with any project_id/project_name field constrained to
    the org's live project list.

    Only the MODEL-FACING tool list is enriched — execution validates against
    the freshly-resolved (lenient) catalog, so a near-miss from the model or
    the deterministic voice FSM still resolves via fuzzy ``find_project_match``
    instead of being hard-rejected. Tools without project fields (e.g.
    ``leads_create``) are returned untouched.
    """
    if not projects:
        return tools
    from app.services.real_estate_project_service import project_choices_for_tool_schema

    choices = project_choices_for_tool_schema(projects)
    enriched: list[PredefinedTool] = []
    for tool in tools:
        props = (tool.input_schema or {}).get("properties") or {}
        if "project_name" not in props and "project_id" not in props:
            enriched.append(tool)
            continue
        new_schema = deepcopy(tool.input_schema)
        new_props = new_schema.setdefault("properties", {})
        for key, fragment in choices.items():
            if key in new_props:
                new_props[key] = {**new_props[key], **fragment}
        enriched.append(dataclasses.replace(tool, input_schema=new_schema))
    return enriched


def _openai_tool_definitions(enabled_tools: list[PredefinedTool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.key,
                "description": tool.description
                + (" This action requires human confirmation." if tool.requires_confirmation else ""),
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }
        for tool in enabled_tools
    ]


def _message_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(item, str):
                chunks.append(item)
        return "\n".join(chunks).strip()
    return str(content).strip()


def _assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    history: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content") if message.get("content") is not None else "",
    }
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        history["tool_calls"] = tool_calls
    return history


def _parse_native_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = call.get("function") or {}
    name = function.get("name") or call.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("Tool call is missing a function name.")
    raw_args = function.get("arguments") or call.get("arguments") or {}
    if isinstance(raw_args, dict):
        return name, raw_args
    if not isinstance(raw_args, str):
        raise ValueError(f"Tool '{name}' arguments must be a JSON object.")
    try:
        parsed = json.loads(raw_args or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Tool '{name}' arguments were not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Tool '{name}' arguments must be a JSON object.")
    return name, parsed


def _native_tool_message(tool_call_id: str, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": json.dumps(payload, default=str),
    }


def _deterministic_tool_result_reply(tool_calls: list[dict[str, Any]]) -> str:
    successes = [call for call in tool_calls if call.get("result")]
    if not successes:
        errors = [str(call.get("error") or "") for call in tool_calls if call.get("error")]
        return _tool_error_reply(errors[-1]) if errors else "I could not complete that action."
    latest = successes[-1]
    result = latest.get("result") or {}
    assignment_status = result.get("assignment_status")
    assigned_name = result.get("assigned_member_name")
    if assignment_status == "assigned" and assigned_name:
        return f"I've recorded the request and assigned it to {assigned_name}."
    if assignment_status == "no_available_member":
        return "I've recorded the request. That preferred time is noted, and the team will confirm availability."
    if result.get("idempotent"):
        return "That request was already recorded, so I did not create a duplicate."
    return "I've recorded that request."


class _AzureOpenAIClient:
    _client: httpx.AsyncClient | None = None

    @classmethod
    def http(cls) -> httpx.AsyncClient:
        if cls._client is None or cls._client.is_closed:
            cls._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        return cls._client

    @classmethod
    def _chat_url(cls) -> str:
        endpoint = (settings.AZURE_OPENAI_GLOBAL_ENDPOINT or "").rstrip("/")
        if not endpoint or not settings.AZURE_OPENAI_GLOBAL_API_KEY:
            raise NokvoOneAgentRuntimeError("Azure OpenAI is not configured for Nokvo One")
        deployment = settings.AZURE_OPENAI_GLOBAL_DEPLOYMENT or "gpt-5.4-mini"
        api_version = urllib_parse.quote(settings.AZURE_OPENAI_GLOBAL_API_VERSION.strip())
        if "/openai/deployments/" in endpoint:
            return f"{endpoint}?api-version={api_version}" if "api-version=" not in endpoint else endpoint
        return (
                f"{endpoint}/openai/deployments/{urllib_parse.quote(deployment)}"
                f"/chat/completions?api-version={api_version}"
        )

    @classmethod
    async def _chat_completion(cls, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"api-key": settings.AZURE_OPENAI_GLOBAL_API_KEY, "Content-Type": "application/json"}
        client = cls.http()
        response = await client.post(cls._chat_url(), json=body, headers=headers)
        if response.status_code != 200:
            raise NokvoOneAgentRuntimeError(
                f"Azure OpenAI returned {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    @classmethod
    async def chat(cls, messages: list[dict[str, Any]]) -> str:
        body = {"messages": messages, "temperature": 0.3, "max_tokens": 400}
        payload = await cls._chat_completion(body)
        choices = payload.get("choices") or []
        if not choices:
            return ""
        content = ((choices[0] or {}).get("message") or {}).get("content") or ""
        return content.strip()

    @classmethod
    async def chat_with_tools(
        cls,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body = {
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 500,
            "tools": tools,
            "tool_choice": "auto",
        }
        payload = await cls._chat_completion(body)
        choices = payload.get("choices") or []
        if not choices:
            return {"role": "assistant", "content": ""}
        return dict(((choices[0] or {}).get("message") or {}) or {"role": "assistant", "content": ""})


class NokvoOneAgentRuntime:
    @staticmethod
    async def _complete_native_tool_loop(
        db: AsyncSession,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        messages: list[dict[str, Any]],
        first_message: dict[str, Any],
        native_tools: list[dict[str, Any]],
        catalog_index: dict[str, PredefinedTool],
        enabled_keys: set[str],
        *,
        agent_id: uuid.UUID | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        runtime_messages = [dict(message) for message in messages]
        assistant_message = first_message
        tool_calls: list[dict[str, Any]] = []
        max_iterations = max(1, int(settings.NOKVO_ONE_TOOL_LOOP_MAX_ITERATIONS or 4))

        for iteration in range(max_iterations):
            calls = assistant_message.get("tool_calls") or []
            if not calls:
                return {
                    "reply": _message_content_text(assistant_message.get("content")),
                    "tool_calls": tool_calls,
                }

            runtime_messages.append(_assistant_message_for_history(assistant_message))

            for call in calls:
                call_id = str(call.get("id") or f"tool_call_{iteration}_{len(tool_calls)}")
                tool_name = "unknown_tool"
                args: dict[str, Any] = {}
                try:
                    requested_key, args = _parse_native_tool_call(call)
                    resolved_key = resolve_legacy_key(requested_key)
                    tool_name = resolved_key
                    if resolved_key not in enabled_keys:
                        raise ValueError(f"Tool '{requested_key}' is not enabled for this agent.")

                    tool = catalog_index[resolved_key]
                    result = await PredefinedToolsService.execute(
                        db,
                        organization_id,
                        user_id,
                        tool,
                        args,
                        agent_id=agent_id,
                        session_id=session_id,
                    )
                    await db.commit()
                    tool_calls.append(
                        {
                            "tool": tool.key,
                            "arguments": args,
                            "result": result,
                            "ok": True,
                            "iteration": iteration,
                        }
                    )
                    runtime_messages.append(
                        _native_tool_message(
                            call_id,
                            tool.key,
                            {
                                "ok": True,
                                "result": result,
                                "instruction": (
                                    "Summarise this result for the user. If assignment_status is "
                                    "no_available_member, do not say it is confirmed; say the requested "
                                    "time was recorded and the team will confirm availability."
                                ),
                            },
                        )
                    )
                except ValueError as exc:
                    await db.rollback()
                    error = str(exc)
                    tool_calls.append(
                        {
                            "tool": tool_name,
                            "arguments": args,
                            "ok": False,
                            "error": error,
                            "iteration": iteration,
                        }
                    )
                    runtime_messages.append(
                        _native_tool_message(call_id, tool_name, {"ok": False, "error": error})
                    )

            if iteration == max_iterations - 1:
                break

            try:
                assistant_message = await _AzureOpenAIClient.chat_with_tools(runtime_messages, native_tools)
            except NokvoOneAgentRuntimeError:
                return {
                    "reply": _deterministic_tool_result_reply(tool_calls),
                    "tool_calls": tool_calls,
                }

        return {
            "reply": _deterministic_tool_result_reply(tool_calls),
            "tool_calls": tool_calls,
        }

    @staticmethod
    async def chat_turn(
        db: AsyncSession,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        agent_system_prompt: str | None,
        business_type: str | None,
        tool_keys: list[str],
        user_message: str,
        *,
        agent_id: uuid.UUID | None = None,
        schema_overrides: dict[str, Any] | None = None,
        custom_tabs: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        catalog_index = resolve_index(business_type, schema_overrides, custom_tabs)
        enabled: list[PredefinedTool] = []
        enabled_keys: set[str] = set()
        for raw_key in tool_keys or []:
            resolved = resolve_legacy_key(raw_key)
            tool = catalog_index.get(resolved)
            if tool is not None and tool.key not in enabled_keys:
                enabled.append(tool)
                enabled_keys.add(tool.key)

        projects_section = ""
        working_hours_section = ""
        if (business_type or "").lower() == "real_estate":
            from app.services.real_estate_project_service import (
                load_active_projects,
                projects_prompt_section,
            )
            from app.services.nokvo_one_assignment_service import (
                NokvoOneAssignmentService,
                working_hours_prompt_line,
            )

            projects = await load_active_projects(db, organization_id)
            projects_section = projects_prompt_section(projects)
            org_defaults = await NokvoOneAssignmentService.resolve_org_working_window(db, organization_id)
            working_hours_section = working_hours_prompt_line(org_defaults)
            # Constrain project fields the model sees to real project names.
            # Execution still uses the lenient ``catalog_index`` below.
            enabled = _with_project_choices(enabled, projects)

        native_tool_calling = bool(enabled) and bool(settings.NOKVO_ONE_NATIVE_TOOL_CALLING)
        system_prompt = _build_system_prompt(
            agent_system_prompt,
            enabled,
            business_type,
            native_tool_calling=native_tool_calling,
            projects_section=projects_section,
            working_hours_section=working_hours_section,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        if native_tool_calling:
            native_tools = _openai_tool_definitions(enabled)
            try:
                first_message = await _AzureOpenAIClient.chat_with_tools(messages, native_tools)
            except NokvoOneAgentRuntimeError:
                # Older Azure deployments may reject native tool calls. Keep the
                # legacy JSON-fence protocol alive so existing agents still work.
                first_message = None
            if first_message is not None:
                return await NokvoOneAgentRuntime._complete_native_tool_loop(
                    db,
                    organization_id,
                    user_id,
                    messages,
                    first_message,
                    native_tools,
                    catalog_index,
                    enabled_keys,
                    agent_id=agent_id,
                    session_id=session_id,
                )
            system_prompt = _build_system_prompt(
                agent_system_prompt,
                enabled,
                business_type,
                native_tool_calling=False,
                projects_section=projects_section,
                working_hours_section=working_hours_section,
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

        raw_reply = await _AzureOpenAIClient.chat(messages)

        tool_calls: list[dict[str, Any]] = []
        parsed = _parse_tool_call(raw_reply)
        if parsed is None:
            return {"reply": raw_reply, "tool_calls": tool_calls}

        requested_key, args = parsed
        resolved_key = resolve_legacy_key(requested_key)
        if resolved_key not in enabled_keys:
            tool_calls.append(
                {
                    "tool": requested_key,
                    "arguments": args,
                    "ok": False,
                    "error": "tool_not_enabled",
                }
            )
            return {
                "reply": (
                    "The agent attempted to call a tool that is not enabled for this agent. "
                    "Please choose a different action."
                ),
                "tool_calls": tool_calls,
            }

        tool = catalog_index[resolved_key]
        try:
            result = await PredefinedToolsService.execute(
                db,
                organization_id,
                user_id,
                tool,
                args,
                agent_id=agent_id,
                session_id=session_id,
            )
            await db.commit()
            tool_calls.append({"tool": tool.key, "arguments": args, "result": result})
            follow_up_messages = messages + [
                {"role": "assistant", "content": raw_reply},
                {
                    "role": "user",
                    "content": (
                        f"TOOL_RESULT for {tool.key}: {json.dumps(result, default=str)}.\n"
                        "Summarise this result for the user in one or two short sentences. "
                        "Do not include the raw JSON. If assignment_status is no_available_member, do not say the "
                        "appointment or visit is confirmed; say the requested time was recorded and the team will "
                        "confirm availability. If assigned_member_name is present, mention who it was assigned to."
                    ),
                },
            ]
            final_text = await _AzureOpenAIClient.chat(follow_up_messages)
        except ValueError as exc:
            await db.rollback()
            tool_calls.append(
                {"tool": tool.key, "arguments": args, "ok": False, "error": str(exc)}
            )
            final_text = _tool_error_reply(str(exc))
        except NokvoOneAgentRuntimeError as exc:
            tool_calls.append({"tool": tool.key, "arguments": args, "ok": False, "error": str(exc)})
            final_text = "Tool call succeeded but the agent could not summarise the result."
        return {"reply": final_text or raw_reply, "tool_calls": tool_calls}
