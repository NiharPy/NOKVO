# NOKVO One Failure Matrix

Last updated: 2026-05-21

This matrix maps the main failure classes in the current NOKVO One system to their impact, detection surface, and the right response pattern. It is organized for implementation and test planning rather than as a theoretical catalog of every possible runtime exception.

## 1. Authentication and Access

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| Invalid or expired JWT | Auth guards | User is blocked | 401/403 response | Force re-login |
| Wrong principal type | Auth guards | Wrong surface denied | JWT claims check | Reject request |
| Disabled user / suspended org | Auth guards | Access denied | User/org status check | Reject request |
| MFA required and TOTP fails | Login / protected routes | User cannot proceed | MFA validation | Ask for correct code |
| `Google sign-in failed` | Google auth flow | Login blocked | OAuth callback error | Show real reason, retry |
| `Could not initialise TOTP` | MFA setup | TOTP setup blocked | TOTP setup error | Allow pass-through when TOTP absent, retry setup when present |

## 2. Tenant and Provisioning

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| Tenant resources not found | Tenant lookup | Request cannot resolve org context | DB lookup returns none | Stop with 404 |
| Organization not found | Tenant lookup | No runtime context | DB lookup returns none | Stop with 404 |
| Product tier mismatch | Nokvo One access | User cannot use surface | Org tier check | Reject request |
| Missing provider status | Provisioning / runtime | Integrations unavailable | Null/empty config | Degrade gracefully |
| Wrong redirect URI / callback URL | OAuth setup | OAuth and webhooks fail | Provider error | Fix config and redeploy |
| Port binding blocked | Local deployment | Backend cannot start | Uvicorn bind failure | Run on allowed loopback or approved host |

## 3. Meta Ads / Instagram Lead Ads

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| `META_ADS_APP_ID` missing | OAuth start / exchange | Meta login cannot start | Config check | Add env var |
| `META_ADS_APP_SECRET` missing | Token exchange | OAuth callback fails | Config check | Add env var |
| `META_ADS_REDIRECT_URI` missing | OAuth start / exchange | OAuth callback cannot complete | Config check | Add env var and match Meta dashboard |
| Verify token mismatch | Webhook validation | Meta webhook rejected | GET challenge failure | Update Meta dashboard token |
| Missing permissions / app review | Lead retrieval | Leads do not sync | Graph API error / empty data | Request and approve scopes |
| Page not subscribed to `leadgen` | Webhook delivery | No real-time leads | No webhook events | Subscribe Page to field |
| Lead payload missing ids | Ingestion | Lead not created | Ingest validation | Reject or log for review |
| Token expired / invalid | Lead sync / webhook pulls | Sync stops | 401 from Graph API | Refresh or reconnect |

## 4. Google Ads

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| OAuth client missing | OAuth start / exchange | Google Ads login cannot start | Config check | Add env vars |
| Redirect URI mismatch | OAuth callback | OAuth exchange fails | Google OAuth error | Fix console config |
| Developer token missing | API access | Ads API calls fail | API auth error | Add token from Ads API Center |
| Login customer ID missing | MCC flows | API calls may fail or be scoped wrong | Request validation | Add manager/customer ID |
| Scope not granted | OAuth consent | Lead form data unavailable | OAuth error / partial access | Re-consent with correct scope |
| API quota / permission failure | Lead sync | Sync unavailable or partial | API error | Retry later or narrow query |

## 5. Google Forms

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| Forms API not enabled | OAuth / sync | Form reads fail | API error | Enable API in Google Cloud |
| OAuth consent missing scope | Read forms/responses | No form data | OAuth error | Reauthorize with correct scopes |
| Form inaccessible | Sync | No data import | API error / empty list | Fix sharing / ownership |
| Wrong field mapping | Lead ingestion | Lead fields malformed | Validation mismatch | Re-map fields in Nokvo |
| Consent field absent | Lead validation | Lead may not be callable | Consent check failure | Require explicit consent field |

## 6. Lead Validation and Consent

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| No phone number | Lead import / campaign launch | Lead cannot be called | Callable-lead validation | Reject lead for outbound |
| Consent not granted | Lead import / launch | Lead cannot be called | Consent flag check | Mark uncallable |
| Lead opted out | Ongoing lead state | Must not call | Opt-out flag | Exclude from campaign |
| Selected leads not found | Campaign launch | Campaign cannot start | Missing IDs | Fail fast |
| Unsupported provider | Lead handling | Source rejected | Provider enum check | Reject source |

## 7. Outbound Campaigns and Telephony

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| No Exotel caller ID configured | Campaign creation / launch | Outbound cannot start | Config check | Link number or set default |
| Exotel API auth failure | Call initiation | Calls do not place | API error | Refresh credentials |
| Inbound/outbound webhook mismatch | Telephony routing | No live media / no status updates | Route failure | Fix callback URLs |
| Media websocket disconnect | Call runtime | Conversation drops | Socket close/error | Reconnect or fail gracefully |
| Call status callback failure | Campaign analytics | Outcomes incomplete | Missing callback | Retry callback processing |
| Call leg overload | Bulk launch | Some calls fail or slow | Provider errors | Throttle / batch launch |

