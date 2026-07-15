<script setup>
// NOKVO Affiliate Program — standalone lightweight app (dark, mirrors the APEX
// visual idiom via the shared apex-theme.css). Three screens driven by the
// route's initialView prop: LOGIN (affiliate number + TOTP), SIGNUP (3-step
// wizard: details → authenticator QR+verify → number reveal), and the
// DASHBOARD (commissions + bank details).
import { ref, onMounted, watch } from 'vue';
import { useRouter } from 'vue-router';
import QrcodeVue from 'qrcode.vue';
import {
  signup as apiSignup,
  verifySignupTotp,
  login as apiLogin,
  fetchMe,
  getToken,
  clearToken,
  extractError,
} from './affiliateApi.js';
import AffiliateDashboard from './views/AffiliateDashboard.vue';
import '../apex/apex-theme.css';

const props = defineProps({ initialView: { type: String, default: 'login' } });
const router = useRouter();

const screen = ref('login'); // login | signup | dashboard
const busy = ref(false);
const errorMsg = ref('');
const me = ref(null);

// ── login state ──
const loginNumber = ref('');
const loginDigits = ref(['', '', '', '', '', '']);

// ── signup wizard state ──
const signupStep = ref(1); // 1 details → 2 authenticator → 3 done
const form = ref({ full_name: '', date_of_birth: '', email: '' });
const setupToken = ref('');
const totpUri = ref('');
const totpSecret = ref('');
const verifyDigits = ref(['', '', '', '', '', '']);
const affiliateNumber = ref('');
const copied = ref(false);

function resetMessages() { errorMsg.value = ''; }

function goLogin() { resetMessages(); screen.value = 'login'; router.replace('/affiliate'); }
function goSignup() { resetMessages(); signupStep.value = 1; screen.value = 'signup'; router.replace('/affiliate/signup'); }

// ── 6-digit code boxes (same interaction as the APEX MFA row) ──
// NOTE: these receive the digits ARRAY, not the ref — template expressions
// auto-unwrap refs, so `codeInput(verifyDigits, …)` in an inline handler
// passes the unwrapped (still reactive) array. Touching `.value` here was the
// bug that swallowed every keystroke.
function codeInput(digits, prefix, i, ev) {
  const v = (ev.target.value || '').replace(/\D/g, '').slice(-1);
  digits[i] = v;
  ev.target.value = v; // keep the DOM box in sync even when no re-render fires
  if (v && i < 5) document.getElementById(`${prefix}-${i + 1}`)?.focus();
}
function codeKey(digits, prefix, i, ev) {
  if (ev.key === 'Backspace' && !digits[i] && i > 0) {
    document.getElementById(`${prefix}-${i - 1}`)?.focus();
  }
}
function codePaste(digits, prefix, ev) {
  const txt = (ev.clipboardData?.getData('text') || '').replace(/\D/g, '').slice(0, 6);
  if (!txt) return;
  ev.preventDefault();
  for (let i = 0; i < 6; i++) digits[i] = txt[i] || '';
  document.getElementById(`${prefix}-${Math.min(txt.length, 6) - 1}`)?.focus();
}

// ── login ──
async function doLogin() {
  resetMessages();
  const code = loginDigits.value.join('');
  if (!loginNumber.value.trim()) { errorMsg.value = 'Enter your affiliate number.'; return; }
  if (code.length !== 6) { errorMsg.value = 'Enter the 6-digit code from your authenticator app.'; return; }
  busy.value = true;
  try {
    me.value = await apiLogin(loginNumber.value.trim().toUpperCase(), code);
    screen.value = 'dashboard';
    router.replace('/affiliate/dashboard');
  } catch (e) {
    errorMsg.value = extractError(e, 'Could not sign in.');
    loginDigits.value = ['', '', '', '', '', ''];
  } finally { busy.value = false; }
}

function logout() {
  clearToken();
  me.value = null;
  loginNumber.value = '';
  loginDigits.value = ['', '', '', '', '', ''];
  goLogin();
}

