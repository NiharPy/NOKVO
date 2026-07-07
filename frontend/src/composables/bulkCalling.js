// Shared, framework-free bulk-calling logic — used by BOTH Nokvo One's
// BulkLeadCapturingView (non-deterministic) and NOKVO APEX (deterministic) so
// the two product surfaces can never drift on categorization, the create
// payload, or the capacity math. Pure functions only (no Vue refs).

// ── Per-number outcome category ─────────────────────────────────────────────
// successful     — crossed the lead score → qualified (Qualified Leads tab)
// not_interested — connected but below the lead score / not interested
// no_pickup      — no connection (no answer / telephony failure)
// pending        — not final yet (queued/dialing, or answered-but-not-scored)
// Scoring wins over telephony status (a scored contact definitionally connected;
// the Plivo answer webhook isn't wired so a connected call can read status=failed).
export function leadCategory(ct, campaign) {
  const hasQ = !!(campaign.questionnaire && (campaign.questionnaire.questions || []).length);
  if (hasQ) {
    const scored = typeof ct.lead_score === 'number' || typeof ct.qualified === 'boolean';
    if (scored) {
      const threshold = Number(campaign.questionnaire.threshold) || 0;
      const successful =
        ct.qualified === true ||
        (typeof ct.lead_score === 'number' && ct.lead_score >= threshold);
      return successful ? 'successful' : 'not_interested';
    }
  } else if (ct.interest_outcome != null && ct.interest_outcome !== '') {
    return ct.interest_outcome === 'interested' ? 'successful' : 'not_interested';
  }
  const status = ct.status || 'pending';
  const connected = !!ct.answered_at || status === 'answered';
  if (!connected && (status === 'no_answer' || status === 'failed' || ct.ended)) {
    return 'no_pickup';
  }
  return 'pending';
}

// Group every dialed contact across the given campaigns into outcome buckets.
// One pass → mutually exclusive groups (each contact in exactly one). Rows carry
// the score/breakdown/note where relevant. `byRecent` sort, most recent first.
export function categorizeContacts(campaigns) {
  const groups = { successful: [], not_interested: [], no_pickup: [], pending: [] };
  for (const c of campaigns || []) {
    const hasQ = !!(c.questionnaire && (c.questionnaire.questions || []).length);
    const maxScore = hasQ ? (c.max_score || (c.questionnaire.questions || []).length) : null;
    for (const ct of c.contacts || []) {
      const cat = leadCategory(ct, c);
      groups[cat].push({
        name: ct.name || ct.phone,
        raw_name: ct.name || '',   // true name (no phone fallback) — for CSV export
        phone: ct.phone,
        campaign_id: c.id,
        campaign_name: c.name,
        answered_at: ct.answered_at,
        call_link_id: ct.call_link_id,
        status: ct.status,
        lead_score: hasQ && typeof ct.lead_score === 'number' ? ct.lead_score : null,
        max_score: maxScore,
        score_breakdown: hasQ ? (ct.score_breakdown || []) : null,
        lead_score_reason: hasQ ? (ct.lead_score_reason || null) : (ct.interest_reason || null),
        call_note: ct.call_note || null,
      });
    }
  }
  const byRecent = (a, b) => String(b.answered_at || '').localeCompare(String(a.answered_at || ''));
  for (const k of Object.keys(groups)) groups[k].sort(byRecent);
  return groups;
}

// The configured calling schedule (from agent_config.call_window). With a daily
// cap → "5d/wk · 200/day" (APEX scheduled dialer); otherwise the legacy capacity
// readout "5d × 8h".
export function campaignWindow(c) {
  const w = c?.agent_config?.call_window;
  if (!w || !w.working_days) return null;
  if (w.calls_per_day) return `${w.working_days}d/wk · ${Number(w.calls_per_day).toLocaleString('en-IN')}/day`;
  if (w.hours_per_day) return `${w.working_days}d × ${w.hours_per_day}h`;
  return null;
}

// ── Estimated call time + calling capacity (deterministic planning aid) ──────
export const EST_OVERHEAD_S = 12; // opener (~7s) + outro (~5s)
export const EST_INTENT_S = 11; // ask + short yes/no + latency
export const EST_ANSWER_S = 15; // ask + open-ended answer + latency

export function estCallSeconds(questions) {
  const qs = questions || [];
  if (!qs.length) return 0;
  const perQ = qs.reduce((s, q) => s + (q.type === 'answer' ? EST_ANSWER_S : EST_INTENT_S), 0);
  return EST_OVERHEAD_S + perQ;
}

export function fmtDuration(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m <= 0) return `${r}s`;
  return r ? `${m}m ${r}s` : `${m}m`;
}