## 8. Speech Input and Output

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| STT API missing key | Voice input | No transcription | Config check | Add API key |
| STT rate limit | Voice input | Slow or failed turns | 429 response | Retry with backoff, then degrade |
| Bad audio / noise / clipping | Voice input | Wrong transcript | Low confidence / poor output | Ask user to repeat |
| Mixed language / code-switching | Voice input / retrieval | Wrong intent or retrieval | Language mismatch / retrieval noise | Use translate-STT and language switch logic |
| Barge-in / interruption | Voice runtime | Old answer may continue or new turn misroutes | Speech-start while speaking | Cancel or arbitrate turn |
| TTS API failure | Voice output | Silent response or fallback text only | API error | Retry once, then fallback |
| Unsupported prosody params | TTS output | Request rejected | 400 from TTS | Retry without prosody modifiers |

## 9. LLM and Retrieval

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| Azure OpenAI key missing | LLM call | No answer generation | Config check | Add key or endpoint |
| Azure OpenAI timeout | LLM call | Slow or refused answer | Timeout | Return graceful fallback |
| Azure OpenAI 429 | LLM call | Delayed response | Rate-limit response | Retry with backoff |
| Malformed LLM output | Answer generation | Bad text or refusal | Sanitizer / parse issue | Sanitize or fallback |
| Retrieval returns no relevant chunks | RAG | No grounded answer | Empty result / low score | Ask clarifying question or refuse safely |
| Wrong topic filter | Retrieval | Missing context | Empty or weak retrieval | Relax filter or use policy card |
| Qdrant unavailable | Retrieval | No grounding path | Search failure | Fallback to safe response |
| Semantic cache miss/stale data | Cache layer | Extra latency or stale answer | Cache key mismatch | Invalidate or recompute |

## 10. Session, State, and Concurrency

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| Redis unavailable | Session memory | History/state lost for turn | Redis error | Fall back to degraded state |
| Session TTL expiry | Long call / inactive call | Memory lost mid-call | Missing history/state | Reconstruct from DB where possible |
| Confirmation state lost | Slot fill flows | Duplicate questions or bad commit | State mismatch | Re-ask and reconfirm |
| Concurrent turn race | Websocket / barge-in | Wrong answer plays | Turn overlap | Cancel stale turn |
| Retry scheduler not running | Background retries | Failed tools stay unresolved | No queue drain | Start scheduler / alert ops |
| Duplicate retry execution | Retry queue | Double action risk | Repeated row processing | Add idempotency guard |

## 11. Business Logic

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| Appointment slot already taken | Booking flow | Wrong slot offered | Availability check | Offer next free slot |
| Invalid date/time | Slot parsing | Booking blocked | Parse validation | Ask for valid date/time |
| Identity not verified | Sensitive actions | Cancellation/refund blocked | Identity check | Ask for booking phone number |
| Wrong workflow branch | FSM / intent routing | Caller gets wrong experience | Mismatch in state | Re-route and re-ask |
| Auto-follow-up not created | Outcome loop | No callback recovery | Missing follow-up row | Retry or manual create |

## 12. Frontend / UX

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| OAuth callback returns error | UI redirect | Setup flow blocked | Query param error | Show real reason and retry |
| Lead/campaign list not loading | Dashboard | Operator cannot act | API failure | Show empty/error state |
| Websocket disconnect | Voice tester | Live test stops | Socket close | Reconnect or surface error |
| MFA screen stuck | Auth flow | User blocked | State mismatch | Reset auth flow |
| Stale UI vs backend state | All surfaces | Confusing operator view | Refresh mismatch | Poll or refetch state |

## 13. Deployment / Config

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| Missing `.env` value | Boot / OAuth / telephony | Feature unavailable | Startup/config validation | Add env var |
| HTTPS unavailable | Webhooks | Meta cannot reach callback | Webhook verification failure | Expose secure endpoint |
| Public base URL wrong | OAuth / redirects | Callback lands wrong place | Bad redirect | Fix base URL |
| Background process not started | Retry / scheduler | Recovery loop broken | No periodic drain | Start scheduler with app |

## 14. Data Quality

| Failure | Where it happens | Impact | Detect | Response |
|---|---|---|---|---|
| Duplicate leads | Lead sync | Double outreach | Dedup logic miss | Normalize provider ids |
| Bad phone formatting | Lead import | Lead not callable | Validation failure | Normalize to E.164 |
| Missing consent text | Form ingestion | Compliance gap | Form validation | Require consent copy |
| Bad Excel headers | Campaign import | Wrong phone/name parsing | Parse fallback | Ask user to fix sheet |
| Poor STT on names/numbers | Voice intake | Wrong record data | Confirmation mismatch | Always read back sensitive values |

## 15. Highest-Risk Classes

The highest-risk failures are the ones that can cause the agent to do the wrong thing, not just fail loudly:

1. Wrong consent state on a lead.
2. Wrong identity verification for a cancellation/refund action.
3. Duplicate execution of a side effect.
4. Stale or incorrect call state during barge-in.
5. Bad phone or name persistence from STT.
6. OAuth/webhook misconfiguration that silently stops lead flow.

These should be covered by tests, not just runtime logging.

## 16. Recommended Recovery Pattern

For each failure class, the default pattern should be:

1. Detect early.
2. Fail closed for side effects.
3. Preserve user experience with a safe fallback response.
4. Write audit state.
5. Retry only when the retry is idempotent and safe.
6. Surface the issue in the operator UI.

## 17. Test Targets

Minimum regression test areas:

- login and MFA
- Meta OAuth and webhook verification
- Google Ads OAuth and sync
- Google Forms ingestion
- consent gating
- campaign launch and callback handling
- STT/TTS failures
- mixed-language turns
- interruption handling
- vague caller clarification
- retry queue processing
- outcome tracking
- stale / duplicate state handling
