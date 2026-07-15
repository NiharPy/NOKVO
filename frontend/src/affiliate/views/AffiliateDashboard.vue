<script setup>
// Affiliate dashboard — commission totals + ledger + referred customers + bank
// details. Settlement is manual (operator NEFT/IMPS within ~2 days of a
// commission becoming due); the KYC banner explains why payouts are blocked
// until the operator's account review + bank details are in place.
import { ref, computed, onMounted } from 'vue';
import { fetchDashboard, saveBankDetails, extractError } from '../affiliateApi.js';

const props = defineProps({ me: { type: Object, default: null } });
const emit = defineEmits(['logout']);

const loading = ref(true);
const errorMsg = ref('');
const dash = ref(null);

const bankForm = ref({ account_holder: '', account_number: '', ifsc: '' });
const bankSaving = ref(false);
const bankError = ref('');
const bankSaved = ref(null); // masked echo after save
const bankEditing = ref(false);

const inr = (n) => '₹' + (Number(n) || 0).toLocaleString('en-IN', { maximumFractionDigits: 2 });
const dt = (s) => (s ? new Date(s).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : '—');

const kycBanner = computed(() => {
  const k = dash.value?.kyc;
  if (!k) return null;
  if (k.settlement_blocked_reason === 'kyc_pending') {
    return { tone: 'wait', text: 'Verification in progress — our team is reviewing your account. Payouts unlock once it\'s verified.' };
  }
  if (k.settlement_blocked_reason === 'bank_details_missing') {
    return { tone: 'warn', text: 'Add your bank details below to receive payouts.' };
  }
  return { tone: 'ok', text: 'You\'re verified — due commissions are settled to your bank within ~2 days.' };
});

async function load() {
  loading.value = true;
  errorMsg.value = '';
  try {
    dash.value = await fetchDashboard();
    if (dash.value?.kyc?.has_bank_details && props.me?.bank) bankSaved.value = props.me.bank;
  } catch (e) {
    errorMsg.value = extractError(e, 'Could not load your dashboard.');
  } finally { loading.value = false; }
}

async function submitBank() {
  bankError.value = '';
  const f = bankForm.value;
  if (!f.account_holder.trim() || !f.account_number.trim() || !f.ifsc.trim()) {
    bankError.value = 'Fill in all three fields.';
    return;
  }
  bankSaving.value = true;
  try {
    const r = await saveBankDetails({
      account_holder: f.account_holder.trim(),
      account_number: f.account_number.trim(),
      ifsc: f.ifsc.trim().toUpperCase(),
    });
    bankSaved.value = r.bank;
    bankEditing.value = false;
    bankForm.value = { account_holder: '', account_number: '', ifsc: '' };
    await load();
  } catch (e) {
    bankError.value = extractError(e, 'Could not save bank details.');
  } finally { bankSaving.value = false; }
}

onMounted(load);
</script>