// ── signup ──
async function submitDetails() {
  resetMessages();
  if (!form.value.full_name.trim()) { errorMsg.value = 'Enter your full name.'; return; }
  if (!form.value.date_of_birth) { errorMsg.value = 'Enter your date of birth.'; return; }
  if (!form.value.email.trim()) { errorMsg.value = 'Enter your email address.'; return; }
  busy.value = true;
  try {
    const fd = new FormData();
    fd.append('full_name', form.value.full_name.trim());
    fd.append('date_of_birth', form.value.date_of_birth);
    fd.append('email', form.value.email.trim());
    const r = await apiSignup(fd);
    setupToken.value = r.setup_token;
    totpUri.value = r.totp_uri;
    totpSecret.value = r.secret;
    verifyDigits.value = ['', '', '', '', '', ''];
    signupStep.value = 2;
  } catch (e) {
    errorMsg.value = extractError(e, 'Could not create your affiliate account.');
  } finally { busy.value = false; }
}

async function submitTotpVerify() {
  resetMessages();
  const code = verifyDigits.value.join('');
  if (code.length !== 6) { errorMsg.value = 'Enter the 6-digit code from your authenticator app.'; return; }
  busy.value = true;
  try {
    const r = await verifySignupTotp(setupToken.value, code);
    affiliateNumber.value = r.affiliate_number;
    me.value = r.affiliate;
    signupStep.value = 3;
  } catch (e) {
    errorMsg.value = extractError(e, 'That code didn\'t match — try the current one.');
    verifyDigits.value = ['', '', '', '', '', ''];
  } finally { busy.value = false; }
}

async function copyNumber() {
  try {
    await navigator.clipboard.writeText(affiliateNumber.value);
    copied.value = true;
    setTimeout(() => { copied.value = false; }, 1600);
  } catch { /* clipboard denied — the number is on screen */ }
}

function enterDashboard() {
  screen.value = 'dashboard';
  router.replace('/affiliate/dashboard');
}

// ── bootstrap: restore an existing session on deep-link/refresh ──
onMounted(async () => {
  if (props.initialView === 'signup') { screen.value = 'signup'; return; }
  if (getToken()) {
    try {
      me.value = await fetchMe();
      screen.value = 'dashboard';
      if (props.initialView !== 'dashboard') router.replace('/affiliate/dashboard');
      return;
    } catch { clearToken(); }
  }
  screen.value = 'login';
  if (props.initialView === 'dashboard') router.replace('/affiliate');
});
watch(() => props.initialView, (v) => {
  if (v === 'signup' && screen.value !== 'dashboard') { goSignup(); }
});
</script>

