<script setup>
// NOKVO APEX — dark product surface for DETERMINISTIC outbound. Own login + MFA
// + dark app shell; shares Nokvo One's accounts + token + bulk-calling backend.
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick, provide } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import {
  login as apexLogin,
  signup as apexSignup,
  requestAccess as apexRequestAccess,
  fetchApexPlans,
  googleLogin as apexGoogleLogin,
  verifyTotp,
  fetchMe,
  fetchCampaigns,
  fetchBulkStatus,
  createCampaign as apiCreateCampaign,
  translateQuestionnaire as apiTranslateQuestionnaire,
  styleRewriteQuestionnaire as apiStyleRewriteQuestionnaire,
  rerunCampaign,
  resumeCampaign,
  updateCampaign,
  addCampaignContacts,
  cancelCampaign,
  deleteCampaign,
  fetchCampaignSummary,
  fetchCampaignContacts,
  downloadCampaignContactsCsv,
  fetchCampaignApiConfig,
  setCampaignApiConfig,
  revealCampaignApiKey,
  rotateCampaignApiKey,
  testCampaignWebhook,
  fetchTranscript,
  submitFeedback as apiSubmitFeedback,
  fetchConfig,
  loadGsi,
  clearToken,
  getToken,
  extractError,
  createSubscription,
  checkAffiliateCode,
  verifyPayment,
  fetchMinutesBalance,
  fetchApexBilling,
  planTopupCreateOrder,
  planTopupVerify,
  cancelSubscription,
  topupCreateOrder,
  topupVerify,
  fetchOnboardingState,
  saveBusinessDetails,
  uploadDocuments,
  loadRazorpay,
  billFor,
  creditedFor,
  apexCreditFor,
  slabRate,
  inviteMember,
  listMembers,
  getInviteContext,
  acceptInvite,
  listQualifiedLeads,
  listMyLeads,
  claimLead,
  setLeadStatus,
} from './apexApi.js';
import ApexCampaign from './views/ApexCampaign.vue';
import ApexQualified from './views/ApexQualified.vue';
import ApexNotInterested from './views/ApexNotInterested.vue';
import ApexNoPickup from './views/ApexNoPickup.vue';
import ApexBusy from './views/ApexBusy.vue';
import ApexCallLogs from './views/ApexCallLogs.vue';
import ApexAvailableLeads from './views/ApexAvailableLeads.vue';
import ApexMyLeads from './views/ApexMyLeads.vue';
import ApexMembers from './views/ApexMembers.vue';
import NovaPanel from './NovaPanel.vue';
import AxIcon from './AxIcon.vue';
import AxCount from './AxCount.vue';
import nokvoMark from '../assets/nokvo-logo.png';  // the real NOKVO mark (header logo)
import { APEX_TERMS_OF_SERVICE_HTML, APEX_PRIVACY_POLICY_HTML, APEX_LEGAL_VERSIONS } from '../content/apexLegalDocs.js';
import './apex-theme.css';

const props = defineProps({ initialAuthState: { type: String, default: 'login' } });
const router = useRouter();
const route = useRoute();

const screen = ref('login'); // login | signup | mfa | app
const tab = ref('campaign'); // campaign | qualified | notint | didnt | logs
const busy = ref(false);
// Mandatory legal acceptance at the (pre-charge) payment step — every new org
// must tick before it can be billed. `legalModal` shows the doc inline (v-html).
const termsAccepted = ref(false);
const legalModal = ref(null); // 'terms' | 'privacy' | null
const APEX_TERMS_HTML = APEX_TERMS_OF_SERVICE_HTML;
const APEX_PRIVACY_HTML = APEX_PRIVACY_POLICY_HTML;
const errorMsg = ref('');

// A verify failure shakes the MFA digit row once. Toggled off→on across a
// frame so a repeat of the same error message still re-fires the animation.
const mfaShake = ref(false);
watch(errorMsg, (v) => {
  if (!v || screen.value !== 'mfa') return;
  mfaShake.value = false;
  requestAnimationFrame(() => {
    mfaShake.value = true;
    setTimeout(() => { mfaShake.value = false; }, 450);
  });
});

// ── auth ──
const form = ref({ email: '', password: '' });
const signupForm = ref({ org_name: '', admin_name: '', admin_email: '', password: '' });
// APEX is request-gated: the public form submits an access request (SuperAdmin then
// creates the account + emails a payment link).
const requestForm = ref({ company_name: '', contact_name: '', email: '', phone: '', requested_plan: '', notes: '' });
const requestSubmitted = ref(false);
const apexPlans = ref([]);
async function loadApexPlans() {
  try { apexPlans.value = await fetchApexPlans(); } catch { apexPlans.value = []; }
}
const mfaDigits = ref(['', '', '', '', '', '']);
const mfaTempToken = ref('');
const user = ref(null);
const googleClientId = ref('');
// 'idle' | 'loading' | 'ready' | 'error' — drives an on-screen fallback so a
// failed Google button is never just a blank space.
const googleState = ref('idle');

// ── payment + onboarding ──
const paymentToken = ref('');
const payMinutes = ref(1000);
const PLATFORM_FEE = 6499;
const onboardingStep = ref('business_details');
const bizForm = ref({ legal_name: '', alias_name: '', business_pan: '', cin: '' });
const docFiles = ref({ incorporation: null, gst_or_pan: null });
const fmtINR = (n) => '₹' + Math.round(Number(n) || 0).toLocaleString('en-IN');
const fmtMin = (n) => (Number(n) || 0).toLocaleString('en-IN');
// Call Credits: a plain number (no ₹) — APEX never shows the rupee symbol on the wallet.
const fmtCredits = (n) => Math.round(Number(n) || 0).toLocaleString('en-IN');
const payBill = computed(() => billFor(payMinutes.value));
const payCredited = computed(() => creditedFor(payMinutes.value));   // ≈ minutes (display)
const payCredits = computed(() => apexCreditFor(payMinutes.value));  // Call Credits granted

// ── affiliate referral (optional code on the payment screen) ──
// Debounced public check → inline valid/invalid indicator. Only a VALID code is
// sent with create-subscription; a typo never blocks payment (server re-checks
// and also ignores unknown codes).
const affiliateCode = ref('');
const affiliateCheck = ref({ state: 'idle', name: '' }); // idle|checking|valid|invalid
let _affiliateTimer = null;
watch(affiliateCode, (raw) => {
  if (_affiliateTimer) clearTimeout(_affiliateTimer);
  const code = (raw || '').trim();
  if (!code) { affiliateCheck.value = { state: 'idle', name: '' }; return; }
  affiliateCheck.value = { state: 'checking', name: '' };
  _affiliateTimer = setTimeout(async () => {
    try {
      const r = await checkAffiliateCode(code.toUpperCase());
      // Ignore stale responses after further typing.
      if ((affiliateCode.value || '').trim() !== code) return;
      affiliateCheck.value = r?.valid
        ? { state: 'valid', name: r.display_name || '' }
        : { state: 'invalid', name: '' };
    } catch {
      if ((affiliateCode.value || '').trim() === code) affiliateCheck.value = { state: 'idle', name: '' };
    }
  }, 400);
});

// ── data ──
const campaigns = ref([]);
const bulkStatus = ref({ plan_eligible: false, enabled: false });
const loadingCampaigns = ref(false);
// APEX is the DETERMINISTIC product — only deterministic campaigns belong here.
const deterministicCampaigns = computed(() => (campaigns.value || []).filter((c) => c.deterministic));

const TABS = [
  { id: 'campaign', label: 'Campaign', is: ApexCampaign },
  { id: 'qualified', label: 'Qualified Leads', is: ApexQualified },
  { id: 'notint', label: 'Not Interested', is: ApexNotInterested },
  { id: 'didnt', label: "Didn't Pick Up", is: ApexNoPickup },
  { id: 'busy', label: 'Busy', is: ApexBusy },
  { id: 'logs', label: 'Call Logs', is: ApexCallLogs },
  { id: 'members', label: 'Members', is: ApexMembers },
];
// Members get a RESTRICTED shell — just the claim pool + their claimed leads.
const MEMBER_TABS = [
  { id: 'available', label: 'Available Leads', is: ApexAvailableLeads },
  { id: 'mine', label: 'My Leads', is: ApexMyLeads },
];
const isMember = computed(() => (user.value?.role || '') === 'member');
const visibleTabs = computed(() => (isMember.value ? MEMBER_TABS : TABS));
const activeTab = computed(() => visibleTabs.value.find((t) => t.id === tab.value) || visibleTabs.value[0]);

// ── toasts — transient action feedback (success/error), auto-dismissed ──
const toasts = ref([]);
let _toastSeq = 0;
function toast(message, type = 'ok', ms = 4200) {
  const id = ++_toastSeq;
  toasts.value = [...toasts.value, { id, message, type }];
  setTimeout(() => { toasts.value = toasts.value.filter((t) => t.id !== id); }, ms);
}

// ── tabs: a single sliding thumb carries the active-pill background ──
const tabsInner = ref(null);
const tabThumb = ref({ x: 0, y: 0, w: 0, h: 0, on: false });
function moveTabThumb() {
  const el = tabsInner.value && tabsInner.value.querySelector('.ax-tab.is-active');
  if (!el) { tabThumb.value = { ...tabThumb.value, on: false }; return; }
  tabThumb.value = { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight, on: true };
}
watch([tab, visibleTabs, screen], () => nextTick(moveTabThumb));
onBeforeUnmount(() => window.removeEventListener('resize', moveTabThumb));

// Per-campaign bucket-count summaries for V2 campaigns (whose rows aren't inlined).
const campaignSummaries = ref({}); // campaign_id -> { total, qualified, not_interested, no_pickup, pending }
function summaryFor(id) { return campaignSummaries.value[String(id)] || null; }

let _ingestPoll = null;
async function reload() {
  loadingCampaigns.value = true;
  try {
    campaigns.value = await fetchCampaigns();
  } catch (e) {
    toast(extractError(e, 'Could not load campaigns.'), 'err');
  } finally {
    loadingCampaigns.value = false;
  }
  // V2 campaigns don't inline their contacts — pull the cheap GROUP BY summary so
  // the campaign rows + tab badges show counts without shipping 1M rows.
  try {
    const v2 = (campaigns.value || []).filter((c) => c.v2 && c.status !== 'ingesting');
    const results = await Promise.all(v2.map((c) => fetchCampaignSummary(c.id).then((s) => [c.id, s]).catch(() => null)));
    const map = { ...campaignSummaries.value };
    for (const r of results) { if (r) map[String(r[0])] = r[1]; }
    campaignSummaries.value = map;
  } catch { /* best-effort */ }
  // While any campaign is still ingesting (async COPY of a large upload), poll so
  // the UI flips to "running" + shows the count without a manual refresh.
  if (_ingestPoll) { clearTimeout(_ingestPoll); _ingestPoll = null; }
  if ((campaigns.value || []).some((c) => c.status === 'ingesting')) {
    _ingestPoll = setTimeout(reload, 4000);
  }
}

// Call Credits wallet: { credits_purchased, credits_used, credits_remaining,
// estimated_minutes_remaining, slab_rate, … }
const wallet = ref(null);
const topupMinutes = ref(1000);
const isToppingUp = ref(false);
const topupNote = ref('');
const topupOk = ref(false);  // true = success note (green), false = error note (red)
// Top-up quick-picks + a live cost/credit preview (mirrors the payment screen's
// breakdown) so the user sees what they get AND what they'll pay before checkout.
const TOPUP_PRESETS = [500, 1000, 2500, 5000];
const MINUTES_MAX = 100000; // per-purchase cap — mirrors the server's _MINUTES_MAX
const fmtKMin = (m) => (m >= 1000 ? `${(m / 1000).toFixed(m % 1000 ? 1 : 0)}K` : `${m}`);
const topupValid = computed(() => {
  const m = Math.floor(Number(topupMinutes.value) || 0);
  return m >= 100 && m <= MINUTES_MAX;
});
const topupBill = computed(() => billFor(topupMinutes.value));            // ₹ charged now
const topupCredits = computed(() => apexCreditFor(topupMinutes.value));   // Call Credits granted
const topupCreditedMin = computed(() => creditedFor(topupMinutes.value)); // ≈ minutes incl. 50% bonus
async function loadBalance() { wallet.value = await fetchMinutesBalance(); }