<template>
  <div class="afd">
    <header class="afd-head">
      <div class="af-brand"><span class="af-brand-nokvo">NOKVO</span><span class="af-brand-tag">AFFILIATE</span></div>
      <div class="afd-id">
        <span class="afd-number">{{ me?.affiliate_number }}</span>
        <span class="afd-name">{{ me?.full_name }}</span>
        <button type="button" class="ax-btn2 ax-btn2--ghost afd-logout" @click="emit('logout')">Sign out</button>
      </div>
    </header>

    <main class="afd-main">
      <p v-if="errorMsg" class="afd-error">{{ errorMsg }}</p>
      <div v-if="loading" class="afd-loading">Loading your dashboard…</div>

      <template v-else-if="dash">
        <div v-if="kycBanner" class="afd-banner" :class="`is-${kycBanner.tone}`">{{ kycBanner.text }}</div>

        <!-- totals -->
        <div class="afd-stats">
          <div class="afd-stat"><span class="afd-stat-val">{{ inr(dash.totals.accrued_rupees) }}</span><span class="afd-stat-lbl">Total earned</span></div>
          <div class="afd-stat"><span class="afd-stat-val">{{ inr(dash.totals.pending_rupees) }}</span><span class="afd-stat-lbl">Pending</span></div>
          <div class="afd-stat"><span class="afd-stat-val">{{ inr(dash.totals.due_rupees) }}</span><span class="afd-stat-lbl">Due for payout</span></div>
          <div class="afd-stat"><span class="afd-stat-val">{{ inr(dash.totals.settled_rupees) }}</span><span class="afd-stat-lbl">Paid out</span></div>
        </div>

        <!-- commissions ledger -->
        <section class="afd-card">
          <h2 class="afd-h2">Commissions</h2>
          <p class="afd-muted">5% of your referral's first month, 2% of every month after. Due commissions are transferred to your bank within ~2 days.</p>
          <table v-if="dash.ledger.length" class="afd-table">
            <thead><tr><th>Date</th><th>Customer</th><th>Type</th><th>Billed</th><th>Commission</th><th>Status</th></tr></thead>
            <tbody>
              <tr v-for="row in dash.ledger" :key="row.id">
                <td>{{ dt(row.created_at) }}</td>
                <td>{{ row.customer }}</td>
                <td><span class="afd-chip" :class="row.commission_type === 'first_month' ? 'is-first' : 'is-rec'">{{ row.commission_type === 'first_month' ? 'First month · 5%' : 'Recurring · 2%' }}</span></td>
                <td>{{ inr(row.billed_rupees) }}</td>
                <td class="afd-strong">{{ inr(row.amount_rupees) }}</td>
                <td><span class="afd-chip" :class="row.status === 'settled' ? 'is-ok' : 'is-wait'">{{ row.status === 'settled' ? 'Paid' : 'Pending' }}</span></td>
              </tr>
            </tbody>
          </table>
          <p v-else class="afd-muted">No commissions yet — share your affiliate number <strong class="afd-mono">{{ me?.affiliate_number }}</strong> with businesses joining NOKVO APEX.</p>
        </section>

        <!-- referred customers -->
        <section class="afd-card">
          <h2 class="afd-h2">Your referrals</h2>
          <table v-if="dash.referred_customers.length" class="afd-table">
            <thead><tr><th>Joined</th><th>Customer</th><th>Status</th><th>Commissions</th><th>Total earned</th></tr></thead>
            <tbody>
              <tr v-for="(c, i) in dash.referred_customers" :key="i">
                <td>{{ dt(c.joined_at) }}</td>
                <td>{{ c.name_masked }}</td>
                <td><span class="afd-chip" :class="c.status === 'active' ? 'is-ok' : (c.status === 'payment_pending' ? 'is-wait' : 'is-off')">{{ c.status === 'payment_pending' ? 'Payment pending' : c.status }}</span></td>
                <td>{{ c.commission_count }}</td>
                <td class="afd-strong">{{ inr(c.total_commission_rupees) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="afd-muted">No referrals yet.</p>
        </section>

        <!-- bank details -->
        <section class="afd-card">
          <h2 class="afd-h2">Bank details</h2>
          <p class="afd-muted">Payouts are sent by bank transfer (NEFT/IMPS). We only ever show a masked account number back to you.</p>
          <div v-if="bankSaved && !bankEditing" class="afd-bank">
            <div><span class="afd-bank-lbl">Account holder</span><span>{{ bankSaved.account_holder }}</span></div>
            <div><span class="afd-bank-lbl">Account number</span><span class="afd-mono">{{ bankSaved.account_number_masked }}</span></div>
            <div><span class="afd-bank-lbl">IFSC</span><span class="afd-mono">{{ bankSaved.ifsc }}</span></div>
            <button type="button" class="ax-btn2 ax-btn2--ghost afd-bank-edit" @click="bankEditing = true">Update details</button>
          </div>
          <div v-else class="afd-bank-form">
            <label class="af-label">Account holder name</label>
            <input v-model="bankForm.account_holder" type="text" class="af-input" placeholder="As per your bank records" />
            <label class="af-label" style="margin-top:12px;">Account number</label>
            <input v-model="bankForm.account_number" type="text" inputmode="numeric" class="af-input afd-mono" placeholder="9–18 digits" />
            <label class="af-label" style="margin-top:12px;">IFSC code</label>
            <input v-model="bankForm.ifsc" type="text" class="af-input afd-mono" placeholder="HDFC0001234" style="text-transform:uppercase;" />
            <p v-if="bankError" class="afd-error">{{ bankError }}</p>
            <div class="afd-bank-actions">
              <button type="button" class="ax-btn2 ax-btn2--accent afd-bank-save" :disabled="bankSaving" @click="submitBank">
                {{ bankSaving ? 'Saving…' : 'Save bank details' }}
              </button>
              <button v-if="bankSaved" type="button" class="ax-btn2 ax-btn2--ghost afd-bank-save" :disabled="bankSaving" @click="bankEditing = false">Cancel</button>
            </div>
          </div>
        </section>
      </template>
    </main>
  </div>
</template>

<style scoped>
.afd { min-height: 100vh; }
.afd-head { display: flex; justify-content: space-between; align-items: center; padding: 22px 34px; border-bottom: 1px solid rgba(255,255,255,0.07); }
.af-brand { display: flex; align-items: baseline; gap: 8px; }
.af-brand-nokvo { font-weight: 700; font-size: 17px; letter-spacing: 0.02em; }
.af-brand-tag { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.14em; color: #E62630; }
.afd-id { display: flex; align-items: center; gap: 14px; }
.afd-number { font-family: 'JetBrains Mono', monospace; font-size: 13px; color: #E62630; letter-spacing: 0.06em; }
.afd-name { font-size: 13.5px; color: rgba(255,255,255,0.6); }
.afd-logout { padding: 8px 16px; font-size: 12.5px; }
.afd-main { max-width: 960px; margin: 0 auto; padding: 30px 24px 60px; }
.afd-loading { color: rgba(255,255,255,0.45); padding: 40px 0; text-align: center; }
.afd-error { color: #F0666E; font-size: 13px; margin: 12px 0; }
.afd-banner { border-radius: 12px; padding: 13px 18px; font-size: 13.5px; margin-bottom: 22px; border: 1px solid; }
.afd-banner.is-wait { color: #D6A15C; border-color: rgba(214,161,92,0.35); background: rgba(214,161,92,0.07); }
.afd-banner.is-warn { color: #F0666E; border-color: rgba(230,38,48,0.35); background: rgba(230,38,48,0.07); }
.afd-banner.is-ok { color: #7FD9A8; border-color: rgba(74,200,140,0.3); background: rgba(74,200,140,0.06); }
.afd-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 26px; }
.afd-stat { display: grid; gap: 4px; padding: 18px 20px; background: linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.015)); border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05); }
.afd-stat-val { font-family: 'JetBrains Mono', monospace; font-size: 22px; font-weight: 700; }
.afd-stat-lbl { font-size: 11.5px; color: rgba(255,255,255,0.42); letter-spacing: 0.03em; text-transform: uppercase; }
.afd-card { border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px 26px; margin-bottom: 22px; background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.005)); }
.afd-h2 { font-size: 18px; font-weight: 600; margin: 0 0 6px; letter-spacing: -0.01em; }
.afd-muted { font-size: 13px; color: rgba(255,255,255,0.45); margin: 0 0 16px; line-height: 1.55; }
.afd-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.afd-table th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: rgba(255,255,255,0.38); padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,0.08); }
.afd-table td { padding: 11px 10px; border-bottom: 1px solid rgba(255,255,255,0.05); color: rgba(255,255,255,0.78); }
.afd-strong { font-family: 'JetBrains Mono', monospace; font-weight: 600; color: #F3F2F0; }
.afd-mono { font-family: 'JetBrains Mono', monospace; }
.afd-chip { display: inline-flex; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: 0.03em; border: 1px solid; }
.afd-chip.is-first { color: #E6A5A8; border-color: rgba(230,38,48,0.4); background: rgba(230,38,48,0.08); }
.afd-chip.is-rec { color: rgba(255,255,255,0.6); border-color: rgba(255,255,255,0.16); background: rgba(255,255,255,0.03); }
.afd-chip.is-ok { color: #7FD9A8; border-color: rgba(74,200,140,0.3); background: rgba(74,200,140,0.06); }
.afd-chip.is-wait { color: #D6A15C; border-color: rgba(214,161,92,0.35); background: rgba(214,161,92,0.07); }
.afd-chip.is-off { color: rgba(255,255,255,0.4); border-color: rgba(255,255,255,0.12); background: rgba(255,255,255,0.02); }
.afd-bank { display: grid; gap: 10px; font-size: 13.5px; }
.afd-bank > div { display: flex; gap: 14px; }
.afd-bank-lbl { width: 150px; color: rgba(255,255,255,0.42); flex: none; }
.afd-bank-edit { width: fit-content; margin-top: 8px; padding: 8px 16px; font-size: 12.5px; }
.afd-bank-form { max-width: 420px; }
.afd-bank-actions { display: flex; gap: 10px; }
.afd-bank-save { margin-top: 16px; padding: 12px 22px; font-size: 13px; font-weight: 600; }
/* form field styles shared with the auth card */
.af-label { display: block; font-size: 12.5px; font-weight: 600; color: rgba(255,255,255,0.7); margin: 0 0 6px; }
.af-input { width: 100%; box-sizing: border-box; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.14); border-radius: 10px; color: #F3F2F0; padding: 12px 14px; font-family: 'Sora', sans-serif; font-size: 14px; outline: none; transition: all .18s; color-scheme: dark; }
.af-input:focus { border-color: #E62630; box-shadow: 0 0 0 4px rgba(230,38,48,0.12); }
@media (max-width: 720px) {
  .afd-head { padding: 18px; flex-wrap: wrap; gap: 10px; }
  .afd-main { padding: 20px 14px 50px; }
  .afd-table { font-size: 12px; }
}
</style>
