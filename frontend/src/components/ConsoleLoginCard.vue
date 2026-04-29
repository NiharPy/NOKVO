<script setup>
import { ref } from 'vue';
import { Shield, LockKeyhole, Mail, KeyRound } from 'lucide-vue-next';
import axios from 'axios';
import QrcodeVue from 'qrcode.vue';
import SuperAdminDashboard from './SuperAdminDashboard.vue';

const email = ref('');
const password = ref('');
const totpCode = ref('');
const isAuthenticating = ref(false);
const errorMsg = ref('');

// auth states: 'login', 'mfa_setup', 'mfa_verify', 'success'
const authStep = ref('login'); 
const tempToken = ref('');
const totpUri = ref('');
const totpSecret = ref('');

const api = axios.create({
  baseURL: 'http://localhost:8000/api',
});

const handleLogin = async () => {
  if (!email.value || !password.value) return;
  isAuthenticating.value = true;
  errorMsg.value = '';

  try {
    const response = await api.post('/auth/login', {
      email: email.value,
      password: password.value
    });

    const data = response.data;
    
    if (data.refresh_token === 'pending_mfa') {
      tempToken.value = data.access_token;
      await checkMfaSetup();
    } else {
      authStep.value = 'success';
      localStorage.setItem('access_token', data.access_token);
      localStorage.setItem('refresh_token', data.refresh_token);
    }
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || 'Authentication failed';
  } finally {
    isAuthenticating.value = false;
  }
};

const checkMfaSetup = async () => {
  try {
    const res = await api.post('/auth/mfa/totp/setup', {}, {
      headers: { Authorization: `Bearer ${tempToken.value}` }
    });
    // First time setup
    totpUri.value = res.data.uri;
    totpSecret.value = res.data.secret;
    authStep.value = 'mfa_setup';
  } catch (err) {
    if (err.response?.status === 400 && err.response?.data?.detail === 'TOTP already set up') {
      authStep.value = 'mfa_verify';
    } else {
      errorMsg.value = 'Failed to initialize MFA sequence';
    }
  }
};

const handleTotpVerify = async () => {
  if (!totpCode.value || totpCode.value.length < 6) return;
  isAuthenticating.value = true;
  errorMsg.value = '';

  try {
    const response = await api.post('/auth/mfa/totp/verify', {
      token: totpCode.value
    }, {
      headers: { Authorization: `Bearer ${tempToken.value}` }
    });

    const data = response.data;
    authStep.value = 'success';
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
  } catch (err) {
    errorMsg.value = err.response?.data?.detail || 'Invalid MFA Token';
  } finally {
    isAuthenticating.value = false;
  }
};

const resetFlow = () => {
  authStep.value = 'login';
  errorMsg.value = '';
  totpCode.value = '';
  tempToken.value = '';
};

const handleLogout = async () => {
  try {
    const token = localStorage.getItem('access_token');
    if (token) {
      await api.post('/auth/logout', {}, {
        headers: { Authorization: `Bearer ${token}` }
      });
    }
  } catch (err) {
    console.error("Logout error", err);
  } finally {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    resetFlow();
  }
};

import { onMounted } from 'vue';

onMounted(async () => {
  const token = localStorage.getItem('access_token');
  if (token) {
    try {
      await api.get('/auth/me', {
        headers: { Authorization: `Bearer ${token}` }
      });
      authStep.value = 'success';
    } catch (err) {
      localStorage.removeItem('access_token');
      localStorage.removeItem('refresh_token');
    }
  }
});
</script>