// ── Plan billing (ENABLE_APEX_PLANS) — plan config, subscription, plan-rate top-up ──
const billing = ref(null);         // null when plan model is off (404) → legacy top-up
const cancelBusy = ref(false);
const cancelNote = ref('');
async function loadBilling() {
  try { billing.value = await fetchApexBilling(); }
  catch { billing.value = null; }  // flag off / not an apex admin → silent, legacy path stays
}
const planActive = computed(() => !!(billing.value && billing.value.plan_code));
const planRate = computed(() => Number(billing.value?.rate_per_minute) || 0);
const planTopupBonus = computed(() => Number(billing.value?.topup_bonus_pct) || 0);
// Plan-rate top-up preview (bills selected × plan rate; credits +topup bonus).
const topupBillPlan = computed(() => Math.round(Math.max(0, Math.floor(Number(topupMinutes.value) || 0)) * planRate.value));
const topupCreditsPlan = computed(() => topupBillPlan.value * (1 + planTopupBonus.value / 100));
const topupCreditedMinPlan = computed(() => planRate.value > 0 ? Math.floor(topupCreditsPlan.value / planRate.value) : 0);
// Display switches: plan-aware when the plan model is on, else the legacy slab values.
const topupBillDisp = computed(() => planActive.value ? topupBillPlan.value : topupBill.value);
const topupCreditsDisp = computed(() => planActive.value ? topupCreditsPlan.value : topupCredits.value);
const topupCreditedMinDisp = computed(() => planActive.value ? topupCreditedMinPlan.value : topupCreditedMin.value);
const topupBonusLabel = computed(() => planActive.value
  ? (planTopupBonus.value > 0 ? `incl. ${planTopupBonus.value}% bonus` : 'no bonus on top-ups')
  : 'incl. 50% bonus');

async function doCancelSubscription() {
  if (cancelBusy.value) return;
  if (!window.confirm('Cancel your subscription at the end of the current billing cycle? Service continues until then.')) return;
  cancelBusy.value = true; cancelNote.value = '';
  try {
    const r = await cancelSubscription();
    cancelNote.value = r.message || 'Cancellation scheduled.';
    await loadBilling();
  } catch (e) {
    cancelNote.value = extractError(e, 'Could not cancel the subscription.');
  } finally {
    cancelBusy.value = false;
  }
}
// Spend meter: how much of the purchased credit pool is left (0..1). Shifts to
// the accent red when it runs low, so a draining wallet is visible at a glance.
const walletPct = computed(() => {
  const bought = Number(wallet.value?.credits_purchased) || 0;
  if (!bought) return 0;
  return Math.max(0, Math.min(1, (Number(wallet.value?.credits_remaining) || 0) / bought));
});

// ── members + qualified-lead claim pool ──
const qualifiedLeads = ref([]);
const myLeads = ref([]);
const loadingLeads = ref(false);
const leadBusy = ref('');   // call_link_id currently being claimed / updated
async function reloadLeads() {
  loadingLeads.value = true;
  try {
    [qualifiedLeads.value, myLeads.value] = await Promise.all([listQualifiedLeads(), listMyLeads()]);
  } catch (e) {
    toast(extractError(e, 'Could not load leads.'), 'err');
  } finally {
    loadingLeads.value = false;
  }
}
async function doClaim(row) {
  if (!row || leadBusy.value) return;
  leadBusy.value = row.call_link_id;
  try {
    await claimLead(row.campaign_id, row.call_link_id);
    toast(`${row.name || 'Lead'} claimed — it's yours now.`);
    await reloadLeads();
  } catch (e) {
    toast(e?.response?.status === 409 ? 'Someone else just claimed that lead.' : extractError(e, 'Could not claim the lead.'), 'err');
    await reloadLeads();
  } finally {
    leadBusy.value = '';
  }
}
async function doSetStatus(row, status) {
  if (!row || leadBusy.value) return;
  leadBusy.value = row.call_link_id;
  try {
    await setLeadStatus(row.campaign_id, row.call_link_id, status);
    await reloadLeads();
  } catch (e) {
    toast(extractError(e, 'Could not update the lead.'), 'err');
  } finally {
    leadBusy.value = '';
  }
}

// Admin: invite members + list them.
const members = ref([]);
const inviteForm = ref({ email: '', full_name: '' });
const inviteBusy = ref(false);
const inviteNote = ref('');
const inviteOk = ref(false);
async function loadMembers() {
  try { members.value = await listMembers(); } catch (_) { /* non-fatal */ }
}
async function submitInvite() {
  const email = (inviteForm.value.email || '').trim();
  if (!email) { inviteNote.value = 'Enter an email to invite.'; inviteOk.value = false; return; }
  inviteBusy.value = true; inviteNote.value = '';
  try {
    await inviteMember(email, inviteForm.value.full_name);
    inviteOk.value = true;
    inviteNote.value = `Invitation sent to ${email}.`;
    inviteForm.value = { email: '', full_name: '' };
    await loadMembers();
  } catch (e) {
    inviteOk.value = false;
    inviteNote.value = extractError(e, 'Could not send the invitation.');
  } finally {
    inviteBusy.value = false;
  }
}

// Invite acceptance — the /nokvo-apex/invite/:token screen.
const inviteToken = ref('');
const inviteContext = ref(null);
const invitePassword = ref('');
const inviteAcceptError = ref('');
async function loadInviteContext() {
  inviteToken.value = String(route.params.token || '');
  screen.value = 'invite';
  if (!inviteToken.value) { inviteAcceptError.value = 'This invitation link is invalid.'; return; }
  try {
    inviteContext.value = await getInviteContext(inviteToken.value);
  } catch (e) {
    inviteContext.value = null;
    inviteAcceptError.value = extractError(e, 'This invitation is invalid or has expired.');
  }
}
async function submitAcceptInvite() {
  inviteAcceptError.value = '';
  if ((invitePassword.value || '').length < 8) {
    inviteAcceptError.value = 'Choose a password (at least 8 characters).';
    return;
  }
  busy.value = true;
  try {
    const data = await acceptInvite(inviteToken.value, invitePassword.value);
    await enterApp(data.user);
  } catch (e) {
    inviteAcceptError.value = extractError(e, 'Could not accept the invitation.');
  } finally {
    busy.value = false;
  }
}

async function enterApp(u) {
  user.value = u || user.value;
  // HARD onboarding gate — EVERY entry path funnels through here, so an org that
  // hasn't finished onboarding can NEVER reach the dashboard (even after payment,
  // and even via refresh / deep-link). Trust an explicit onboarding_required on the
  // passed object; otherwise confirm against /apex/me (authoritative).
  let required = u?.onboarding_required;
  let step = u?.onboarding_step;
  if (required === undefined) {
    try {
      const me = await fetchMe();
      user.value = me;
      required = !!me.onboarding_required;
      step = me.onboarding_step;
    } catch { required = false; }
  }
  if (required) {
    onboardingStep.value = step || 'business_details';
    screen.value = 'onboarding';
    return;
  }
  screen.value = 'app';
  // Make sure we know the role before choosing the view (member → restricted leads
  // pool; admin → the full engine). The session user may omit it; /apex/me has it.
  if (!user.value?.role) {
    try { user.value = await fetchMe(); } catch { /* keep what we have */ }
  }
  if (isMember.value) {
    tab.value = 'available';
    await reloadLeads();
  } else {
    bulkStatus.value = await fetchBulkStatus();
    await Promise.all([reload(), loadBalance(), loadBilling()]);
  }
}

function setTopupNote(msg, ok) { topupNote.value = msg; topupOk.value = ok; }
async function startTopup() {
  topupNote.value = '';
  const minutes = Math.floor(Number(topupMinutes.value) || 0);
  if (minutes < 100) { setTopupNote('Enter at least 100 minutes.', false); return; }
  if (minutes > MINUTES_MAX) { setTopupNote(`Maximum ${MINUTES_MAX.toLocaleString('en-IN')} minutes per purchase.`, false); return; }
  isToppingUp.value = true;
  try {
    const Rzp = await loadRazorpay();
    // Plan model on → plan-rate top-up (bills at the plan ₹/min, credits the plan bonus);
    // else the legacy slab top-up.
    const usePlan = planActive.value;
    const data = usePlan ? await planTopupCreateOrder(minutes) : await topupCreateOrder(minutes);
    const rzp = new Rzp({
      key: data.key_id, order_id: data.order_id, amount: data.amount_paise, currency: 'INR',
      name: data.name, description: data.description, theme: { color: '#E62630' },
      handler: async (resp) => {
        try {
          if (usePlan) {
            const vr = await planTopupVerify({
              minutes,
              razorpay_order_id: resp.razorpay_order_id,
              razorpay_payment_id: resp.razorpay_payment_id,
              razorpay_signature: resp.razorpay_signature,
            });
            await Promise.all([loadBalance(), loadBilling()]);
            setTopupNote(`Added ${fmtCredits(vr.credits_added)} Call Credits.`, true);
          } else {
            await topupVerify(minutes, resp);
            await loadBalance();
            setTopupNote(`Added ${fmtCredits(apexCreditFor(minutes))} Call Credits (≈${fmtMin(creditedFor(minutes))} min, incl. 50% bonus).`, true);
          }
        } catch (e) { setTopupNote(extractError(e, 'Top-up verification failed.'), false); }
        finally { isToppingUp.value = false; }
      },
      modal: { ondismiss: () => { isToppingUp.value = false; } },
    });
    rzp.on('payment.failed', (r) => { setTopupNote(r?.error?.description || 'Payment failed.', false); isToppingUp.value = false; });
    rzp.open();
  } catch (e) { setTopupNote(extractError(e, 'Could not start top-up.'), false); isToppingUp.value = false; }
}

// ── Nova (the in-product assistant) ──
const novaOpen = ref(false);
// ONE-WAY handoff of a Nova-drafted campaign into the Campaign form: set here,
// consumed (and cleared) by ApexCampaign on mount/watch. Nova never reads the
// form back — manual edits stay invisible to the chat by design.
const novaDraft = ref(null);
function onNovaApplyDraft(draft) {
  novaDraft.value = draft || null;
  novaOpen.value = false;
  tab.value = 'campaign';
}

// ── Feedback / Suggest a feature ──
const feedbackOpen = ref(false);
const feedbackForm = ref({ message: '', category: 'feedback' });
const feedbackBusy = ref(false);
const feedbackNote = ref('');
const feedbackSent = ref(false);
function openFeedback() {
  feedbackForm.value = { message: '', category: 'feedback' };
  feedbackNote.value = '';
  feedbackSent.value = false;
  feedbackOpen.value = true;
}
async function sendFeedback() {
  const msg = (feedbackForm.value.message || '').trim();
  if (!msg) { feedbackNote.value = 'Please type your feedback first.'; return; }
  feedbackBusy.value = true; feedbackNote.value = '';
  try {
    await apiSubmitFeedback(msg, feedbackForm.value.category);
    feedbackSent.value = true;
  } catch (e) {
    feedbackNote.value = extractError(e, 'Could not send feedback. Please try again.');
  } finally {
    feedbackBusy.value = false;
  }
}

// Route any auth result (login/signup/google) to the right next screen.
async function routeAuthResult(res) {
  if (res.kind === 'mfa') {
    mfaTempToken.value = res.tempToken;
    mfaDigits.value = ['', '', '', '', '', ''];
    screen.value = 'mfa';
  } else if (res.kind === 'payment') {
    paymentToken.value = res.paymentToken;
    screen.value = 'payment';
  } else if (res.kind === 'onboarding') {
    user.value = res.user || user.value;
    onboardingStep.value = res.step || 'business_details';
    screen.value = 'onboarding';
  } else if (res.kind === 'pending_activation') {
    // Paid, awaiting SuperAdmin activation — not usable yet. Show a clear message.
    errorMsg.value = res.message || 'Payment received — your account is being activated (within 6 hours).';
  } else {
    await enterApp(res.user);
  }
}