<template>
  <div class="ax-root af-root">
    <!-- ============ LOGIN ============ -->
    <div v-if="screen === 'login'" class="af-auth">
      <div class="af-glow"></div>
      <div class="af-card ax-anim">
        <div class="af-brand"><span class="af-brand-nokvo">NOKVO</span><span class="af-brand-tag">AFFILIATE</span></div>
        <div class="af-eyebrow">Partner sign in</div>
        <h1 class="af-h1">Welcome back</h1>
        <label class="af-label">Affiliate number</label>
        <input v-model="loginNumber" type="text" class="af-input af-input--mono" placeholder="NKV•••••••" autocapitalize="characters" spellcheck="false" style="text-transform:uppercase;" />
        <label class="af-label" style="margin-top:16px;">Authenticator code</label>
        <div class="af-mfa-row" @paste="codePaste(loginDigits, 'af-login', $event)">
          <input
            v-for="(d, i) in loginDigits" :key="i" :id="`af-login-${i}`"
            class="af-mfa-box" inputmode="numeric" maxlength="1"
            :value="d" @input="codeInput(loginDigits, 'af-login', i, $event)" @keydown="codeKey(loginDigits, 'af-login', i, $event)"
          />
        </div>
        <p v-if="errorMsg" class="af-error">{{ errorMsg }}</p>
        <button type="button" class="ax-btn2 ax-btn2--accent af-btn-full" :disabled="busy" @click="doLogin">
          {{ busy ? 'Signing in…' : 'Sign in' }}
        </button>
        <p class="af-hint">Lost your authenticator? Contact support to reset it.</p>
        <p class="af-switch">New here? <span class="af-link" @click="goSignup">Become an affiliate</span></p>
      </div>
    </div>

    <!-- ============ SIGNUP WIZARD ============ -->
    <div v-else-if="screen === 'signup'" class="af-auth">
      <div class="af-glow"></div>
      <div class="af-card ax-anim" style="max-width:460px;">
        <div class="af-brand"><span class="af-brand-nokvo">NOKVO</span><span class="af-brand-tag">AFFILIATE</span></div>

        <!-- step 1 · details -->
        <template v-if="signupStep === 1">
          <div class="af-eyebrow">Step 1 of 3 · Your details</div>
          <h1 class="af-h1">Become a NOKVO affiliate</h1>
          <p class="af-sub">Refer businesses to NOKVO APEX and earn 5% of their first month and 2% every month after — paid to your bank within 2 days of billing.</p>
          <label class="af-label">Full name <span class="af-req">as per your bank records</span></label>
          <input v-model="form.full_name" type="text" class="af-input" placeholder="Priya Sharma" />
          <label class="af-label" style="margin-top:14px;">Date of birth <span class="af-req">you must be 18+</span></label>
          <input v-model="form.date_of_birth" type="date" class="af-input" />
          <label class="af-label" style="margin-top:14px;">Email</label>
          <input v-model="form.email" type="email" class="af-input" placeholder="you@example.com" />
          <p v-if="errorMsg" class="af-error">{{ errorMsg }}</p>
          <button type="button" class="ax-btn2 ax-btn2--accent af-btn-full" :disabled="busy" @click="submitDetails">
            {{ busy ? 'Creating…' : 'Continue' }}
          </button>
          <p class="af-switch">Already an affiliate? <span class="af-link" @click="goLogin">Sign in</span></p>
        </template>

        <!-- step 2 · authenticator -->
        <template v-else-if="signupStep === 2">
          <div class="af-eyebrow">Step 2 of 3 · Secure your account</div>
          <h1 class="af-h1">Scan with your authenticator</h1>
          <p class="af-sub">Scan this QR in Google Authenticator (or any TOTP app). It becomes your only way to sign in — there is no password.</p>
          <div class="af-qr"><qrcode-vue :value="totpUri" :size="164" level="M" background="#ffffff" foreground="#000000" /></div>
          <p class="af-secret">Can't scan? Enter this key manually: <code>{{ totpSecret }}</code></p>
          <label class="af-label" style="margin-top:16px;">Enter the 6-digit code to confirm</label>
          <div class="af-mfa-row" @paste="codePaste(verifyDigits, 'af-verify', $event)">
            <input
              v-for="(d, i) in verifyDigits" :key="i" :id="`af-verify-${i}`"
              class="af-mfa-box" inputmode="numeric" maxlength="1"
              :value="d" @input="codeInput(verifyDigits, 'af-verify', i, $event)" @keydown="codeKey(verifyDigits, 'af-verify', i, $event)"
            />
          </div>
          <p v-if="errorMsg" class="af-error">{{ errorMsg }}</p>
          <button type="button" class="ax-btn2 ax-btn2--accent af-btn-full" :disabled="busy" @click="submitTotpVerify">
            {{ busy ? 'Verifying…' : 'Verify & activate' }}
          </button>
        </template>

        <!-- step 3 · number reveal -->
        <template v-else>
          <div class="af-eyebrow">Step 3 of 3 · You're in</div>
          <h1 class="af-h1">Your affiliate number</h1>
          <div class="af-number">{{ affiliateNumber }}</div>
          <button type="button" class="ax-btn2 ax-btn2--ghost af-copy" @click="copyNumber">{{ copied ? 'Copied ✓' : 'Copy number' }}</button>
          <p class="af-sub" style="margin-top:18px;"><strong>Save this number — it's your login ID</strong> and the referral code your customers enter at NOKVO APEX checkout. Payouts unlock once our team verifies your account and your bank details are in place.</p>
          <button type="button" class="ax-btn2 ax-btn2--accent af-btn-full" @click="enterDashboard">Open my dashboard</button>
        </template>
      </div>
    </div>

    <!-- ============ DASHBOARD ============ -->
    <AffiliateDashboard v-else :me="me" @logout="logout" />
  </div>
</template>