<template>
  <SuperAdminDashboard v-if="authStep === 'success'" @logout="handleLogout" />
  <div v-else class="login-card">
    <div class="card-header">
      <div class="shield-container">
        <Shield :size="48" color="var(--text-secondary)" strokeWidth="1" />
        <div class="shield-inner-icon">
          <div class="user-badge" :class="{'badge-success': authStep === 'success'}"></div>
        </div>
      </div>
      <div class="title-group">
        <h2 class="sub-title">NOKVO</h2>
        <h1 class="main-title">ORBITAL-V1 SECURE CONSOLE</h1>
      </div>
    </div>

    <div class="status-box">
      <div class="status-text">
        <div>SECURE ENCRYPTED</div>
        <div>HANDSHAKE ACTIVE</div>
      </div>
      <div class="mfa-badge">
        MFA<br>REQUIRED
      </div>
    </div>

    <div v-if="errorMsg" class="error-box">
      {{ errorMsg }}
    </div>

    <!-- STEP 1: INITIAL LOGIN -->
    <form v-if="authStep === 'login'" @submit.prevent="handleLogin" class="login-form">
      <div class="input-group">
        <label for="system-id">
          <Mail :size="14" class="label-icon" />
          System Identifier
        </label>
        <div class="input-wrapper">
          <input 
            id="system-id" 
            v-model="email" 
            type="email" 
            placeholder="Awaiting input..." 
            autocomplete="email"
            required
          />
          <div class="input-glow"></div>
        </div>
      </div>

      <div class="input-group">
        <label for="access-token">
          <LockKeyhole :size="14" class="label-icon" />
          Access Token
        </label>
        <div class="input-wrapper">
          <input 
            id="access-token" 
            v-model="password" 
            type="password" 
            placeholder="••••••••••••" 
            required
          />
          <div class="input-glow"></div>
        </div>
      </div>

      <button type="submit" class="auth-button" :class="{ 'authenticating': isAuthenticating }" :disabled="isAuthenticating">
        <span class="btn-content">
          <LockKeyhole :size="16" />
          {{ isAuthenticating ? 'VERIFYING...' : 'INITIATE AUTHENTICATION' }}
        </span>
        <div class="scanner"></div>
      </button>
    </form>

    <!-- STEP 2: MFA SETUP -->
    <form v-else-if="authStep === 'mfa_setup'" @submit.prevent="handleTotpVerify" class="login-form mfa-form">
      <div class="mfa-instruction">
        Scan this QR code with your Authenticator app (e.g., Google Authenticator, Authy) to link your device.
      </div>
      <div class="qr-container">
        <qrcode-vue :value="totpUri" :size="150" level="M" background="#ffffff" foreground="#000000" />
      </div>
      <div class="secret-text">
        Manual entry key: <code>{{ totpSecret }}</code>
      </div>

      <div class="input-group">
        <label for="totp-code">
          <KeyRound :size="14" class="label-icon" />
          6-Digit Verification Code
        </label>
        <div class="input-wrapper">
          <input 
            id="totp-code" 
            v-model="totpCode" 
            type="text" 
            placeholder="000000" 
            maxlength="6"
            required
            class="code-input"
          />
          <div class="input-glow"></div>
        </div>
      </div>

      <div class="btn-group">
        <button type="button" class="back-button" @click="resetFlow" :disabled="isAuthenticating">CANCEL</button>
        <button type="submit" class="auth-button mfa-btn" :class="{ 'authenticating': isAuthenticating }" :disabled="isAuthenticating">
          <span class="btn-content">VERIFY & LINK DEVICE</span>
          <div class="scanner"></div>
        </button>
      </div>
    </form>

    <!-- STEP 3: MFA VERIFY -->
    <form v-else-if="authStep === 'mfa_verify'" @submit.prevent="handleTotpVerify" class="login-form mfa-form">
      <div class="mfa-instruction">
        Please enter the 6-digit code from your authenticator app.
      </div>
      <div class="input-group">
        <label for="totp-code-verify">
          <KeyRound :size="14" class="label-icon" />
          Verification Code
        </label>
        <div class="input-wrapper">
          <input 
            id="totp-code-verify" 
            v-model="totpCode" 
            type="text" 
            placeholder="000000" 
            maxlength="6"
            required
            class="code-input"
          />
          <div class="input-glow"></div>
        </div>
      </div>

      <div class="btn-group">
        <button type="button" class="back-button" @click="resetFlow" :disabled="isAuthenticating">BACK</button>
        <button type="submit" class="auth-button mfa-btn" :class="{ 'authenticating': isAuthenticating }" :disabled="isAuthenticating">
          <span class="btn-content">AUTHENTICATE</span>
          <div class="scanner"></div>
        </button>
      </div>
    </form>

    <!-- STEP 4: SUCCESS handled by SuperAdminDashboard -->

    <div v-if="authStep === 'login'" class="emergency-override">
      <a href="#">REQUEST EMERGENCY OVERRIDE</a>
    </div>

    <div class="card-footer">
      <hr class="footer-divider" />
      <p class="footer-text">UNAUTHORIZED ACCESS IS LOGGED AND TRACED.</p>
    </div>
  </div>