async function doLogin() {
  errorMsg.value = '';
  if (!form.value.email.trim() || !form.value.password) {
    errorMsg.value = 'Enter your email and password.';
    return;
  }
  busy.value = true;
  try {
    await routeAuthResult(await apexLogin(form.value.email.trim(), form.value.password));
  } catch (e) {
    errorMsg.value = extractError(e, 'Sign in failed.');
  } finally {
    busy.value = false;
  }
}

async function doRequestAccess() {
  errorMsg.value = '';
  const f = requestForm.value;
  const email = f.email.trim();
  if (!f.company_name.trim() || !email) {
    errorMsg.value = 'Please enter your company name and email.';
    return;
  }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    errorMsg.value = 'Please enter a valid email address.';
    return;
  }
  if (f.phone && !/^[+0-9()\-\s]{6,20}$/.test(f.phone.trim())) {
    errorMsg.value = 'Please enter a valid phone number.';
    return;
  }
  busy.value = true;
  try {
    await apexRequestAccess({
      company_name: f.company_name.trim(),
      contact_name: f.contact_name.trim() || null,
      email,
      phone: f.phone.trim() || null,
      requested_plan: f.requested_plan || null,
      notes: f.notes.trim() || null,
    });
    requestSubmitted.value = true;
  } catch (e) {
    errorMsg.value = extractError(e, 'Could not submit your request. Please try again.');
  } finally {
    busy.value = false;
  }
}

async function onGoogleCredential(resp) {
  errorMsg.value = '';
  busy.value = true;
  try {
    await routeAuthResult(await apexGoogleLogin(resp.credential));
  } catch (e) {
    errorMsg.value = extractError(e, 'Google sign-in failed.');
  } finally {
    busy.value = false;
  }
}

async function initGoogle() {
  googleState.value = 'loading';
  try {
    if (!googleClientId.value) {
      const cfg = await fetchConfig();
      googleClientId.value = cfg.google_client_id || '';
    }
    if (!googleClientId.value) {
      console.warn('[APEX] Google sign-in hidden: /config returned no google_client_id (set GOOGLE_OAUTH_CLIENT_ID).');
      googleState.value = 'error';
      return;
    }
    const g = await loadGsi();
    if (!g?.accounts?.id) { googleState.value = 'error'; return; }
    g.accounts.id.initialize({
      client_id: googleClientId.value,
      callback: onGoogleCredential,
      ux_mode: 'popup',
      auto_select: false,
    });
    // The host belongs to whichever auth screen is mounted — wait a tick so it
    // exists in the DOM (covers both the onMounted call and the screen-change watch).
    await nextTick();
    const host = document.getElementById('apex-google-btn');
    if (!host) { googleState.value = 'error'; return; }
    host.innerHTML = '';
    g.accounts.id.renderButton(host, { theme: 'filled_black', size: 'large', width: 320, text: 'continue_with' });
    // GSI injects the button iframe async; if it's still empty shortly after, the
    // render was rejected (almost always: this origin isn't authorized for the
    // OAuth client). Flip to the visible fallback instead of leaving a blank gap.
    googleState.value = 'ready';
    window.setTimeout(() => {
      const h = document.getElementById('apex-google-btn');
      if (h && !h.firstElementChild) googleState.value = 'error';
    }, 1600);
  } catch (e) {
    // Surface GSI failures (e.g. "origin not allowed for this client id") instead
    // of silently rendering nothing — the #1 cause of a missing Google button.
    console.error('[APEX] Google sign-in failed to initialize:', e);
    googleState.value = 'error';
  }
}

function retryGoogle() {
  googleClientId.value = '';
  initGoogle();
}

// ── payment ──
async function startPayment() {
  errorMsg.value = '';
  // Hard gate: no payment without accepting the Terms & Privacy Policy.
  if (!termsAccepted.value) {
    errorMsg.value = 'Please read and accept the Terms & Conditions and Privacy Policy to continue.';
    return;
  }
  const minutes = Math.floor(Number(payMinutes.value) || 0);
  if (minutes < 100) { errorMsg.value = 'Enter at least 100 minutes.'; return; }
  if (minutes > MINUTES_MAX) { errorMsg.value = `Maximum ${MINUTES_MAX.toLocaleString('en-IN')} minutes per purchase.`; return; }
  busy.value = true;
  try {
    const Rzp = await loadRazorpay();
    const data = await createSubscription(
      paymentToken.value,
      minutes,
      {
        termsAccepted: termsAccepted.value,
        termsVersion: APEX_LEGAL_VERSIONS.terms,
        privacyVersion: APEX_LEGAL_VERSIONS.privacy,
      },
      affiliateCheck.value.state === 'valid' ? affiliateCode.value.trim().toUpperCase() : null,
    );
    const rzp = new Rzp({
      key: data.key_id,
      subscription_id: data.subscription_id,
      name: data.name,
      description: data.description,
      theme: { color: '#E62630' },
      handler: async (resp) => {
        try {
          const vr = await verifyPayment(paymentToken.value, resp);
          if (vr.session?.access_token) { /* token persisted server-side flow */ }
          // verify provisions + credits; resume into onboarding.
          onboardingStep.value = vr.onboarding_step || 'business_details';
          // The verify response carries a session token under .session or top-level.
          const tok = vr.access_token || vr.session?.access_token;
          if (tok) localStorage.setItem('nokvo_apex_access_token', tok);
          screen.value = 'onboarding';
        } catch (e) {
          errorMsg.value = extractError(e, 'Payment verification failed. If charged, it will be reconciled.');
        } finally { busy.value = false; }
      },
      modal: { ondismiss: () => { busy.value = false; } },
    });
    rzp.on('payment.failed', (r) => { errorMsg.value = r?.error?.description || 'Payment failed.'; busy.value = false; });
    rzp.open();
  } catch (e) {
    errorMsg.value = extractError(e, 'Could not start payment.');
    busy.value = false;
  }
}

// ── onboarding (business details → documents → dashboard) ──
async function submitBusiness() {
  errorMsg.value = '';
  if (!bizForm.value.legal_name.trim()) { errorMsg.value = 'Company legal name is required.'; return; }
  busy.value = true;
  try {
    const r = await saveBusinessDetails({
      legal_name: bizForm.value.legal_name.trim(),
      alias_name: bizForm.value.alias_name.trim() || null,
      business_pan: bizForm.value.business_pan.trim() || null,
      cin: bizForm.value.cin.trim() || null,
    });
    onboardingStep.value = r.onboarding_step || 'documents';
  } catch (e) {
    errorMsg.value = extractError(e, 'Could not save business details.');
  } finally { busy.value = false; }
}
async function submitDocuments() {
  errorMsg.value = '';
  if (!docFiles.value.incorporation || !docFiles.value.gst_or_pan) {
    errorMsg.value = 'Upload both documents.'; return;
  }
  busy.value = true;
  try {
    const r = await uploadDocuments(docFiles.value.incorporation, docFiles.value.gst_or_pan);
    if ((r.onboarding_step || 'done') === 'done') {
      await enterApp(user.value);
    } else {
      onboardingStep.value = r.onboarding_step;
    }
  } catch (e) {
    errorMsg.value = extractError(e, 'Could not upload documents.');
  } finally { busy.value = false; }
}
function onDocPick(kind, ev) { docFiles.value[kind] = ev?.target?.files?.[0] || null; }

function onMfaInput(i, ev) {
  const v = (ev.target.value || '').replace(/\D/g, '').slice(-1);
  mfaDigits.value[i] = v;
  if (v && i < 5) {
    const next = document.getElementById(`ax-mfa-${i + 1}`);
    if (next) next.focus();
  }
}
function onMfaKey(i, ev) {
  if (ev.key === 'Backspace' && !mfaDigits.value[i] && i > 0) {
    const prev = document.getElementById(`ax-mfa-${i - 1}`);
    if (prev) prev.focus();
  }
}
function onMfaPaste(ev) {
  const txt = (ev.clipboardData?.getData('text') || '').replace(/\D/g, '').slice(0, 6);
  if (!txt) return;
  ev.preventDefault();
  for (let i = 0; i < 6; i++) mfaDigits.value[i] = txt[i] || '';
  const last = document.getElementById(`ax-mfa-${Math.min(txt.length, 6) - 1}`);
  if (last) last.focus();
}
async function doVerify() {
  errorMsg.value = '';
  const code = mfaDigits.value.join('');
  if (code.length !== 6) {
    errorMsg.value = 'Enter the 6-digit code.';
    return;
  }
  busy.value = true;
  try {
    const u = await verifyTotp(mfaTempToken.value, code);
    await enterApp(u);
  } catch (e) {
    errorMsg.value = extractError(e, 'Invalid code.');
  } finally {
    busy.value = false;
  }
}

function signOut() {
  clearToken();
  user.value = null;
  form.value = { email: '', password: '' };
  screen.value = 'login';
  tab.value = 'campaign';
}
function backToLogin() {
  screen.value = 'login';
  errorMsg.value = '';
}
// APEX onboarding escape hatch → back to the sign in / sign up screens. Unlike the
// MFA back (no full token yet), an onboarding user holds a real session token (the
// KYC steps are authenticated), so we clear it for a clean auth screen. Onboarding
// progress is server-side (onboarding_step), so signing in again resumes at the next
// step — nothing is lost.
function backToAuthFromOnboarding() {
  signOut();
}

// Share APEX context with tab components.
provide('apex', {
  user,
  campaigns,
  deterministicCampaigns,
  bulkStatus,
  loadingCampaigns,
  wallet,
  reload,
  createCampaign: apiCreateCampaign,
  translateQuestionnaire: apiTranslateQuestionnaire,
  styleRewriteQuestionnaire: apiStyleRewriteQuestionnaire,
  rerunCampaign,
  resumeCampaign,
  updateCampaign,
  addCampaignContacts,
  cancelCampaign,
  deleteCampaign,
  fetchCampaignSummary,
  fetchCampaignContacts,
  downloadCampaignContactsCsv,
  fetchCampaignApiConfig,
  setCampaignApiConfig,
  revealCampaignApiKey,
  rotateCampaignApiKey,
  testCampaignWebhook,
  campaignSummaries,
  summaryFor,
  fetchTranscript,
  extractError,
  setTab: (id) => { tab.value = id; },
  novaDraft,
  clearNovaDraft: () => { novaDraft.value = null; },
  // members + qualified-lead claim pool
  isMember,
  qualifiedLeads,
  myLeads,
  loadingLeads,
  leadBusy,
  reloadLeads,
  claim: doClaim,
  setStatus: doSetStatus,
  members,
  loadMembers,
  inviteForm,
  inviteBusy,
  inviteNote,
  inviteOk,
  submitInvite,
  toast,
});

onMounted(async () => {
  // The tab thumb tracks the active button's box — re-measure on resize and
  // once the webfonts land (font swap changes button widths).
  window.addEventListener('resize', moveTabThumb);
  if (document.fonts?.ready) document.fonts.ready.then(() => moveTabThumb());
  // Load the Sora + JetBrains Mono fonts once (idempotent).
  if (!document.getElementById('apex-fonts')) {
    const l = document.createElement('link');
    l.id = 'apex-fonts';
    l.rel = 'stylesheet';
    l.href = 'https://fonts.googleapis.com/css2?family=Sora:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap';
    document.head.appendChild(l);
  }
  // Member invite acceptance — show the accept screen even if a stale token exists.
  if (props.initialAuthState === 'invite') {
    await loadInviteContext();
    return;
  }
  // Deep-link / refresh: if an APEX token exists, resume straight into the app.
  if (props.initialAuthState === 'ready' || getToken()) {
    try {
      const me = await fetchMe();
      await enterApp(me);
      return;
    } catch {
      clearToken();
    }
  }
  screen.value = props.initialAuthState === 'signup' ? 'signup' : 'login';
  if (screen.value === 'signup') loadApexPlans();
  initGoogle();
});

// Re-render the Google button whenever an auth screen (with the host) shows.
watch(screen, async (s) => {
  if (s === 'login' || s === 'signup') {
    await nextTick();
    initGoogle();
  }
});
</script>