<style scoped>
/* Affiliate-specific surface in the APEX dark idiom (Sora + JetBrains Mono,
   #0A0A0B ground, red accent). The auth-screen classes are scoped copies of
   ApexApp's — those live in ApexApp's own scoped style, not the shared theme. */
.af-root { min-height: 100vh; background: #0A0A0B; color: #F3F2F0; font-family: 'Sora', sans-serif; }
.af-auth { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 40px 20px; position: relative; overflow: hidden; }
.af-glow { position: absolute; width: 560px; height: 560px; border-radius: 50%; background: radial-gradient(circle, rgba(230,38,48,0.14), transparent 65%); top: -180px; right: -120px; pointer-events: none; }
.af-card { position: relative; width: 100%; max-width: 400px; background: linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.012)); border: 1px solid rgba(255,255,255,0.09); border-radius: 18px; padding: 34px 34px 30px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 30px 60px -30px rgba(0,0,0,0.8); }
.af-brand { display: flex; align-items: baseline; gap: 8px; margin-bottom: 22px; }
.af-brand-nokvo { font-weight: 700; font-size: 17px; letter-spacing: 0.02em; }
.af-brand-tag { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.14em; color: #E62630; }
.af-eyebrow { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: rgba(255,255,255,0.4); margin-bottom: 8px; }
.af-h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.015em; margin: 0 0 8px; }
.af-sub { font-size: 13.5px; line-height: 1.6; color: rgba(255,255,255,0.55); margin: 0 0 18px; }
.af-label { display: block; font-size: 12.5px; font-weight: 600; color: rgba(255,255,255,0.7); margin: 0 0 6px; }
.af-req { font-weight: 400; color: rgba(255,255,255,0.38); font-size: 11.5px; }
.af-input { width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.14); border-radius: 10px; color: #F3F2F0; padding: 12px 14px; font-family: 'Sora', sans-serif; font-size: 14px; outline: none; transition: all .18s; color-scheme: dark; }
.af-input:focus { border-color: #E62630; box-shadow: 0 0 0 4px rgba(230,38,48,0.12); }
.af-input--mono { font-family: 'JetBrains Mono', monospace; letter-spacing: 0.08em; }
.af-mfa-row { display: flex; gap: 8px; }
.af-mfa-box { width: 46px; height: 56px; border: 1px solid rgba(255,255,255,0.16); border-radius: 11px; background: rgba(0,0,0,0.22); box-shadow: inset 0 1px 3px rgba(0,0,0,0.25); color: #F3F2F0; text-align: center; font-family: 'JetBrains Mono', monospace; font-size: 22px; outline: none; transition: all .18s; }
.af-mfa-box:focus { border: 1.5px solid #E62630; box-shadow: 0 0 0 4px rgba(230,38,48,0.12); }
.af-error { color: #F0666E; font-size: 13px; margin: 12px 0 0; }
.af-btn-full { display: block; width: 100%; margin-top: 18px; padding: 14px; font-size: 14px; font-weight: 600; text-align: center; }
.af-hint { font-size: 12px; color: rgba(255,255,255,0.35); margin: 14px 0 0; text-align: center; }
.af-switch { font-size: 13px; color: rgba(255,255,255,0.5); margin: 16px 0 0; text-align: center; }
.af-link { color: #E62630; cursor: pointer; font-weight: 600; }
.af-link:hover { text-decoration: underline; }
.af-qr { display: flex; justify-content: center; padding: 16px; background: #ffffff; border-radius: 14px; width: fit-content; margin: 6px auto 12px; }
.af-secret { font-size: 12px; color: rgba(255,255,255,0.45); text-align: center; word-break: break-all; }
.af-secret code { font-family: 'JetBrains Mono', monospace; color: rgba(255,255,255,0.75); }
.af-number { font-family: 'JetBrains Mono', monospace; font-size: 34px; font-weight: 700; letter-spacing: 0.1em; text-align: center; padding: 22px 12px; margin: 8px 0 12px; border: 1px dashed rgba(230,38,48,0.5); border-radius: 14px; background: rgba(230,38,48,0.06); color: #fff; }
.af-copy { display: block; margin: 0 auto; padding: 9px 18px; font-size: 12.5px; }
</style>