</template>

<style scoped>
.login-card {
  background: rgba(17, 24, 39, 0.7);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.05);
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
  width: 100%;
  max-width: 500px;
  padding: 3rem;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}

.login-card::before, .login-card::after {
  content: '';
  position: absolute;
  width: 20px;
  height: 20px;
  border-color: rgba(255, 255, 255, 0.1);
  border-style: solid;
  pointer-events: none;
}
.login-card::before { top: 0; left: 0; border-width: 1px 0 0 1px; }
.login-card::after { bottom: 0; right: 0; border-width: 0 1px 1px 0; }

.card-header {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-bottom: 2.5rem;
}

.shield-container {
  position: relative;
  margin-bottom: 1.5rem;
}

.shield-inner-icon {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  background: var(--bg-dark);
  border-radius: 50%;
  padding: 2px;
}

.user-badge {
  width: 12px;
  height: 12px;
  border: 1.5px solid var(--text-secondary);
  border-radius: 50%;
  position: relative;
  transition: all 0.3s;
}
.user-badge.badge-success {
  border-color: var(--success-color);
  background: var(--success-color);
  box-shadow: 0 0 10px var(--success-color);
}
.user-badge::after {
  content: '';
  position: absolute;
  bottom: -4px;
  left: 50%;
  transform: translateX(-50%);
  width: 18px;
  height: 8px;
  border: 1.5px solid var(--text-secondary);
  border-radius: 10px 10px 0 0;
  border-bottom: 0;
  transition: all 0.3s;
}
.user-badge.badge-success::after {
  border-color: var(--success-color);
}

.title-group { text-align: center; }
.sub-title { font-size: 0.9rem; color: var(--text-secondary); letter-spacing: 2px; font-weight: 500; margin-bottom: 0.5rem; }
.main-title { font-size: 1.2rem; color: var(--text-primary); letter-spacing: 3px; font-weight: 400; }

.status-box {
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(0, 0, 0, 0.2);
  padding: 1rem 1.5rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 2rem;
  position: relative;
  overflow: hidden;
}
.status-box::before {
  content: '';
  position: absolute;
  top: 0; left: 0; width: 2px; height: 100%;
  background: var(--accent-color);
  box-shadow: 0 0 10px var(--accent-glow);
}
.status-text { font-size: 0.85rem; color: var(--text-secondary); letter-spacing: 1px; line-height: 1.4; }
.mfa-badge { border: 1px solid rgba(255, 255, 255, 0.2); padding: 0.4rem 0.8rem; font-size: 0.65rem; letter-spacing: 1px; color: var(--text-secondary); text-align: center; line-height: 1.2; }

.error-box {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid var(--danger-color);
  color: var(--danger-color);
  padding: 0.8rem;
  margin-bottom: 1.5rem;
  font-size: 0.85rem;
  text-align: center;
}

.login-form { display: flex; flex-direction: column; gap: 1.5rem; }
.input-group { display: flex; flex-direction: column; gap: 0.5rem; }
.input-group label { font-size: 0.85rem; color: var(--text-secondary); display: flex; align-items: center; gap: 0.5rem; }
.label-icon { opacity: 0.7; }
.input-wrapper { position: relative; width: 100%; }