export function callCapacity(workingDays, hoursPerDay, perCallSeconds) {
  const days = Math.max(1, Math.min(60, Math.round(Number(workingDays) || 1)));
  const hours = Math.max(1, Math.min(10, Math.round(Number(hoursPerDay) || 1)));
  const totalHours = days * hours;
  const calls = perCallSeconds > 0 ? Math.floor((totalHours * 3600) / perCallSeconds) : 0;
  return { days, hours, totalHours, calls };
}

// ── APEX scheduled-dialer estimation (Call Credits + duration) ───────────────
// APEX runs N calls/day over D weekdays inside 09:00–19:00 IST. Given the list
// size, the daily cap and the estimated call length, work out how long the list
// takes and the Call Credits it can consume. Spend uses the APEX per-call cost
// (₹1.50 connect ONCE + (slab−1.50)/60 per second) — mirrors apex_call_cost.
export function apexCallCredits(perCallSeconds, slabRate) {
  const s = Math.max(0, Number(perCallSeconds) || 0);
  if (s <= 0) return 0;
  const rate = (Math.max(0, Number(slabRate) || 0) - 1.5) / 60;
  return 1.5 + rate * s;
}

export function scheduleEstimate({ listSize, callsPerDay, workingDays, perCallSeconds, slabRate }) {
  const L = Math.max(0, Math.floor(Number(listSize) || 0));
  const C = Math.max(0, Math.floor(Number(callsPerDay) || 0));
  const D = Math.max(1, Math.min(7, Math.floor(Number(workingDays) || 1)));
  const perCall = apexCallCredits(perCallSeconds, slabRate);
  const callDays = C > 0 && L > 0 ? Math.ceil(L / C) : null;        // working days to clear the list
  const weeks = callDays != null ? Math.ceil(callDays / D) : null;  // calendar weeks
  return {
    listSize: L,
    callDays,
    weeks,
    perCallCredits: perCall,
    dailySpend: C > 0 ? C * perCall : null,
    maxSpend: L > 0 ? L * perCall : null,
  };
}