<template>
  <div class="ax-root">
    <!-- ============ LOGIN ============ -->
    <div v-if="screen === 'login'" class="ax-auth">
      <div class="ax-glow"></div>
      <div class="ax-grid"></div>
      <div class="ax-auth-card ax-fade">
        <div class="ax-brand-stack">
          <img :src="nokvoMark" class="ax-brand-img ax-brand-img--lg" alt="NOKVO" />
          <div class="ax-brand-name">NOKVO</div>
          <div class="ax-brand-apex"><span class="ax-rule"></span><span class="ax-apex-text">APEX</span><span class="ax-rule"></span></div>
        </div>
        <div class="ax-form">
          <div class="ax-eyebrow">Welcome back</div>
          <h1 class="ax-h1">Sign in to your workspace</h1>
          <label class="ax-label">Email</label>
          <input v-model="form.email" type="email" class="ax-input" autocomplete="username" @keyup.enter="doLogin" />
          <div class="ax-label-row">
            <label class="ax-label ax-label--inline">Password</label>
            <span class="ax-forgot">Forgot?</span>
          </div>
          <input v-model="form.password" type="password" class="ax-input" autocomplete="current-password" @keyup.enter="doLogin" />
          <p v-if="errorMsg" class="ax-error">{{ errorMsg }}</p>
          <button type="button" class="ax-btn ax-btn--accent ax-btn--full" :disabled="busy" @click="doLogin">
            {{ busy ? 'Signing in…' : 'Continue' }}
          </button>
          <div class="ax-divider"><span></span><span class="ax-divider-text">OR</span><span></span></div>
          <div v-show="googleState !== 'error'" id="apex-google-btn" class="ax-google"></div>
          <div v-if="googleState === 'error'" class="ax-google-fallback">
            <span>Google sign-in isn't loading here — this site's origin may not be authorized for the OAuth client.</span>
            <button type="button" class="ax-link" @click="retryGoogle">Retry</button>
          </div>
          <p class="ax-sub-cta">New to NOKVO APEX? <span class="ax-link" @click="screen = 'signup'; errorMsg = ''; loadApexPlans()">Request access</span></p>
          <p class="ax-sub-cta ax-affiliate-cta">Want to earn by referring businesses? <span class="ax-link" @click="router.push('/affiliate')">Join the NOKVO Affiliate Program</span></p>
        </div>
      </div>
    </div>

    <!-- ============ REQUEST ACCESS (replaces self-serve signup) ============ -->
    <div v-else-if="screen === 'signup'" class="ax-auth">
      <div class="ax-glow"></div>
      <div class="ax-grid"></div>
      <div class="ax-auth-card ax-fade">
        <div class="ax-brand-stack">
          <img :src="nokvoMark" class="ax-brand-img ax-brand-img--lg" alt="NOKVO" />
          <div class="ax-brand-name">NOKVO</div>
          <div class="ax-brand-apex"><span class="ax-rule"></span><span class="ax-apex-text">APEX</span><span class="ax-rule"></span></div>
        </div>

        <!-- success state -->
        <div v-if="requestSubmitted" class="ax-form" style="text-align:center;">
          <div class="ax-eyebrow">Request received</div>
          <h1 class="ax-h1">We'll be in touch</h1>
          <p class="ax-muted" style="margin:10px 0 22px;">
            Thanks — our team will review your request and reach out shortly to set up your
            NOKVO APEX account. Once your account is created and payment is received, it's
            activated within 6 hours (24 hours for Free Trial).
          </p>
          <button type="button" class="ax-btn ax-btn--accent ax-btn--full" @click="screen = 'login'; requestSubmitted = false; errorMsg = ''">
            Back to sign in
          </button>
        </div>

        <!-- request form -->
        <div v-else class="ax-form">
          <div class="ax-eyebrow">Get started</div>
          <h1 class="ax-h1">Request access</h1>
          <p class="ax-muted" style="margin:2px 0 16px;font-size:13px;">
            NOKVO APEX is set up for you by our team — tell us about your business and we'll reach out.
          </p>
          <label class="ax-label">Company name</label>
          <input v-model="requestForm.company_name" type="text" class="ax-input" placeholder="Raghava Estates" />
          <label class="ax-label" style="margin-top:16px;">Your name</label>
          <input v-model="requestForm.contact_name" type="text" class="ax-input" placeholder="Preeth" />
          <label class="ax-label" style="margin-top:16px;">Work email</label>
          <input v-model="requestForm.email" type="email" class="ax-input" autocomplete="email" @keyup.enter="doRequestAccess" />
          <label class="ax-label" style="margin-top:16px;">Phone <span style="opacity:.5;font-weight:400;">(optional)</span></label>
          <input v-model="requestForm.phone" type="tel" class="ax-input" placeholder="+91 98765 43210" />
          <label class="ax-label" style="margin-top:16px;">Plan you're interested in <span style="opacity:.5;font-weight:400;">(optional)</span></label>
          <select v-model="requestForm.requested_plan" class="ax-input">
            <option value="">Not sure yet</option>
            <option v-for="p in apexPlans" :key="p.code" :value="p.code">
              {{ p.label }}<template v-if="p.monthly_inr"> — ₹{{ Math.round(p.monthly_inr).toLocaleString('en-IN') }}/mo</template>
            </option>
          </select>
          <label class="ax-label" style="margin-top:16px;">Anything else? <span style="opacity:.5;font-weight:400;">(optional)</span></label>
          <textarea v-model="requestForm.notes" class="ax-input" rows="2" placeholder="Tell us about your calling needs…"></textarea>
          <p v-if="errorMsg" class="ax-error">{{ errorMsg }}</p>
          <button type="button" class="ax-btn ax-btn--accent ax-btn--full" :disabled="busy" @click="doRequestAccess">
            {{ busy ? 'Submitting…' : 'Request access' }}
          </button>
          <p class="ax-sub-cta">Already have an account? <span class="ax-link" @click="screen = 'login'; errorMsg = ''">Sign in</span></p>
          <p class="ax-sub-cta ax-affiliate-cta">Want to earn by referring businesses? <span class="ax-link" @click="router.push('/affiliate')">Join the NOKVO Affiliate Program</span></p>
        </div>
      </div>
    </div>

    <!-- ============ MFA ============ -->
    <div v-else-if="screen === 'mfa'" class="ax-auth">
      <div class="ax-glow"></div>
      <div class="ax-auth-card ax-auth-card--center ax-fade">
        <div class="ax-brand-inline">
          <div class="ax-mark ax-mark--sm"></div>
          <span class="ax-brand-name ax-brand-name--sm">NOKVO</span>
          <span class="ax-apex-text ax-apex-text--sm">APEX</span>
        </div>
        <div class="ax-eyebrow">Two-factor</div>
        <h1 class="ax-h1">Enter your code</h1>
        <p class="ax-mfa-hint">Enter the 6-digit code from your authenticator app.</p>
        <div class="ax-mfa-row" :class="{ 'is-shake': mfaShake }" @paste="onMfaPaste">
          <input
            v-for="(d, i) in mfaDigits" :key="i" :id="`ax-mfa-${i}`"
            class="ax-mfa-box" :class="{ 'is-filled': d }" inputmode="numeric" maxlength="1"
            :value="d" @input="onMfaInput(i, $event)" @keydown="onMfaKey(i, $event)"
          />
        </div>
        <p v-if="errorMsg" class="ax-error ax-error--center">{{ errorMsg }}</p>
        <button type="button" class="ax-btn ax-btn--accent ax-btn--full" :disabled="busy" @click="doVerify">
          {{ busy ? 'Verifying…' : 'Verify & continue' }}
        </button>
        <div class="ax-back"><span class="ax-link" @click="backToLogin">← Back to sign in</span></div>
      </div>
    </div>

    <!-- ============ PAYMENT ============ -->
    <div v-else-if="screen === 'payment'" class="ax-auth">
      <div class="ax-glow"></div>
      <div class="ax-auth-card ax-fade" style="max-width:440px;">
        <div class="ax-brand-inline" style="justify-content:center;">
          <div class="ax-mark ax-mark--sm"></div>
          <span class="ax-brand-name ax-brand-name--sm">NOKVO</span>
          <span class="ax-apex-text ax-apex-text--sm">APEX</span>
        </div>
        <div class="ax-eyebrow" style="text-align:center;">Subscribe</div>
        <h1 class="ax-h1" style="text-align:center;">Activate your workspace</h1>
        <div class="ax-pay-plan">
          <div><strong>NOKVO APEX</strong><span>Deterministic outbound · scored campaigns</span></div>
          <div class="ax-pay-price">{{ fmtINR(PLATFORM_FEE) }}<small>/mo</small></div>
        </div>
        <label class="ax-label" style="margin-top:22px;">Prepaid voice minutes</label>
        <input v-model.number="payMinutes" type="number" min="100" :max="MINUTES_MAX" step="100" class="ax-input" inputmode="numeric" />
        <div class="ax-pay-credit">You're credited <strong>{{ fmtCredits(payCredits) }}</strong> Call Credits — that's {{ fmtMin(payMinutes) }} min + 50% bonus (≈{{ fmtMin(payCredited) }} min), at {{ fmtINR(slabRate(payMinutes)) }}/min.</div>
        <div class="ax-pay-summary">
          <div class="ax-pay-row"><span>Platform fee</span><span>{{ fmtINR(PLATFORM_FEE) }}/mo</span></div>
          <div class="ax-pay-row"><span>{{ fmtMin(payMinutes) }} minutes (one-time)</span><span>{{ fmtINR(payBill) }}</span></div>
          <div class="ax-pay-row ax-pay-row--total"><span>Pay now</span><span>{{ fmtINR(PLATFORM_FEE + payBill) }}</span></div>
        </div>
        <label class="ax-label" style="margin-top:18px;">Affiliate code <span style="opacity:.5;font-weight:400;">(optional)</span></label>
        <input v-model="affiliateCode" type="text" class="ax-input" placeholder="e.g. NKV7XQ2MRT" autocapitalize="characters" spellcheck="false" style="text-transform:uppercase;" />
        <p v-if="affiliateCheck.state === 'checking'" class="ax-muted" style="margin:6px 0 0;font-size:12.5px;">Checking code…</p>
        <p v-else-if="affiliateCheck.state === 'valid'" style="margin:6px 0 0;font-size:12.5px;color:#7FD9A8;">✓ Referred by {{ affiliateCheck.name || 'a NOKVO affiliate' }}</p>
        <p v-else-if="affiliateCheck.state === 'invalid'" class="ax-muted" style="margin:6px 0 0;font-size:12.5px;">Code not found — you can still continue without it.</p>
        <label class="ax-terms">
          <input type="checkbox" v-model="termsAccepted" class="ax-terms-box" />
          <span>I have read and agree to the
            <span class="ax-link" @click.stop.prevent="legalModal = 'terms'">Terms &amp; Conditions</span>
            and
            <span class="ax-link" @click.stop.prevent="legalModal = 'privacy'">Privacy Policy</span>,
            including that all fees are non-refundable.</span>
        </label>
        <p v-if="errorMsg" class="ax-error ax-error--center">{{ errorMsg }}</p>
        <button type="button" class="ax-btn ax-btn--accent ax-btn--full" :disabled="busy || !termsAccepted" @click="startPayment">
          {{ busy ? 'Opening checkout…' : `Pay ${fmtINR(PLATFORM_FEE + payBill)}` }}
        </button>
        <div class="ax-back"><span class="ax-link" @click="backToLogin">← Back to sign in / sign up</span></div>
      </div>
    </div>

    <!-- ============ ONBOARDING ============ -->
    <div v-else-if="screen === 'onboarding'" class="ax-auth">
      <div class="ax-glow"></div>
      <div class="ax-auth-card ax-fade" style="max-width:460px;">
        <div class="ax-brand-inline" style="justify-content:center;">
          <div class="ax-mark ax-mark--sm"></div>
          <span class="ax-brand-name ax-brand-name--sm">NOKVO</span>
          <span class="ax-apex-text ax-apex-text--sm">APEX</span>
        </div>
        <div class="ax-onb-steps">
          <span :class="{ 'is-on': onboardingStep === 'business_details' }">1 · Business</span>
          <span :class="{ 'is-on': onboardingStep === 'documents' }">2 · Documents</span>
        </div>

        <template v-if="onboardingStep === 'business_details'">
          <h1 class="ax-h1" style="text-align:center;">Business details</h1>
          <label class="ax-label">Company legal name</label>
          <input v-model="bizForm.legal_name" type="text" class="ax-input" />
          <label class="ax-label" style="margin-top:16px;">Trading / brand name <span style="opacity:.5;">(optional)</span></label>
          <input v-model="bizForm.alias_name" type="text" class="ax-input" />
          <label class="ax-label" style="margin-top:16px;">Business PAN <span style="opacity:.5;">(optional)</span></label>
          <input v-model="bizForm.business_pan" type="text" class="ax-input" />
          <label class="ax-label" style="margin-top:16px;">CIN <span style="opacity:.5;">(optional)</span></label>
          <input v-model="bizForm.cin" type="text" class="ax-input" />
          <p v-if="errorMsg" class="ax-error">{{ errorMsg }}</p>
          <button type="button" class="ax-btn ax-btn--accent ax-btn--full" :disabled="busy" @click="submitBusiness">
            {{ busy ? 'Saving…' : 'Continue' }}
          </button>
        </template>

        <template v-else>
          <h1 class="ax-h1" style="text-align:center;">Documents</h1>
          <p class="ax-mfa-hint" style="text-align:center;">Upload your incorporation certificate and GST/PAN — we provision your dedicated calling number from these.</p>
          <label class="ax-label">Certificate of incorporation</label>
          <input type="file" class="ax-input" @change="onDocPick('incorporation', $event)" />
          <label class="ax-label" style="margin-top:16px;">GST or PAN</label>
          <input type="file" class="ax-input" @change="onDocPick('gst_or_pan', $event)" />
          <p v-if="errorMsg" class="ax-error">{{ errorMsg }}</p>
          <button type="button" class="ax-btn ax-btn--accent ax-btn--full" :disabled="busy" @click="submitDocuments">
            {{ busy ? 'Submitting…' : 'Finish setup' }}
          </button>
        </template>

        <div class="ax-back" style="margin-top:18px;">
          <span class="ax-link" @click="backToAuthFromOnboarding">← Back to sign in / sign up</span>
        </div>
      </div>
    </div>

    <!-- ============ INVITE ACCEPT ============ -->
    <div v-else-if="screen === 'invite'" class="ax-auth">
      <div class="ax-glow"></div>
      <div class="ax-auth-card ax-fade" style="max-width:440px;">
        <div class="ax-brand-inline" style="justify-content:center;">
          <img :src="nokvoMark" class="ax-brand-img" alt="NOKVO" />
          <span class="ax-brand-name ax-brand-name--sm">NOKVO</span>
          <span class="ax-apex-text ax-apex-text--sm">APEX</span>
        </div>
        <template v-if="inviteContext">
          <div class="ax-eyebrow" style="text-align:center;">Team invitation</div>
          <h1 class="ax-h1" style="text-align:center;">Join {{ inviteContext.organization_name }}</h1>
          <p class="ax-mfa-hint" style="text-align:center;">Set a password for <strong>{{ inviteContext.email }}</strong> to start claiming leads.</p>
          <label class="ax-label">Password</label>
          <input v-model="invitePassword" type="password" class="ax-input" placeholder="At least 8 characters" @keyup.enter="submitAcceptInvite" />
          <p v-if="inviteAcceptError" class="ax-error">{{ inviteAcceptError }}</p>
          <button type="button" class="ax-btn ax-btn--accent ax-btn--full" :disabled="busy" @click="submitAcceptInvite">
            {{ busy ? 'Setting up…' : 'Accept & continue' }}
          </button>
        </template>
        <template v-else>
          <h1 class="ax-h1" style="text-align:center;">Invitation</h1>
          <p class="ax-error ax-error--center">{{ inviteAcceptError || 'This invitation is invalid or has expired.' }}</p>
          <div class="ax-back"><span class="ax-link" @click="backToLogin">← Back to sign in</span></div>
        </template>
      </div>
    </div>

    <!-- ============ APP ============ -->
    <div v-else>
      <header class="ax-header">
        <div class="ax-header-inner ax-cas" style="--d:0ms;">
          <div class="ax-brand-inline">
            <img :src="nokvoMark" class="ax-brand-img" alt="NOKVO" />
            <span class="ax-brand-name ax-brand-name--sm">NOKVO</span>
            <span class="ax-apex-text ax-apex-text--sm">APEX</span>
          </div>
          <div class="ax-header-right">
            <div class="ax-user">
              <div class="ax-avatar">{{ (user?.name || user?.email || 'A').slice(0, 1).toUpperCase() }}</div>
              <div class="ax-user-meta">
                <div class="ax-user-name">{{ user?.name || user?.email || 'Account' }}</div>
                <div class="ax-user-role">{{ (user?.role || 'member').toUpperCase() }}</div>
              </div>
            </div>
            <button type="button" class="ax-btn ax-btn--nova" @click="novaOpen = true"><span class="ax-nova-star"><AxIcon name="sparkles" :size="13" filled /></span>NOVA</button>
            <button type="button" class="ax-btn ax-btn--ghost" @click="openFeedback">Feedback</button>
            <button type="button" class="ax-btn ax-btn--ghost" @click="signOut">Sign out</button>
          </div>
        </div>
      </header>

      <!-- Nova — the in-product assistant -->
      <Transition name="nova">
        <NovaPanel v-if="novaOpen" :role="user?.role || 'member'" @close="novaOpen = false" @apply-draft="onNovaApplyDraft" />
      </Transition>

      <!-- Feedback / Suggest a feature -->
      <Transition name="axmodal">
      <div v-if="feedbackOpen" class="ax-modal-overlay" @click.self="feedbackOpen = false">
        <div class="ax-modal">
          <template v-if="feedbackSent">
            <div class="ax-eyebrow">Thank you</div>
            <h2 class="ax-modal-title">Feedback sent</h2>
            <p class="ax-page-sub" style="margin:0 0 18px;">Thanks — your note has reached our team. We read every one.</p>
            <div class="ax-fb-actions">
              <button type="button" class="ax-btn ax-btn--accent" @click="feedbackOpen = false">Done</button>
            </div>
          </template>
          <template v-else>
            <div class="ax-eyebrow">We're listening</div>
            <h2 class="ax-modal-title">Feedback / Suggest a feature</h2>
            <div class="ax-fb-cats">
              <button type="button" class="ax-fb-cat" :class="{ 'is-on': feedbackForm.category === 'feedback' }" @click="feedbackForm.category = 'feedback'">Feedback</button>
              <button type="button" class="ax-fb-cat" :class="{ 'is-on': feedbackForm.category === 'feature' }" @click="feedbackForm.category = 'feature'">Feature request</button>
            </div>
            <textarea
              v-model="feedbackForm.message" class="ax-input ax-fb-textarea" rows="5"
              placeholder="Tell us what's working, what's not, or what you'd like to see…"
            ></textarea>
            <p v-if="feedbackNote" class="ax-error">{{ feedbackNote }}</p>
            <div class="ax-fb-actions">
              <button type="button" class="ax-btn ax-btn--ghost" :disabled="feedbackBusy" @click="feedbackOpen = false">Cancel</button>
              <button type="button" class="ax-btn ax-btn--accent" :disabled="feedbackBusy" @click="sendFeedback">{{ feedbackBusy ? 'Sending…' : 'Send' }}</button>
            </div>
          </template>
        </div>
      </div>
      </Transition>

      <main class="ax-main">
        <div class="ax-page-head ax-page-head--row">
          <div>
            <div class="ax-eyebrow ax-cas" style="--d:60ms;">{{ isMember ? 'Leads' : 'Outbound' }}</div>
            <h1 class="ax-title ax-cas--blur" style="--d:120ms;">{{ isMember ? 'Your leads' : 'Apex Engine' }}</h1>
            <p class="ax-page-sub ax-cas" style="--d:180ms;">{{ isMember ? 'Claim qualified leads from the pool and work each one through to a result.' : 'Upload a contact list, let the agent dial it, and track the leads and calls it produces.' }}</p>
          </div>
          <div v-if="planActive && !isMember" class="ax-plan ax-cas--x" style="--d:200ms;">
            <div class="ax-plan-head">
              <div>
                <div class="ax-plan-name">{{ billing.plan_label }}</div>
                <div class="ax-plan-sub">{{ fmtINR(billing.rate_per_minute) }}/min · {{ billing.concurrency }} concurrent · {{ billing.support_tier?.replace(/_/g, ' ') }}</div>
              </div>
              <div class="ax-plan-price" v-if="billing.monthly_inr">{{ fmtINR(billing.monthly_inr) }}<small>/mo</small></div>
            </div>
            <div class="ax-plan-meta">
              <span v-if="billing.subscription_status" class="ax-plan-chip" :class="billing.cancel_at_period_end ? 'is-warn' : 'is-ok'">
                {{ billing.cancel_at_period_end ? 'Cancels at cycle end' : billing.subscription_status }}
              </span>
              <span v-if="billing.current_period_end" class="ax-plan-till">until {{ new Date(billing.current_period_end).toLocaleDateString('en-IN') }}</span>
              <button
                v-if="billing.subscription_status && !billing.cancel_at_period_end"
                type="button" class="ax-plan-cancel" :disabled="cancelBusy" @click="doCancelSubscription"
              >{{ cancelBusy ? 'Cancelling…' : 'Cancel subscription' }}</button>
            </div>
            <div v-if="cancelNote" class="ax-topup-note is-ok" style="margin-top:8px;">{{ cancelNote }}</div>
          </div>

          <div v-if="wallet && !isMember" class="ax-wallet ax-cas--x" style="--d:220ms;">
            <div class="ax-wallet-head">
              <div>
                <div class="ax-wallet-num"><AxCount :value="Number(wallet.credits_remaining) || 0" :format="fmtCredits" /></div>
                <div class="ax-wallet-lbl">Call Credits left</div>
              </div>
              <div class="ax-wallet-meta">
                <div class="ax-wallet-min">≈ <AxCount :value="Number(wallet.estimated_minutes_remaining) || 0" :format="fmtMin" /> min</div>
                <div class="ax-wallet-used"><AxCount :value="Number(wallet.credits_used) || 0" :format="fmtCredits" /> used</div>
              </div>
            </div>
            <div class="ax-wallet-meter" :class="{ 'is-low': walletPct < 0.15, 'is-busy': isToppingUp }">
              <span class="ax-wallet-meter-fill" :style="{ width: (walletPct * 100).toFixed(1) + '%' }"></span>
            </div>

            <div class="ax-topup">
              <div class="ax-topup-label">Add minutes</div>
              <div class="ax-topup-field">
                <input v-model.number="topupMinutes" type="number" min="100" :max="MINUTES_MAX" step="100" class="ax-topup-input" inputmode="numeric" aria-label="Minutes to add" />
                <span class="ax-topup-unit">min</span>
              </div>
              <div class="ax-topup-chips">
                <button
                  v-for="m in TOPUP_PRESETS" :key="m" type="button"
                  class="ax-chip" :class="{ 'is-on': topupMinutes === m }"
                  @click="topupMinutes = m"
                >{{ fmtKMin(m) }}</button>
              </div>
              <div v-if="topupValid" class="ax-topup-preview">
                <strong>≈ {{ fmtMin(topupCreditedMinDisp) }} min</strong>
                <span class="ax-topup-bonus">{{ topupBonusLabel }}</span>
                <span class="ax-topup-sep">·</span>{{ fmtCredits(topupCreditsDisp) }} credits
              </div>
              <button type="button" class="ax-btn2 ax-btn2--accent ax-topup-btn" :disabled="isToppingUp || !topupValid" @click="startTopup">
                {{ isToppingUp ? 'Opening checkout…' : `Top up · pay ${fmtINR(topupBillDisp)}` }}
              </button>
              <div v-if="topupNote" class="ax-topup-note" :class="topupOk ? 'is-ok' : 'is-err'">{{ topupNote }}</div>
              <div v-else-if="!topupValid" class="ax-topup-hint">100 – {{ MINUTES_MAX.toLocaleString('en-IN') }} minutes per purchase.</div>
            </div>
          </div>
        </div>
        <div class="ax-hr ax-draw" style="--d:320ms;"></div>

        <nav class="ax-tabs ax-cas" style="--d:380ms;">
          <div ref="tabsInner" class="ax-tabs-inner">
            <span
              v-if="tabThumb.on" class="ax-tab-thumb"
              :style="{ transform: `translate(${tabThumb.x}px, ${tabThumb.y}px)`, width: tabThumb.w + 'px', height: tabThumb.h + 'px' }"
            ></span>
            <button
              v-for="t in visibleTabs" :key="t.id" type="button"
              class="ax-tab" :class="{ 'is-active': tab === t.id }"
              @click="tab = t.id"
            >{{ t.label }}</button>
          </div>
        </nav>

        <Transition name="axtab" mode="out-in">
          <component :is="activeTab.is" :key="activeTab.id" />
        </Transition>
      </main>
    </div>

    <!-- ============ LEGAL DOC MODAL ============ -->
    <Transition name="axmodal">
    <div v-if="legalModal" class="ax-legal-overlay" @click.self="legalModal = null">
      <div class="ax-legal-modal">
        <button type="button" class="ax-legal-close" aria-label="Close" @click="legalModal = null"><AxIcon name="x" :size="18" /></button>
        <div class="ax-legal-body" v-html="legalModal === 'terms' ? APEX_TERMS_HTML : APEX_PRIVACY_HTML"></div>
        <div class="ax-legal-actions">
          <button type="button" class="ax-btn ax-btn--ghost" @click="legalModal = null">Close</button>
          <button type="button" class="ax-btn ax-btn--accent" @click="termsAccepted = true; legalModal = null">I agree</button>
        </div>
      </div>
    </div>
    </Transition>

    <!-- ============ TOASTS ============ -->
    <div class="ax-toasts">
      <TransitionGroup name="axtoast">
        <div v-for="t in toasts" :key="t.id" class="ax-toast" :class="`is-${t.type}`">
          <AxIcon :name="t.type === 'err' ? 'alert' : 'check'" :size="15" />
          <span>{{ t.message }}</span>
        </div>
      </TransitionGroup>
    </div>
  </div>