input {
  width: 100%;
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  padding: 0.8rem 1rem;
  color: var(--text-primary);
  font-size: 0.95rem;
  outline: none;
  transition: all 0.3s ease;
  position: relative;
  z-index: 2;
}
input::placeholder { color: var(--text-muted); }
input:focus { border-color: var(--border-focus); }

.code-input {
  text-align: center;
  font-size: 1.2rem;
  letter-spacing: 4px;
}

.input-glow {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  box-shadow: 0 0 15px var(--accent-glow);
  opacity: 0;
  transition: opacity 0.3s ease;
  z-index: 1;
  pointer-events: none;
}
input:focus + .input-glow { opacity: 1; }

.auth-button {
  background: rgba(15, 23, 42, 0.8);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 1rem;
  font-size: 0.9rem;
  letter-spacing: 2px;
  font-weight: 500;
  position: relative;
  overflow: hidden;
  transition: all 0.3s ease;
  width: 100%;
}
.btn-content { display: flex; align-items: center; justify-content: center; gap: 0.8rem; position: relative; z-index: 2; }
.auth-button:hover:not(:disabled) {
  background: rgba(30, 41, 59, 0.8);
  border-color: var(--accent-color);
  box-shadow: 0 0 20px rgba(59, 130, 246, 0.2);
}
.auth-button:disabled { opacity: 0.7; cursor: not-allowed; }

.scanner {
  position: absolute;
  top: 0; left: 0; width: 100%; height: 2px;
  background: var(--accent-color);
  box-shadow: 0 0 10px var(--accent-glow);
  opacity: 0;
  z-index: 1;
}
.auth-button:hover:not(:disabled) .scanner { opacity: 0.5; animation: scanline 2s linear infinite; }
.authenticating .btn-content { color: var(--accent-color); text-shadow: 0 0 8px var(--accent-glow); }

/* MFA Specific */
.mfa-form { animation: fadeIn 0.3s ease-out; }
.mfa-instruction { text-align: center; font-size: 0.85rem; color: var(--text-secondary); line-height: 1.5; margin-bottom: 0.5rem; }
.qr-container { display: flex; justify-content: center; margin: 1rem 0; padding: 1rem; background: rgba(255,255,255,0.05); border-radius: 8px; border: 1px solid var(--border-color); }
.secret-text { text-align: center; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 1rem; }
.secret-text code { background: rgba(0,0,0,0.3); padding: 0.2rem 0.4rem; letter-spacing: 1px; color: var(--text-primary); }

.btn-group { display: flex; gap: 1rem; margin-top: 0.5rem; }
.back-button {
  background: transparent;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  padding: 1rem;
  flex: 1;
  font-size: 0.8rem;
  letter-spacing: 1px;
  transition: all 0.3s ease;
}
.back-button:hover:not(:disabled) { background: rgba(255,255,255,0.05); color: var(--text-primary); }
.mfa-btn { flex: 2; margin-top: 0; }

.success-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1rem;
  text-align: center;
  padding: 2rem 0;
  animation: fadeIn 0.5s ease-out;
}
.success-box h3 { color: var(--success-color); letter-spacing: 2px; }
.success-box p { color: var(--text-secondary); font-size: 0.9rem; }
.success-icon {
  animation: pulseSuccess 2s infinite;
}

@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
@keyframes pulseSuccess {
  0% { filter: drop-shadow(0 0 5px var(--success-color)); }
  50% { filter: drop-shadow(0 0 20px var(--success-color)); }
  100% { filter: drop-shadow(0 0 5px var(--success-color)); }
}

.emergency-override { text-align: center; margin-top: 1rem; }
.emergency-override a { font-size: 0.7rem; letter-spacing: 1px; color: var(--text-muted); }
.emergency-override a:hover { color: var(--text-secondary); }
.card-footer { margin-top: 3rem; text-align: center; }
.footer-divider { border: none; border-top: 1px solid rgba(255, 255, 255, 0.1); margin-bottom: 1.5rem; }
.footer-text { font-size: 0.65rem; color: var(--text-muted); letter-spacing: 1px; }
</style>