// Build + download a CSV of categorized contacts: phone in column A, name (only
// when a real one exists — uses raw_name, not the phone fallback) in column B.
// RFC-4180 quoting so names with commas/quotes/newlines stay in one cell. Rows
// with no phone are skipped. Triggers a browser download of `filename`.
export function exportContactsCsv(rows, filename = 'contacts.csv') {
  const esc = (v) => {
    const s = String(v ?? '').trim();
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = ['Phone,Name'];
  for (const r of rows || []) {
    const phone = esc(r?.phone);
    if (!phone) continue;
    lines.push(`${phone},${esc(r?.raw_name)}`);
  }
  const blob = new Blob([lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Best-effort CSV row count (data rows, minus a header) for the estimation widget.
// Returns null for non-CSV (e.g. XLSX) — the caller falls back to "—".
export function countCsvRows(file) {
  return new Promise((resolve) => {
    if (!file || !/\.csv$/i.test(file.name || '')) return resolve(null);
    const reader = new FileReader();
    reader.onload = () => {
      const lines = String(reader.result || '').split(/\r?\n/).filter((l) => l.trim().length);
      resolve(Math.max(0, lines.length - 1));
    };
    reader.onerror = () => resolve(null);
    reader.readAsText(file);
  });
}

// ── Questionnaire helpers + create-campaign payload ──────────────────────────
// Points are the admin's own weighting scheme — the ceiling is fat-finger
// insurance only, NOT a product rule (a 200-pt dealbreaker question is
// legitimate; the old 100 cap silently corrupted scores and thresholds).
const clampPts = (v) => Math.max(1, Math.min(10000, Math.round(Number(v) || 1)));

export function makeId(prefix) {
  return (
    (typeof crypto !== 'undefined' && crypto.randomUUID && crypto.randomUUID()) ||
    (prefix + Date.now().toString(36) + Math.random().toString(16).slice(2, 6))
  );
}

export function newQuestion() {
  return { id: makeId('q'), type: 'intent', text: '', desired_answer: '', required: 'yes', gate: false, points: 1, graded: false, tiers: [] };
}

// Max achievable lead score (top band for a graded answer, else its weight) —
// mirrors backend questionnaire_max_points.
export function maxPointsFor(questions) {
  return (questions || []).reduce((sum, q) => {
    if (q.type === 'answer' && q.graded && (q.tiers || []).length) {
      const pts = q.tiers.map((t) => Math.max(1, Number(t.points) || 1));
      return sum + (pts.length ? Math.max(...pts) : 0);
    }
    return sum + Math.max(1, Number(q.points) || 1);
  }, 0);
}

// Non-empty pre-translated languages only ({en,hi,te} with blanks dropped).
export function pruneI18n(i18n) {
  const out = {};
  for (const [k, v] of Object.entries(i18n || {})) {
    const t = (v || '').trim();
    if (t) out[k] = t;
  }
  return out;
}

export function hasI18n(i18n) {
  return Object.keys(pruneI18n(i18n)).length > 0;
}

// Normalize the builder's questions into the API shape (drops blanks, clamps
// points, normalizes graded tiers). Mirrors the Nokvo One bulk create exactly.
export function cleanQuestions(questions) {
  return (questions || [])
    .map((q) => {
      const type = q.type === 'answer' ? 'answer' : 'intent';
      const graded = type === 'answer' && !!q.graded;
      const tiers = graded
        ? (q.tiers || [])
            .map((t) => ({ label: (t.label || '').trim(), points: clampPts(t.points) }))
            .filter((t) => t.label)
        : [];
      const out = {
        type,
        text: (q.text || '').trim(),
        desired_answer: type === 'answer' && !graded ? (q.desired_answer || '').trim() : '',
        required: type === 'answer' ? undefined : (q.required === 'no' ? 'no' : 'yes'),
        gate: graded ? false : !!q.gate,
        points: clampPts(q.points),
      };
      if (tiers.length) out.tiers = tiers;
      // Reviewed/hand-edited translations (translate-preview flow) ride along;
      // the server preserves non-empty languages and fills only the blanks.
      if (hasI18n(q.text_i18n)) out.text_i18n = pruneI18n(q.text_i18n);
      return out;
    })
    .filter((q) => q.text);
}

// Build the multipart FormData for POST /bulk-calling/campaigns. `deterministic`
// selects the path. Throws { message } on a validation failure so the caller can
// surface it. Mirrors startBulkCallingCampaign in NokvoOneApp.vue.
export function buildBulkCampaignFormData(form, { deterministic }) {
  const fail = (message) => { throw { message }; };
  if (!(form.name || '').trim()) fail('Name the campaign.');
  if (!form.file) fail('Choose a CSV (or XLSX) of phone numbers.');

  const cleaned = cleanQuestions(form.questions);
  if (deterministic) {
    if (!(form.intro || '').trim()) fail('Add an intro — how the agent opens the call.');
    if (!cleaned.length) fail('Add at least one question for a deterministic campaign.');
    for (const q of cleaned) {
      if (q.type !== 'answer') continue;
      if (q.tiers) {
        if (!q.tiers.length) fail(`"${q.text.slice(0, 40)}" is graded but has no scoring bands — add a band, or turn off graded scoring.`);
      } else if (!q.desired_answer) {
        fail('Each "Desired answer" question needs an expected answer (add one, turn on graded scoring, or switch it to Intent detection).');
      }
    }
  } else if (!(form.content || '').trim()) {
    fail('Add the campaign content — what the agent should say.');
  }

  const maxPoints = maxPointsFor(cleaned);
  const clampedThreshold = cleaned.length
    ? Math.min(maxPoints, Math.max(1, Number(form.threshold) || maxPoints))
    : 1;

  const fd = new FormData();
  fd.append('name', form.name.trim());
  fd.append('deterministic', deterministic ? 'true' : 'false');
  fd.append('content', (form.content || '').trim());
  if ((form.company_name || '').trim()) fd.append('company_name', form.company_name.trim());
  if ((form.caller_name || '').trim()) fd.append('caller_name', form.caller_name.trim());
  if (form.language) fd.append('language', form.language);
  if (deterministic) {
    const questionnaire = {
      intro: (form.intro || '').trim(),
      outro: (form.outro || '').trim(),
      questions: cleaned,
      threshold: clampedThreshold,
    };
    // Reviewed/hand-edited translations from the translate-preview flow. The
    // server preserves any non-empty language and only fills the blanks.
    if (hasI18n(form.intro_i18n)) questionnaire.intro_i18n = pruneI18n(form.intro_i18n);
    if (hasI18n(form.outro_i18n)) questionnaire.outro_i18n = pruneI18n(form.outro_i18n);
    fd.append('questionnaire', JSON.stringify(questionnaire));
    fd.append('working_days', String(Math.max(1, Math.min(7, Math.round(Number(form.working_days) || 5)))));
    fd.append('hours_per_day', String(Math.max(1, Math.min(10, Math.round(Number(form.hours_per_day) || 10)))));
    if (form.calls_per_day) {
      fd.append('calls_per_day', String(Math.max(1, Math.round(Number(form.calls_per_day)))));
    }
  }
  fd.append('contacts_file', form.file);
  return fd;
}