</template>

<style scoped>
.ax-root {
  min-height: 100vh;
  background:
    radial-gradient(ellipse 900px 480px at 50% -140px, rgba(230,38,48,0.055), transparent 70%),
    radial-gradient(ellipse 700px 500px at 108% 108%, rgba(255,255,255,0.016), transparent 70%),
    #0A0A0B;
  color: #F3F2F0; font-family: 'Sora', sans-serif; -webkit-font-smoothing: antialiased;
}
.ax-root :deep(*) { box-sizing: border-box; }
@keyframes axFade { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
@keyframes axBlink { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0; } }
.ax-fade { animation: axFade .5s ease; }

/* ── brand mark ── */
.ax-mark { width: 56px; height: 44px; border-radius: 9px; background: linear-gradient(122deg, #F3F2F0 0 42%, #0A0A0B 42% 51%, #F3F2F0 51% 100%); }
.ax-mark--sm { width: 34px; height: 27px; border-radius: 6px; }
.ax-mark--xs { width: 30px; height: 24px; border-radius: 6px; }
/* Real NOKVO mark image (replaces the CSS .ax-mark glyph). Header = sm; the
   login/sign-up hero lockup = lg. */
.ax-brand-img { height: 22px; width: auto; display: block; }
.ax-brand-img--lg { height: 46px; }
.ax-brand-name { font-size: 28px; font-weight: 600; letter-spacing: 0.36em; padding-left: 0.36em; }
.ax-brand-name--sm { font-size: 16px; letter-spacing: 0.2em; padding-left: 0.2em; }
.ax-apex-text { font-size: 12px; font-weight: 600; letter-spacing: 0.5em; padding-left: 0.5em; color: #E62630; }
.ax-apex-text--sm { font-size: 10.5px; letter-spacing: 0.3em; padding-left: 0.3em; }

/* ── auth ── */
.ax-auth { min-height: 100vh; display: grid; place-items: center; position: relative; overflow: hidden; padding: 40px; }
.ax-glow { position: absolute; top: -22%; left: 50%; transform: translateX(-50%); width: 880px; height: 600px; background: radial-gradient(ellipse at center, rgba(230,38,48,0.13), rgba(230,38,48,0.04) 45%, transparent 68%); filter: blur(2px); pointer-events: none; }
.ax-grid { position: absolute; inset: 0; background-image: linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px); background-size: 100% 64px; pointer-events: none; -webkit-mask-image: linear-gradient(transparent, black 30%, black 70%, transparent); mask-image: linear-gradient(transparent, black 30%, black 70%, transparent); }
.ax-auth-card { position: relative; width: 100%; max-width: 392px; }
.ax-auth-card--center { max-width: 420px; text-align: center; }
.ax-brand-stack { display: flex; flex-direction: column; align-items: center; gap: 18px; }
.ax-brand-apex { display: flex; align-items: center; gap: 13px; justify-content: center; margin-top: 9px; }
.ax-rule { width: 36px; height: 1.5px; background: #E62630; }
.ax-brand-inline { display: inline-flex; align-items: center; gap: 11px; margin-bottom: 12px; }
.ax-form { margin-top: 54px; }
.ax-eyebrow { font-size: 11px; letter-spacing: 0.24em; text-transform: uppercase; color: rgba(255,255,255,0.34); font-weight: 600; }
.ax-h1 { font-size: 25px; font-weight: 600; letter-spacing: -0.01em; margin: 11px 0 34px; }
.ax-label { display: block; font-size: 12px; color: rgba(255,255,255,0.55); margin-bottom: 9px; }
.ax-label--inline { margin-bottom: 0; }
.ax-label-row { display: flex; justify-content: space-between; align-items: center; margin: 20px 0 9px; }
.ax-forgot { font-size: 12px; color: rgba(255,255,255,0.4); cursor: pointer; }
.ax-input { width: 100%; background: rgba(0,0,0,0.22); border: 1px solid rgba(255,255,255,0.11); border-radius: 9px; padding: 13px 15px; color: #F3F2F0; font-family: 'Sora', sans-serif; font-size: 14px; outline: none; box-shadow: inset 0 1px 3px rgba(0,0,0,0.25); transition: border-color .18s, box-shadow .18s; }
.ax-input::placeholder { color: rgba(255,255,255,0.26); }
.ax-input:hover { border-color: rgba(255,255,255,0.18); }
.ax-input:focus { border-color: rgba(230,38,48,0.65); box-shadow: inset 0 1px 3px rgba(0,0,0,0.25), 0 0 0 3px rgba(230,38,48,0.13); }
.ax-btn { border-radius: 9px; padding: 9px 15px; font-family: 'Sora', sans-serif; font-size: 13px; cursor: pointer; border: 1px solid transparent; transition: all .18s cubic-bezier(0.22, 1, 0.36, 1); }
.ax-btn--accent { background: linear-gradient(180deg, #F03540, #D91F29); color: #fff; border: none; font-weight: 600; letter-spacing: 0.02em; padding: 15px; font-size: 14px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.22), 0 10px 26px -10px rgba(230,38,48,0.55), 0 2px 6px rgba(0,0,0,0.3); }
.ax-btn--accent:hover:not(:disabled) { transform: translateY(-1px); box-shadow: inset 0 1px 0 rgba(255,255,255,0.25), 0 14px 32px -10px rgba(230,38,48,0.65), 0 3px 8px rgba(0,0,0,0.3); }
.ax-btn--accent:active:not(:disabled) { transform: translateY(0); }
.ax-btn--accent:disabled { opacity: 0.6; cursor: default; }
.ax-btn--full { width: 100%; margin-top: 30px; }
.ax-btn--ghost { background: linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.015)); border: 1px solid rgba(255,255,255,0.13); color: rgba(255,255,255,0.72); font-size: 12.5px; padding: 9px 15px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05); }
.ax-btn--ghost:hover:not(:disabled) { border-color: rgba(255,255,255,0.28); color: #F3F2F0; }
.ax-divider { display: flex; align-items: center; gap: 14px; margin: 30px 0; }
.ax-divider > span:first-child, .ax-divider > span:last-child { flex: 1; height: 1px; background: rgba(255,255,255,0.08); }
.ax-divider-text { font-size: 11px; color: rgba(255,255,255,0.3); letter-spacing: 0.06em; }
.ax-sub-cta { text-align: center; font-size: 13px; color: rgba(255,255,255,0.4); margin: 0; }
/* affiliate-program pointer — a quieter second line under the main CTA */
.ax-affiliate-cta { margin-top: 10px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.06); font-size: 12px; color: rgba(255,255,255,0.32); }
.ax-link { color: #F3F2F0; cursor: pointer; background: none; border: none; padding: 0; font: inherit; text-decoration: underline; }
.ax-google-fallback { display: flex; flex-direction: column; align-items: center; gap: 8px; margin-bottom: 26px; padding: 12px 14px; border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; font-size: 12px; line-height: 1.5; color: rgba(255,255,255,0.55); text-align: center; }
/* Feedback modal */
/* NOVA — the assistant's launcher. Deliberately the loudest thing in the header:
   animated gradient fill, breathing glow, a sweeping shine, and a twinkling
   star. Everything else in the header stays ghost-quiet so this reads first. */
.ax-btn--nova {
  position: relative; display: inline-flex; align-items: center; gap: 8px;
  padding: 9px 18px; border-radius: 10px; cursor: pointer; overflow: hidden;
  font-family: inherit; font-size: 12.5px; font-weight: 700; letter-spacing: 0.18em;
  color: #fff; border: 1px solid rgba(230,38,48,0.7);
  background: linear-gradient(120deg, rgba(230,38,48,0.30), rgba(255,120,60,0.14) 45%, rgba(230,38,48,0.30));
  background-size: 220% 220%;
  box-shadow: 0 0 16px rgba(230,38,48,0.35), inset 0 0 12px rgba(230,38,48,0.14);
  animation: axNovaSheen 3.6s ease-in-out infinite, axNovaBreathe 2.4s ease-in-out infinite;
  transition: transform .15s ease, border-color .15s ease;
}
.ax-btn--nova::after {
  content: ''; position: absolute; top: 0; left: -60%; width: 42%; height: 100%;
  background: linear-gradient(100deg, transparent, rgba(255,255,255,0.30), transparent);
  transform: skewX(-20deg); animation: axNovaSweep 3s ease-in-out infinite; pointer-events: none;
}
.ax-btn--nova:hover {
  border-color: #E62630;
  box-shadow: 0 0 26px rgba(230,38,48,0.6), inset 0 0 14px rgba(230,38,48,0.22);
  transform: translateY(-1px);
}
/* Press: a quick sink + a light burst from the center + the star flaring —
   the click itself carries the same energy as the idle shimmer. ::before is
   the burst layer (::after is taken by the sweeping shine). */
.ax-btn--nova::before {
  content: ''; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
  background: radial-gradient(circle at 50% 50%, rgba(255,255,255,0.4), rgba(230,38,48,0.25) 45%, transparent 75%);
  opacity: 0; transition: opacity .3s ease;
}
.ax-btn--nova:active {
  transform: translateY(0) scale(0.96);
  border-color: #FF4B54;
  box-shadow: 0 0 34px rgba(230,38,48,0.85), inset 0 0 18px rgba(230,38,48,0.35);
}
.ax-btn--nova:active::before { opacity: 1; transition: opacity .04s ease; }
.ax-btn--nova:active .ax-nova-star {
  animation: none;
  transform: scale(1.55) rotate(120deg);
  transition: transform .2s cubic-bezier(0.34, 1.56, 0.64, 1);
  color: #fff; text-shadow: 0 0 12px rgba(255,255,255,0.9);
}
.ax-nova-star { font-size: 13px; line-height: 1; color: #FFD9DC; animation: axNovaTwinkle 1.8s ease-in-out infinite; transition: transform .2s ease, color .2s ease; }
@keyframes axNovaSheen { 0%, 100% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } }
@keyframes axNovaBreathe {
  0%, 100% { box-shadow: 0 0 14px rgba(230,38,48,0.3), inset 0 0 12px rgba(230,38,48,0.12); }
  50% { box-shadow: 0 0 24px rgba(230,38,48,0.55), inset 0 0 12px rgba(230,38,48,0.2); }
}
@keyframes axNovaSweep { 0% { left: -60%; } 55%, 100% { left: 135%; } }
@keyframes axNovaTwinkle {
  0%, 100% { opacity: 0.75; transform: scale(1) rotate(0deg); }
  50% { opacity: 1; transform: scale(1.3) rotate(22deg); }
}
@media (prefers-reduced-motion: reduce) {
  .ax-btn--nova, .ax-btn--nova::after, .ax-nova-star { animation: none; }
  .ax-btn--nova:active, .ax-btn--nova:active .ax-nova-star { transform: none; transition: none; }
}
/* Panel exit — the overlay fades while the panel glides back right, mirroring
   the entrance so closing feels as considered as opening. (Enter is handled by
   NovaPanel's own entrance animation.) */
.nova-leave-active { transition: opacity .24s ease; }
.nova-leave-active :deep(.nv-panel) { transition: transform .24s cubic-bezier(0.4, 0, 1, 1); }
.nova-leave-to { opacity: 0; }
.nova-leave-to :deep(.nv-panel) { transform: translateX(56px); }
.ax-modal-overlay { position: fixed; inset: 0; background: rgba(4,4,5,0.6); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: 20px; }
.ax-modal { background: linear-gradient(180deg, #17171A, #101012); border: 1px solid rgba(255,255,255,0.11); border-radius: 18px; padding: 26px; width: 100%; max-width: 460px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.07), 0 40px 90px -20px rgba(0,0,0,0.75); }

/* Mandatory legal acceptance (payment step) */
.ax-terms { display: flex; align-items: flex-start; gap: 10px; margin-top: 22px; font-size: 12.5px; line-height: 1.55; color: rgba(255,255,255,0.6); cursor: pointer; }
.ax-terms-box { margin-top: 2px; width: 16px; height: 16px; accent-color: #E62630; cursor: pointer; flex: 0 0 auto; }
/* Legal doc modal */
.ax-legal-overlay { position: fixed; inset: 0; background: rgba(4,4,5,0.66); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); display: flex; align-items: center; justify-content: center; z-index: 1100; padding: 20px; }
.ax-legal-modal { position: relative; background: linear-gradient(180deg, #17171A, #101012); border: 1px solid rgba(255,255,255,0.11); border-radius: 18px; width: 100%; max-width: 720px; max-height: 84vh; display: flex; flex-direction: column; box-shadow: inset 0 1px 0 rgba(255,255,255,0.07), 0 40px 90px -20px rgba(0,0,0,0.75); }
.ax-legal-close { position: absolute; top: 12px; right: 14px; background: none; border: none; color: rgba(255,255,255,0.5); font-size: 22px; line-height: 1; cursor: pointer; }
.ax-legal-body { overflow-y: auto; padding: 30px 30px 10px; color: rgba(255,255,255,0.78); font-size: 13px; line-height: 1.6; }
.ax-legal-actions { display: flex; justify-content: flex-end; gap: 10px; padding: 14px 24px; border-top: 1px solid rgba(255,255,255,0.08); }
.ax-legal-actions .ax-btn { margin-top: 0; width: auto; }
.ax-legal-body :deep(h3) { font-family: 'Sora', sans-serif; font-size: 18px; color: #F3F2F0; margin: 0 0 4px; }
.ax-legal-body :deep(h4) { font-family: 'Sora', sans-serif; font-size: 14px; color: #F3F2F0; margin: 20px 0 6px; }
.ax-legal-body :deep(.legal-meta) { font-size: 11.5px; color: rgba(255,255,255,0.4); margin: 0 0 14px; }
.ax-legal-body :deep(p) { margin: 0 0 10px; }
.ax-legal-body :deep(ul) { margin: 0 0 10px; padding-left: 20px; }
.ax-legal-body :deep(li) { margin: 0 0 5px; }
.ax-legal-body :deep(a) { color: #E88; text-decoration: underline; }
.ax-legal-body :deep(strong) { color: rgba(255,255,255,0.92); }
.ax-modal-title { font-family: 'Sora', sans-serif; font-size: 22px; font-weight: 600; margin: 4px 0 16px; color: #F3F2F0; }
.ax-fb-cats { display: flex; gap: 8px; margin: 0 0 12px; }
.ax-fb-cat { flex: 1; padding: 9px 12px; font-size: 13px; border-radius: 9px; border: 1px solid rgba(255,255,255,0.14); background: transparent; color: rgba(255,255,255,0.6); cursor: pointer; transition: all .18s; }
.ax-fb-cat:hover:not(.is-on) { border-color: rgba(255,255,255,0.28); color: #F3F2F0; }
.ax-fb-cat.is-on { background: rgba(230,38,48,0.13); border-color: rgba(230,38,48,0.8); color: #F3F2F0; box-shadow: 0 0 14px -6px rgba(230,38,48,0.5); }
.ax-fb-textarea { resize: vertical; min-height: 110px; font-family: inherit; width: 100%; }
.ax-fb-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
.ax-google { display: flex; justify-content: center; margin-bottom: 26px; min-height: 40px; color-scheme: light; }

/* payment */
.ax-pay-plan { display: flex; justify-content: space-between; align-items: center; gap: 16px; border: 1px solid rgba(255,255,255,0.11); border-radius: 14px; padding: 16px; margin-top: 24px; background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.012)); box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 14px 30px -18px rgba(0,0,0,0.55); }
.ax-pay-plan strong { display: block; font-size: 15px; }
.ax-pay-plan span { font-size: 12px; color: rgba(255,255,255,0.45); }
.ax-pay-price { font-size: 22px; font-weight: 700; white-space: nowrap; }
.ax-pay-price small { font-size: 12px; font-weight: 400; opacity: 0.6; }
.ax-pay-credit { font-size: 12.5px; color: #7FD9A8; margin-top: 10px; line-height: 1.5; }
.ax-pay-summary { border: 1px solid rgba(255,255,255,0.09); border-radius: 12px; padding: 14px 16px; margin-top: 18px; display: grid; gap: 8px; background: rgba(0,0,0,0.18); box-shadow: inset 0 1px 3px rgba(0,0,0,0.2); }
.ax-pay-row { display: flex; justify-content: space-between; font-size: 13.5px; color: rgba(255,255,255,0.7); }
.ax-pay-row--total { border-top: 1px solid rgba(255,255,255,0.1); padding-top: 8px; font-size: 15px; font-weight: 700; color: #F3F2F0; }

/* onboarding */
.ax-onb-steps { display: flex; justify-content: center; gap: 18px; margin: 8px 0 24px; font-size: 12px; color: rgba(255,255,255,0.35); font-family: 'JetBrains Mono', monospace; }
.ax-onb-steps .is-on { color: #E62630; }

/* balance widget */
.ax-page-head--row { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; flex-wrap: wrap; }
.ax-plan { border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 16px 20px; min-width: 320px; max-width: 360px; margin-bottom: 14px; background: linear-gradient(160deg, rgba(230,38,48,0.06), rgba(255,255,255,0.012) 70%); box-shadow: inset 0 1px 0 rgba(255,255,255,0.06); }
.ax-plan-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.ax-plan-name { font-size: 16px; font-weight: 700; letter-spacing: 0.01em; }
.ax-plan-sub { font-size: 12px; color: rgba(255,255,255,0.55); margin-top: 3px; text-transform: capitalize; }
.ax-plan-price { font-size: 18px; font-weight: 700; white-space: nowrap; }
.ax-plan-price small { font-size: 11px; font-weight: 500; opacity: 0.6; }
.ax-plan-meta { display: flex; align-items: center; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.ax-plan-chip { font-size: 11px; padding: 3px 9px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.16); text-transform: capitalize; }
.ax-plan-chip.is-ok { color: #7FD9A8; border-color: rgba(127,217,168,0.4); }
.ax-plan-chip.is-warn { color: #F0B657; border-color: rgba(240,182,87,0.4); }
.ax-plan-till { font-size: 11.5px; color: rgba(255,255,255,0.5); }
.ax-plan-cancel { margin-left: auto; font-size: 12px; color: rgba(255,255,255,0.6); background: none; border: 1px solid rgba(255,255,255,0.16); border-radius: 8px; padding: 5px 11px; cursor: pointer; transition: color .15s, border-color .15s; }
.ax-plan-cancel:hover:not(:disabled) { color: #E62630; border-color: rgba(230,38,48,0.5); }
.ax-plan-cancel:disabled { opacity: 0.5; cursor: default; }
.ax-wallet { border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 18px 20px; min-width: 320px; max-width: 360px; background: linear-gradient(160deg, rgba(255,255,255,0.045), rgba(255,255,255,0.012) 60%, rgba(230,38,48,0.03)); box-shadow: inset 0 1px 0 rgba(255,255,255,0.06), 0 20px 44px -22px rgba(0,0,0,0.6); }
.ax-wallet-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; }
.ax-wallet-num { font-size: 32px; font-weight: 700; font-family: 'JetBrains Mono', monospace; line-height: 1; letter-spacing: -0.01em; }
.ax-wallet-lbl { font-size: 10.5px; letter-spacing: 0.1em; text-transform: uppercase; color: rgba(255,255,255,0.4); margin-top: 7px; }
.ax-wallet-meta { text-align: right; font-family: 'JetBrains Mono', monospace; line-height: 1.5; }
.ax-wallet-min { font-size: 13px; color: rgba(255,255,255,0.6); }
.ax-wallet-used { font-size: 11px; color: rgba(255,255,255,0.3); }
/* spend meter — remaining share of the purchased pool; reddens when low */
.ax-wallet-meter { height: 5px; border-radius: 999px; background: rgba(255,255,255,0.07); overflow: hidden; margin-top: 15px; box-shadow: inset 0 1px 2px rgba(0,0,0,0.3); }
.ax-wallet-meter-fill { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #4AC88C, #7FD9A8); transition: width .7s cubic-bezier(0.22, 1, 0.36, 1); }
.ax-wallet-meter.is-low .ax-wallet-meter-fill { background: linear-gradient(90deg, #D91F29, #F03540); box-shadow: 0 0 10px rgba(230,38,48,0.5); }
.ax-topup { margin-top: 18px; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.08); }
.ax-topup-label { font-size: 10.5px; letter-spacing: 0.1em; text-transform: uppercase; color: rgba(255,255,255,0.4); margin-bottom: 11px; }
.ax-topup-field { position: relative; display: flex; align-items: center; }
.ax-topup-input { width: 100%; padding: 10px 44px 10px 13px; font-size: 15px; font-family: 'JetBrains Mono', monospace; background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.14); border-radius: 8px; color: #F3F2F0; outline: none; transition: border-color .15s, box-shadow .15s; }
.ax-topup-input:focus { border-color: #E62630; box-shadow: 0 0 0 3px rgba(230,38,48,0.12); }
.ax-topup-input::-webkit-outer-spin-button, .ax-topup-input::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
.ax-topup-unit { position: absolute; right: 14px; font-size: 12px; color: rgba(255,255,255,0.35); pointer-events: none; font-family: 'JetBrains Mono', monospace; }
.ax-topup-chips { display: flex; gap: 7px; margin-top: 9px; }
.ax-chip { flex: 1; padding: 7px 0; font-size: 12px; font-family: 'JetBrains Mono', monospace; border-radius: 7px; border: 1px solid rgba(255,255,255,0.14); background: transparent; color: rgba(255,255,255,0.55); cursor: pointer; transition: all .15s; }
.ax-chip:hover { border-color: rgba(255,255,255,0.3); color: #F3F2F0; }
.ax-chip.is-on { border-color: #E62630; background: rgba(230,38,48,0.12); color: #fff; }
.ax-topup-preview { margin-top: 13px; font-size: 13px; color: rgba(255,255,255,0.7); }
.ax-topup-preview strong { color: #F3F2F0; font-family: 'JetBrains Mono', monospace; font-weight: 600; }
.ax-topup-bonus { color: #7FD9A8; font-size: 11px; margin-left: 7px; }
.ax-topup-sep { color: rgba(255,255,255,0.22); margin: 0 7px; }
.ax-btn2 { border-radius: 6px; font-family: 'Sora', sans-serif; cursor: pointer; border: none; transition: all .15s; }
.ax-btn2--accent { background: #E62630; color: #fff; font-weight: 600; }
.ax-btn2--sm { padding: 7px 12px; font-size: 12px; }
.ax-btn2:disabled { opacity: 0.55; cursor: default; }
.ax-topup-btn { width: 100%; margin-top: 14px; padding: 12px; font-size: 14px; }
.ax-topup-note { font-size: 12px; margin-top: 10px; line-height: 1.45; }
.ax-topup-note.is-ok { color: #7FD9A8; }
.ax-topup-note.is-err { color: #ff8b8b; }
.ax-topup-hint { font-size: 12px; color: rgba(255,255,255,0.4); margin-top: 9px; }
.ax-error { color: #ff8b8b; font-size: 13px; margin: 14px 0 0; }
.ax-error--center { text-align: center; }

/* ── mfa ── */
.ax-mfa-hint { font-size: 14px; color: rgba(255,255,255,0.5); margin: 11px 0 28px; line-height: 1.5; }
.ax-mfa-row { display: flex; gap: 11px; justify-content: center; margin-bottom: 26px; }
.ax-mfa-box { width: 52px; height: 62px; border: 1px solid rgba(255,255,255,0.16); border-radius: 11px; background: rgba(0,0,0,0.22); box-shadow: inset 0 1px 3px rgba(0,0,0,0.25); color: #F3F2F0; text-align: center; font-family: 'JetBrains Mono', monospace; font-size: 24px; outline: none; transition: all .18s; }
.ax-mfa-box:focus { border: 1.5px solid #E62630; box-shadow: 0 0 0 4px rgba(230,38,48,0.12); }
.ax-back { margin-top: 30px; }

/* ── app shell ── */
.ax-header { position: sticky; top: 0; z-index: 30; backdrop-filter: blur(20px) saturate(1.4); -webkit-backdrop-filter: blur(20px) saturate(1.4); background: rgba(10,10,11,0.66); border-bottom: 1px solid rgba(255,255,255,0.07); box-shadow: 0 1px 0 rgba(0,0,0,0.4), 0 12px 32px -20px rgba(0,0,0,0.6); }
.ax-header-inner { max-width: 1120px; margin: 0 auto; padding: 0 40px; height: 66px; display: flex; align-items: center; justify-content: space-between; }
.ax-header-right { display: flex; align-items: center; gap: 20px; }
.ax-user { display: flex; align-items: center; gap: 11px; }
.ax-avatar { width: 34px; height: 34px; border-radius: 9px; border: 1px solid rgba(255,255,255,0.16); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 14px; background: linear-gradient(180deg, rgba(255,255,255,0.09), rgba(255,255,255,0.02)); box-shadow: inset 0 1px 0 rgba(255,255,255,0.1), 0 4px 10px -4px rgba(0,0,0,0.5); }
.ax-user-meta { line-height: 1.2; }
.ax-user-name { font-size: 13px; font-weight: 600; }
.ax-user-role { font-size: 10px; letter-spacing: 0.14em; color: #E62630; font-family: 'JetBrains Mono', monospace; }
.ax-main { max-width: 1120px; margin: 0 auto; padding: 54px 40px 96px; }
.ax-page-head { }
.ax-title { font-size: 42px; font-weight: 600; letter-spacing: -0.025em; margin: 14px 0 13px; background: linear-gradient(180deg, #FFFFFF 20%, rgba(243,242,240,0.72)); -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
.ax-page-sub { font-size: 15px; color: rgba(255,255,255,0.5); max-width: 560px; line-height: 1.55; margin: 0; }
.ax-hr { height: 1px; background: linear-gradient(90deg, rgba(230,38,48,0.35), rgba(255,255,255,0.09) 22%, rgba(255,255,255,0.05) 70%, transparent); margin-top: 34px; }
.ax-tabs { display: flex; justify-content: center; margin: 32px 0 46px; }
.ax-tabs-inner { position: relative; display: inline-flex; gap: 4px; padding: 6px; background: rgba(0,0,0,0.32); border: 1px solid rgba(255,255,255,0.08); border-radius: 15px; flex-wrap: wrap; box-shadow: inset 0 1px 4px rgba(0,0,0,0.35), 0 1px 0 rgba(255,255,255,0.04); }
.ax-tab { position: relative; z-index: 1; padding: 11px 22px; border-radius: 10px; font-size: 13.5px; cursor: pointer; border: none; background: transparent; color: rgba(255,255,255,0.5); font-weight: 500; font-family: 'Sora', sans-serif; white-space: nowrap; transition: color .18s cubic-bezier(0.22, 1, 0.36, 1), background .18s cubic-bezier(0.22, 1, 0.36, 1); }
.ax-tab:hover:not(.is-active) { color: rgba(255,255,255,0.82); background: rgba(255,255,255,0.045); }
.ax-tab.is-active { color: #0A0A0B; font-weight: 600; }
/* mobile shell */
@media (max-width: 720px) {
  .ax-header-inner { padding: 0 18px; }
  .ax-header-right { gap: 12px; }
  .ax-user-meta { display: none; }
  .ax-main { padding: 34px 18px 72px; }
  .ax-title { font-size: 30px; }
  .ax-wallet { min-width: 0; width: 100%; max-width: none; }
  .ax-tab { padding: 10px 14px; font-size: 12.5px; }
}
/* the single sliding pill behind the active tab — glides between buttons */
.ax-tab-thumb {
  position: absolute; top: 0; left: 0; z-index: 0; border-radius: 10px;
  background: linear-gradient(180deg, #FFFFFF, #E9E7E4);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.8), 0 3px 10px -2px rgba(0,0,0,0.55);
  transition: transform .28s cubic-bezier(0.22, 1, 0.36, 1), width .28s cubic-bezier(0.22, 1, 0.36, 1), height .28s cubic-bezier(0.22, 1, 0.36, 1);
}
@media (prefers-reduced-motion: reduce) { .ax-tab-thumb { transition: none; } }
/* tab content swap — a rise + clip wipe, like a panel powering on */
.axtab-enter-active { transition: opacity .28s ease, transform .28s var(--ax-out), clip-path .28s var(--ax-out); }
.axtab-leave-active { transition: opacity .14s var(--ax-in); }
.axtab-enter-from { opacity: 0; transform: translateY(10px); clip-path: inset(0 0 12% 0 round 16px); }
.axtab-enter-to { clip-path: inset(0 0 0 0 round 16px); }
.axtab-leave-to { opacity: 0; }

/* ── motion: login timeline ──
   The door introduces the house motif: the mark lands and broadcasts ONE radar
   ring; the wordmark's tracking settles; the red rules draw outward; then the
   form cascades in. */
.ax-brand-stack { position: relative; }
.ax-brand-stack::before {
  content: ''; position: absolute; top: 23px; left: 50%; width: 72px; height: 72px;
  margin: -36px 0 0 -36px; border-radius: 50%; pointer-events: none;
  border: 1.5px solid rgba(230,38,48,0.5); opacity: 0;
  animation: axRingIn 1.1s var(--ax-out) 0.4s both;
}
@keyframes axRingIn { 0% { opacity: 0; transform: scale(0.5); } 18% { opacity: 0.8; } 100% { opacity: 0; transform: scale(2.4); } }
.ax-brand-img--lg { animation: axMarkIn .7s var(--ax-out) both; }
@keyframes axMarkIn { from { opacity: 0; transform: scale(0.7); } to { opacity: 1; transform: scale(1); } }
.ax-brand-stack .ax-brand-name { animation: axNameIn .65s var(--ax-out) .15s both; }
@keyframes axNameIn { from { opacity: 0; letter-spacing: 0.5em; } to { opacity: 1; letter-spacing: 0.36em; } }
.ax-brand-apex .ax-rule { animation: axRuleIn .5s var(--ax-out) .5s both; }
.ax-brand-apex .ax-rule:first-child { transform-origin: right center; }
.ax-brand-apex .ax-rule:last-child { transform-origin: left center; }
@keyframes axRuleIn { from { transform: scaleX(0); } to { transform: scaleX(1); } }
.ax-brand-apex .ax-apex-text { animation: axCasIn .5s var(--ax-out) .55s both; }
.ax-form > * { animation: axCasIn var(--ax-slow) var(--ax-out) both; }
.ax-form > *:nth-child(1) { animation-delay: 240ms; }
.ax-form > *:nth-child(2) { animation-delay: 280ms; }
.ax-form > *:nth-child(3) { animation-delay: 320ms; }
.ax-form > *:nth-child(4) { animation-delay: 360ms; }
.ax-form > *:nth-child(5) { animation-delay: 400ms; }
.ax-form > *:nth-child(6) { animation-delay: 440ms; }
.ax-form > *:nth-child(7) { animation-delay: 480ms; }
.ax-form > *:nth-child(8) { animation-delay: 520ms; }
.ax-form > *:nth-child(9) { animation-delay: 560ms; }
.ax-form > *:nth-child(n+10) { animation-delay: 600ms; }

/* ambient: the glow breathes, the grid drifts — alive, not busy */
.ax-glow { animation: axGlowBreathe 9s ease-in-out infinite; }
@keyframes axGlowBreathe {
  0%, 100% { opacity: 0.85; transform: translateX(-50%) scale(1); }
  50% { opacity: 1; transform: translateX(-50%) scale(1.06); }
}
.ax-grid { animation: axGridDrift 60s linear infinite; }
@keyframes axGridDrift { from { background-position: 0 0; } to { background-position: 0 64px; } }

/* ── motion: MFA ── */
.ax-mfa-box.is-filled { animation: axDigitPop .18s var(--ax-out); }
@keyframes axDigitPop { from { transform: scale(1.08); } to { transform: scale(1); } }
.ax-mfa-row.is-shake { animation: axShake .35s ease; }

/* ── motion: modals — card scales up under an easing veil ── */
.axmodal-enter-active { transition: opacity var(--ax-t) ease; }
.axmodal-enter-active :is(.ax-modal, .ax-legal-modal) { transition: transform var(--ax-t) var(--ax-out); }
.axmodal-enter-from { opacity: 0; }
.axmodal-enter-from :is(.ax-modal, .ax-legal-modal) { transform: scale(0.96) translateY(8px); }
.axmodal-leave-active { transition: opacity .15s var(--ax-in); }
.axmodal-leave-active :is(.ax-modal, .ax-legal-modal) { transition: transform .15s var(--ax-in); }
.axmodal-leave-to { opacity: 0; }
.axmodal-leave-to :is(.ax-modal, .ax-legal-modal) { transform: scale(0.97) translateY(4px); }

/* ── motion: wallet meter — shimmer only while a top-up is applying; low
   balance breathes as a warning, never celebrates ── */
.ax-wallet-meter.is-busy .ax-wallet-meter-fill { position: relative; overflow: hidden; }
.ax-wallet-meter.is-busy .ax-wallet-meter-fill::after {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(90deg, transparent 30%, rgba(255,255,255,0.4) 50%, transparent 70%);
  background-size: 200% 100%; animation: axShimmer 1.8s linear infinite;
}
.ax-wallet-meter.is-low .ax-wallet-meter-fill { animation: axLowBreath 2.4s ease-in-out infinite; }
@keyframes axLowBreath { 0%, 100% { opacity: 1; } 50% { opacity: 0.55; } }

/* ── motion: auth accent button press burst ── */
.ax-btn--accent { position: relative; overflow: hidden; }
.ax-btn--accent::after {
  content: ''; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
  background: radial-gradient(circle at 50% 50%, rgba(255,255,255,0.35), transparent 70%);
  opacity: 0; transition: opacity .22s ease;
}
.ax-btn--accent:active:not(:disabled)::after { opacity: 1; transition: opacity .04s ease; }
.ax-btn--ghost:active:not(:disabled) { transform: scale(0.97); }
</style>
