<script setup>
import { computed, ref } from 'vue';
import {
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Database,
  FileText,
  Hash,
  Mic,
  Search,
  Shield,
  Trash2,
  Upload,
  XCircle,
  Zap,
} from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const kbUploadInputRef = ref(null);

const {
  kbStats,
  formatBytes,
  kbError,
  kbInfo,
  isAdmin,
  kbDocumentUploadEnabled,
  kbForm,
  kbBulkQueue,
  isUploadingKb,
  isUploadingKbBulk,
  handleKbDrop,
  handleKbFileChange,
  clearKbFile,
  removeBulkQueueItem,
  kbDocumentTypes,
  uploadKnowledgeDocument,
  uploadKnowledgeDocumentsBulk,
  kbSinglePromptConfig,
  formatRelativeDate,
  isDisablingSinglePromptAgent,
  disableSinglePromptVoiceAgent,
  kbSinglePromptForm,
  isSavingSinglePromptAgent,
  kbSinglePromptCanSubmit,
  saveSinglePromptVoiceAgent,
  kbQuery,
  testKnowledgeRetrieval,
  isSearchingKb,
  kbResults,
  kbDocuments,
  kbExpandedDocs,
  kbChunksByDoc,
  toggleKnowledgeChunks,
  approveKnowledgeDocument,
  rejectKnowledgeDocument,
  removeKnowledgeDocument,
  isLoadingKb,
  isReconcilingKb,
  reconcileKnowledgeDocuments,
  loadKnowledgeDocuments,
} = useDashboardState();

const stats = computed(() => kbStats?.value || {});
const documents = computed(() => kbDocuments?.value || []);
const results = computed(() => kbResults?.value || []);
const queue = computed(() => kbBulkQueue?.value || []);

function docStatusTone(s) {
  if (s === 'approved') return 'n-tag--success';
  if (s === 'rejected') return 'n-tag--danger';
  return 'n-tag--warning';
}
</script>

<template>
  <div class="n-page kb">
    <!-- Hero -->
    <header class="kb__hero n-rise">
      <div class="kb__hero-copy">
        <span class="n-page-head__eyebrow">Knowledge base</span>
        <h1 class="n-page-head__title">Documents your agent answers from</h1>
        <p class="n-page-head__sub">
          Uploads land in tenant blob storage, chunk, and embed with
          <strong>text-embedding-3-small</strong> on your dedicated Azure OpenAI
          deployment in <strong>South India</strong>. Only approved documents serve answers.
        </p>
        <div class="kb__hero-pills">
          <span class="n-tag n-tag--brand">
            <span class="n-dot n-dot--success n-dot--pulse"></span>
            Azure OpenAI · South India
          </span>
          <span class="n-tag n-tag--mono">text-embedding-3-small</span>
          <span class="n-tag n-tag--mono">1536-dim · cosine</span>
        </div>
      </div>

      <div class="kb__hero-stats">
        <article class="n-stat">
          <span class="n-stat__label">Documents</span>
          <strong class="n-stat__value">{{ stats.total ?? 0 }}</strong>
          <span class="n-stat__sub">{{ stats.approved ?? 0 }} approved · {{ stats.pending ?? 0 }} pending</span>
        </article>
        <article class="n-stat">
          <span class="n-stat__label">Chunks</span>
          <strong class="n-stat__value">{{ stats.chunks ?? 0 }}</strong>
          <span class="n-stat__sub">{{ stats.vectors ?? 0 }} vectors in Qdrant</span>
        </article>
        <article class="n-stat">
          <span class="n-stat__label">Storage</span>
          <strong class="n-stat__value">{{ formatBytes(stats.bytes) || '—' }}</strong>
          <span class="n-stat__sub">{{ stats.errors ?? 0 }} with errors</span>
        </article>
      </div>
    </header>

    <div v-if="kbError" class="kb__msg kb__msg--error">
      <XCircle :size="14" />
      {{ kbError }}
    </div>
    <div v-else-if="kbInfo" class="kb__msg kb__msg--info">
      <CheckCircle2 :size="14" />
      {{ kbInfo }}
    </div>

    <!-- Workspace grid: upload + single-prompt + retrieval -->
    <section class="n-section n-rise" data-delay="1">
      <div class="kb__workspace">
        <!-- Upload -->
        <article v-if="isAdmin && kbDocumentUploadEnabled" class="n-card kb__upload">
          <header class="kb__card-head">
            <div class="kb__card-icon"><Upload :size="16" /></div>
            <div>
              <h2 class="kb__card-title">Upload document</h2>
              <p class="kb__card-sub">PDF, DOCX, TXT, or Markdown · embeds on save.</p>
            </div>
          </header>

          <label
            class="kb__dropzone"
            :class="{ 'has-file': kbForm.file || queue.length, 'is-busy': isUploadingKb || isUploadingKbBulk }"
            @dragover.prevent
            @drop.prevent="handleKbDrop"
          >
            <input
              ref="kbUploadInputRef"
              class="kb__dropzone-input"
              type="file"
              multiple
              accept=".pdf,.docx,.txt,.md,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,text/markdown"
              @change="handleKbFileChange"
            />
            <div v-if="!kbForm.file && !queue.length" class="kb__dropzone-prompt">
              <span class="kb__dropzone-icon"><Upload :size="20" /></span>
              <strong>Drop files or click to browse</strong>
              <span>PDF, DOCX, TXT, MD · one for a named upload, many for bulk embed</span>
            </div>
            <div v-else-if="kbForm.file && !queue.length" class="kb__dropzone-filled">
              <span class="kb__dropzone-icon"><FileText :size="18" /></span>
              <div>
                <strong class="n-truncate">{{ kbForm.file.name }}</strong>
                <span>{{ formatBytes(kbForm.file.size) }} · {{ kbForm.file.type || 'unknown type' }}</span>
              </div>
              <button type="button" class="n-btn n-btn--quiet n-btn--sm" @click.prevent.stop="clearKbFile">
                Replace
              </button>
            </div>
            <div v-else class="kb__dropzone-filled">
              <span class="kb__dropzone-icon"><FileText :size="18" /></span>
              <div>
                <strong>{{ queue.length }} files queued for bulk embed</strong>
                <span>Tags &amp; type below apply to all. Filenames become document names.</span>
              </div>
              <button type="button" class="n-btn n-btn--quiet n-btn--sm" :disabled="isUploadingKbBulk" @click.prevent.stop="clearKbFile">
                Clear
              </button>
            </div>
          </label>

          <ul v-if="queue.length" class="kb__queue">
            <li v-for="(entry, idx) in queue" :key="entry.file.name + idx" class="kb__queue-row" :class="`is-${entry.status}`">
              <span class="kb__queue-icon"><FileText :size="12" /></span>
              <span class="kb__queue-name n-truncate">{{ entry.name }}</span>
              <span class="kb__queue-size">{{ formatBytes(entry.file.size) }}</span>
              <span class="kb__queue-status">
                <template v-if="entry.status === 'queued'">Queued</template>
                <template v-else-if="entry.status === 'uploading'">Embedding…</template>
                <template v-else-if="entry.status === 'done'">Embedded</template>
                <template v-else>Failed</template>
              </span>
              <button
                v-if="entry.status === 'queued' && !isUploadingKbBulk"
                type="button"
                class="n-btn n-btn--quiet n-btn--sm"
                @click="removeBulkQueueItem(idx)"
              >
                <Trash2 :size="11" />
              </button>
              <div v-if="entry.status === 'error' && entry.error" class="kb__queue-error">
                {{ entry.error }}
              </div>
            </li>
          </ul>

          <div v-if="!queue.length" class="kb__upload-meta">
            <label class="n-field">
              <span class="n-field__label">Name</span>
              <input v-model="kbForm.name" type="text" class="n-input" placeholder="Refund policy v2" />
            </label>
            <label class="n-field">
              <span class="n-field__label">Type</span>
              <select v-model="kbForm.document_type" class="n-select">
                <option v-for="opt in kbDocumentTypes" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </label>
            <label class="n-field kb__upload-meta-wide">
              <span class="n-field__label">Description</span>
              <input v-model="kbForm.description" type="text" class="n-input" placeholder="Optional summary" />
            </label>
            <label class="n-field kb__upload-meta-wide">
              <span class="n-field__label">Tags <span class="n-field__sub">comma separated</span></span>
              <input v-model="kbForm.tags" type="text" class="n-input" placeholder="refunds, returns, escalation" />
            </label>
          </div>

          <div v-else class="kb__upload-meta">
            <label class="n-field">
              <span class="n-field__label">Type <span class="n-field__sub">applies to all</span></span>
              <select v-model="kbForm.document_type" class="n-select" :disabled="isUploadingKbBulk">
                <option v-for="opt in kbDocumentTypes" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
              </select>
            </label>
            <label class="n-field kb__upload-meta-wide">
              <span class="n-field__label">Tags <span class="n-field__sub">applies to all</span></span>
              <input v-model="kbForm.tags" type="text" class="n-input" :disabled="isUploadingKbBulk" />
            </label>
          </div>

          <div class="kb__upload-foot">
            <button
              v-if="!queue.length"
              type="button"
              class="n-btn n-btn--primary"
              :disabled="isUploadingKb || !kbForm.file"
              @click="uploadKnowledgeDocument"
            >
              <Upload :size="14" />
              {{ isUploadingKb ? 'Embedding…' : 'Upload &amp; embed' }}
            </button>
            <button
              v-else
              type="button"
              class="n-btn n-btn--primary"
              :disabled="isUploadingKbBulk || !queue.length"
              @click="uploadKnowledgeDocumentsBulk"
            >
              <Upload :size="14" />
              {{ isUploadingKbBulk ? 'Embedding…' : `Upload ${queue.length} files` }}
            </button>
            <span class="kb__upload-hint">
              <template v-if="isUploadingKb || isUploadingKbBulk">Chunking and calling Azure OpenAI…</template>
              <template v-else-if="queue.length">2 files embed in parallel to stay under rate limits.</template>
              <template v-else>Approved by default if text is extractable.</template>
            </span>
          </div>
        </article>

        <!-- Read-only banner for non-admins -->
        <article v-else-if="!isAdmin" class="n-card kb__readonly">
          <header class="kb__card-head">
            <div class="kb__card-icon"><Shield :size="16" /></div>
            <div>
              <h2 class="kb__card-title">Read-only access</h2>
              <p class="kb__card-sub">Only admins can upload or approve Knowledge Base documents. Ask an admin to add new sources.</p>
            </div>
          </header>
        </article>

        <!-- Single-prompt voice agent -->
        <article v-if="isAdmin" class="n-card kb__single">
          <header class="kb__card-head">
            <div class="kb__card-icon kb__card-icon--accent"><Mic :size="16" /></div>
            <div>
              <h2 class="kb__card-title">Single-prompt voice</h2>
              <p class="kb__card-sub">
                ON, the LLM voices every reply — natural but can paraphrase confirmations.
                OFF, deterministic templates voice slot questions and confirmations — robotic but predictable.
              </p>
            </div>
          </header>

          <div class="kb__tradeoff">
            <div class="kb__tradeoff-tile">
              <strong>ON · LLM voice</strong>
              <ul>
                <li>Natural, persona-driven replies</li>
                <li>Better around objections</li>
                <li>Can paraphrase a confirmation</li>
              </ul>
            </div>
            <div class="kb__tradeoff-tile">
              <strong>OFF · templates</strong>
              <ul>
                <li>Booking confirmation byte-identical</li>
                <li>Slot questions read verbatim</li>
                <li>Less natural — script-like</li>
              </ul>
            </div>
          </div>

          <div v-if="kbSinglePromptConfig?.enabled" class="kb__single-status">
            <CheckCircle2 :size="14" />
            <div>
              <strong>LLM is voicing replies</strong>
              <span>
                <template v-if="kbSinglePromptConfig.updated_at">Updated {{ formatRelativeDate(kbSinglePromptConfig.updated_at) }}</template>
                <template v-else>Runtime prompt enabled</template>
              </span>
            </div>
            <button
              type="button"
              class="n-btn n-btn--ghost n-btn--sm"
              :disabled="isDisablingSinglePromptAgent"
              @click="disableSinglePromptVoiceAgent"
            >
              {{ isDisablingSinglePromptAgent ? 'Turning off…' : 'Turn off' }}
            </button>
          </div>

          <label class="n-field">
            <span class="n-field__label">Single prompt</span>
            <textarea
              v-model="kbSinglePromptForm.prompt"
              class="n-textarea"
              rows="6"
              placeholder="You are a warm phone agent. Greet briefly, answer only from approved KB documents, ask for the missing detail when needed, keep replies under three sentences."
              :disabled="isSavingSinglePromptAgent"
            ></textarea>
          </label>

          <div class="kb__upload-foot">
            <button
              type="button"
              class="n-btn n-btn--primary"
              :disabled="isSavingSinglePromptAgent || !kbSinglePromptCanSubmit"
              @click="saveSinglePromptVoiceAgent"
            >
              <Zap :size="14" />
              {{ isSavingSinglePromptAgent ? 'Saving…' : 'Activate single-prompt voice' }}
            </button>
            <span class="kb__upload-hint">Knowledge still comes from approved KB documents.</span>
          </div>
        </article>

        <!-- Retrieval test -->
        <article class="n-card kb__retrieval">
          <header class="kb__card-head">
            <div class="kb__card-icon"><Search :size="16" /></div>
            <div>
              <h2 class="kb__card-title">Retrieval tester</h2>
              <p class="kb__card-sub">Embed a query and inspect the chunks the agent would surface.</p>
            </div>
          </header>

          <div class="kb__search">
            <Search :size="14" class="kb__search-icon" />
            <input
              v-model="kbQuery"
              type="text"
              class="kb__search-input"
              placeholder="What is the refund window?"
              @keyup.enter="testKnowledgeRetrieval"
            />
            <button
              type="button"
              class="n-btn n-btn--brand n-btn--sm"
              :disabled="isSearchingKb || !kbQuery.trim()"
              @click="testKnowledgeRetrieval"
            >
              {{ isSearchingKb ? 'Searching…' : 'Search' }}
            </button>
          </div>

          <div v-if="results.length" class="kb__results">
            <article v-for="(chunk, idx) in results" :key="chunk.chunk_id" class="kb__result">
              <header>
                <span class="kb__result-rank">#{{ idx + 1 }}</span>
                <strong class="n-truncate">{{ chunk.document_name }}</strong>
                <span class="n-tag n-tag--brand n-tag--mono">{{ chunk.score.toFixed(3) }}</span>
              </header>
              <p>{{ chunk.text.slice(0, 320) }}{{ chunk.text.length > 320 ? '…' : '' }}</p>
            </article>
          </div>
          <div v-else-if="!isSearchingKb && kbQuery" class="kb__results-empty">
            No results — try a different phrasing or upload more documents.
          </div>
        </article>
      </div>
    </section>

    <!-- Documents list -->
    <section class="n-section n-rise" data-delay="2">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Indexed documents</h2>
          <p class="n-section__sub">{{ documents.length }} document{{ documents.length === 1 ? '' : 's' }}. Pending uploads still embed; only approved documents serve answers.</p>
        </div>
        <div class="kb__list-actions">
          <button
            v-if="isAdmin"
            type="button"
            class="n-btn n-btn--ghost n-btn--sm"
            :disabled="isReconcilingKb"
            @click="reconcileKnowledgeDocuments"
            title="Rebuild from chunks already in Qdrant"
          >
            <Database :size="13" />
            {{ isReconcilingKb ? 'Reconciling…' : 'Reconcile from Qdrant' }}
          </button>
          <button
            type="button"
            class="n-btn n-btn--ghost n-btn--sm"
            :disabled="isLoadingKb"
            @click="loadKnowledgeDocuments"
          >
            <Database :size="13" />
            {{ isLoadingKb ? 'Loading…' : 'Refresh' }}
          </button>
        </div>
      </header>

      <div v-if="!documents.length && !isLoadingKb" class="n-empty">
        <div class="n-empty__icon"><BookOpen :size="20" /></div>
        <p class="n-empty__title">No documents yet</p>
        <p class="n-empty__copy">
          Upload your first policy or FAQ to start grounding the agent.
          <template v-if="isAdmin">If you've already uploaded but the list is empty, use <em>Reconcile from Qdrant</em> to rebuild the registry from orphaned embeddings.</template>
        </p>
      </div>

      <ul v-else-if="documents.length" class="kb__doc-list">
        <li
          v-for="doc in documents"
          :key="doc.id"
          class="kb__doc"
          :class="{ 'is-open': kbExpandedDocs[doc.id] }"
        >
          <div class="kb__doc-head">
            <span class="kb__doc-icon"><FileText :size="16" /></span>
            <div class="kb__doc-id">
              <div class="kb__doc-title-row">
                <strong>{{ doc.name }}</strong>
                <span class="n-tag n-tag--mono">{{ doc.document_type }}</span>
                <span class="n-tag" :class="docStatusTone(doc.approval_status)">{{ doc.approval_status }}</span>
              </div>
              <p v-if="doc.description" class="kb__doc-desc">{{ doc.description }}</p>
              <div class="kb__doc-meta">
                <span><Hash :size="11" /> {{ doc.chunk_count }} chunks</span>
                <span><Database :size="11" /> {{ doc.qdrant_point_count }} vectors</span>
                <span v-if="doc.created_at" class="n-mono">added {{ formatRelativeDate(doc.created_at) }}</span>
              </div>
              <div v-if="doc.tags?.length" class="kb__doc-tags">
                <span v-for="tag in doc.tags.slice(0, 4)" :key="tag" class="n-tag n-tag--mono">{{ tag }}</span>
              </div>
              <p v-if="doc.last_error" class="kb__doc-err">{{ doc.last_error }}</p>
            </div>
            <div class="kb__doc-actions">
              <button
                type="button"
                class="n-btn n-btn--ghost n-btn--sm"
                @click="toggleKnowledgeChunks(doc.id)"
                :title="kbExpandedDocs[doc.id] ? 'Hide chunks' : 'View chunks'"
              >
                <component :is="kbExpandedDocs[doc.id] ? ChevronUp : ChevronDown" :size="13" />
                {{ kbExpandedDocs[doc.id] ? 'Hide' : 'Chunks' }}
              </button>
              <template v-if="isAdmin">
                <button
                  v-if="doc.approval_status !== 'approved' && doc.chunk_count > 0"
                  type="button"
                  class="n-btn n-btn--brand n-btn--sm"
                  @click="approveKnowledgeDocument(doc.id)"
                >
                  <CheckCircle2 :size="13" /> Approve
                </button>
                <button
                  v-if="doc.approval_status === 'approved'"
                  type="button"
                  class="n-btn n-btn--ghost n-btn--sm"
                  @click="rejectKnowledgeDocument(doc.id)"
                >
                  <XCircle :size="13" /> Revoke
                </button>
                <button
                  type="button"
                  class="n-btn n-btn--danger n-btn--sm"
                  @click="removeKnowledgeDocument(doc.id, doc.name)"
                >
                  <Trash2 :size="13" />
                </button>
              </template>
            </div>
          </div>

          <div v-if="kbExpandedDocs[doc.id]" class="kb__doc-chunks">
            <div v-if="kbChunksByDoc[doc.id]?.loading" class="kb__doc-chunks-state">Loading chunks…</div>
            <div v-else-if="kbChunksByDoc[doc.id]?.error" class="kb__doc-chunks-state is-error">
              {{ kbChunksByDoc[doc.id].error }}
            </div>
            <div v-else-if="!kbChunksByDoc[doc.id]?.chunks?.length" class="kb__doc-chunks-state">
              No chunks indexed for this document.
            </div>
            <ol v-else class="kb__chunk-list">
              <li v-for="chunk in kbChunksByDoc[doc.id].chunks" :key="chunk.index" class="kb__chunk">
                <header>
                  <span class="kb__chunk-num">#{{ chunk.index + 1 }}</span>
                  <span v-if="chunk.section_title" class="kb__chunk-section">{{ chunk.section_title }}</span>
                  <span class="n-mono kb__chunk-meta">{{ chunk.token_count }} tok · {{ chunk.char_end - chunk.char_start }} chars</span>
                </header>
                <p>{{ chunk.text }}</p>
              </li>
            </ol>
          </div>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
/* ─── Hero ─────────────────────────────────────── */
.kb__hero {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 32px;
  padding: 32px 0 28px;
  border-bottom: 1px solid var(--n-border);
  position: relative;
}
.kb__hero::before {
  content: '';
  position: absolute;
  inset: 0 -20px auto auto;
  width: 400px;
  height: 220px;
  background: radial-gradient(closest-side, var(--n-brand-soft), transparent 80%);
  filter: blur(40px);
  opacity: 0.7;
  z-index: -1;
}
@media (max-width: 1000px) { .kb__hero { grid-template-columns: 1fr; gap: 20px; } }

.kb__hero-copy strong { color: var(--n-text); font-weight: 600; }
.kb__hero-pills { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 16px; }

.kb__hero-stats { display: grid; gap: 10px; }

/* ─── Top-level messages ────────────────────── */
.kb__msg {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 16px;
  border-radius: var(--n-r-lg);
  font-size: 13px;
}
.kb__msg--error {
  background: var(--n-danger-soft);
  border: 1px solid rgba(220, 38, 38, 0.18);
  color: var(--n-danger-2);
}
.kb__msg--info {
  background: var(--n-success-soft);
  border: 1px solid rgba(5, 150, 105, 0.18);
  color: var(--n-success-2);
}

/* ─── Workspace grid ──────────────────────── */
.kb__workspace {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
@media (max-width: 1100px) { .kb__workspace { grid-template-columns: 1fr; } }

.kb__card-head {
  display: flex; align-items: flex-start; gap: 12px;
  margin-bottom: 16px;
}
.kb__card-icon {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: var(--n-surface-2);
  color: var(--n-text-2);
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.kb__card-icon--accent {
  background: var(--n-brand-soft);
  color: var(--n-brand);
}
.kb__card-title {
  margin: 0;
  font-family: var(--n-font-display);
  font-size: 16px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
}
.kb__card-sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--n-text-3);
  line-height: 1.5;
}

/* ─── Upload ──────────────────────────────── */
.kb__upload, .kb__retrieval, .kb__single { display: grid; gap: 14px; }

.kb__dropzone {
  position: relative;
  display: block;
  padding: 24px;
  background: var(--n-surface);
  border: 1.5px dashed var(--n-border-strong);
  border-radius: var(--n-r-lg);
  cursor: pointer;
  transition: background var(--n-t-base) var(--n-ease),
              border-color var(--n-t-base) var(--n-ease);
}
.kb__dropzone:hover { background: var(--n-surface-2); border-color: var(--n-brand); }
.kb__dropzone.has-file { background: var(--n-brand-soft); border-color: rgba(99, 102, 241, 0.3); border-style: solid; }
.kb__dropzone.is-busy { opacity: 0.7; cursor: progress; }
.kb__dropzone-input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}
.kb__dropzone-prompt {
  display: grid;
  justify-items: center;
  gap: 4px;
  text-align: center;
}
.kb__dropzone-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: var(--n-bg);
  color: var(--n-brand);
  display: grid;
  place-items: center;
  margin-bottom: 6px;
  border: 1px solid var(--n-border);
}
.kb__dropzone-prompt strong {
  font-family: var(--n-font-display);
  font-size: 14px;
  color: var(--n-text);
  font-weight: 600;
}
.kb__dropzone-prompt span,
.kb__dropzone-filled span {
  font-size: 12px;
  color: var(--n-text-3);
}
.kb__dropzone-filled {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 12px;
  align-items: center;
}
.kb__dropzone-filled div { display: grid; gap: 2px; min-width: 0; }
.kb__dropzone-filled strong {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--n-text);
  display: block;
}

/* Queue */
.kb__queue { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.kb__queue-row {
  display: grid;
  grid-template-columns: auto 1fr auto auto auto;
  gap: 10px;
  align-items: center;
  padding: 8px 12px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
  font-size: 12.5px;
}
.kb__queue-row.is-done { background: var(--n-success-soft); border-color: rgba(5, 150, 105, 0.18); }
.kb__queue-row.is-error { background: var(--n-danger-soft); border-color: rgba(220, 38, 38, 0.18); }
.kb__queue-icon {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  background: var(--n-bg);
  color: var(--n-text-2);
  display: grid;
  place-items: center;
  border: 1px solid var(--n-border-subtle);
}
.kb__queue-name { color: var(--n-text); font-weight: 500; }
.kb__queue-size { font-family: var(--n-font-mono); font-size: 11px; color: var(--n-text-3); }
.kb__queue-status { font-family: var(--n-font-mono); font-size: 11px; color: var(--n-text-2); }
.kb__queue-row.is-done .kb__queue-status { color: var(--n-success-2); }
.kb__queue-row.is-error .kb__queue-status { color: var(--n-danger-2); }
.kb__queue-error {
  grid-column: 1 / -1;
  font-size: 11.5px;
  color: var(--n-danger-2);
  padding-top: 4px;
}

/* Upload meta */
.kb__upload-meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.kb__upload-meta-wide { grid-column: 1 / -1; }
.kb__upload-foot {
  display: flex; align-items: center; gap: 12px;
  justify-content: space-between;
  flex-wrap: wrap;
}
.kb__upload-hint { font-size: 12px; color: var(--n-text-3); font-style: italic; }

/* Readonly notice */
.kb__readonly { display: grid; gap: 0; }

/* ─── Single-prompt tradeoff ───────────────── */
.kb__tradeoff {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
@media (max-width: 700px) { .kb__tradeoff { grid-template-columns: 1fr; } }
.kb__tradeoff-tile {
  padding: 12px 14px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.kb__tradeoff-tile strong {
  font-family: var(--n-font-display);
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text);
  display: block;
  margin-bottom: 6px;
}
.kb__tradeoff-tile ul {
  margin: 0; padding: 0; list-style: none;
  display: grid; gap: 4px;
  font-size: 12px;
  color: var(--n-text-3);
  line-height: 1.5;
}
.kb__tradeoff-tile li::before {
  content: '·';
  margin-right: 6px;
  color: var(--n-text-4);
}

.kb__single-status {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 12px;
  align-items: center;
  padding: 12px 14px;
  background: var(--n-success-soft);
  border: 1px solid rgba(5, 150, 105, 0.18);
  border-radius: var(--n-r-md);
  color: var(--n-success-2);
}
.kb__single-status > svg { color: var(--n-success); }
.kb__single-status strong {
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text);
  display: block;
}
.kb__single-status span {
  font-size: 11.5px;
  color: var(--n-text-3);
}

/* ─── Retrieval ───────────────────────────── */
.kb__search {
  display: flex; align-items: center; gap: 8px;
  padding: 4px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
}
.kb__search-icon { color: var(--n-text-3); margin-left: 8px; flex-shrink: 0; }
.kb__search-input {
  appearance: none;
  border: 0;
  background: transparent;
  flex: 1;
  padding: 8px 0;
  font-family: var(--n-font-body);
  font-size: 13.5px;
  color: var(--n-text);
  outline: none;
  min-width: 0;
}
.kb__search-input::placeholder { color: var(--n-text-4); }

.kb__results { display: grid; gap: 8px; max-height: 320px; overflow-y: auto; }
.kb__result {
  padding: 10px 12px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.kb__result header {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 4px;
}
.kb__result-rank {
  font-family: var(--n-font-mono);
  font-size: 11px;
  color: var(--n-text-3);
}
.kb__result header strong {
  flex: 1;
  font-size: 13px;
  color: var(--n-text);
  font-weight: 600;
}
.kb__result p {
  margin: 0;
  font-size: 12.5px;
  color: var(--n-text-2);
  line-height: 1.5;
}
.kb__results-empty {
  padding: 20px;
  text-align: center;
  color: var(--n-text-3);
  font-size: 13px;
  font-style: italic;
}

/* ─── Documents list ─────────────────────── */
.kb__list-actions { display: flex; gap: 8px; }
.kb__doc-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.kb__doc {
  background: var(--n-bg-elev);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-xl);
  overflow: hidden;
  transition: border-color var(--n-t-fast) var(--n-ease);
}
.kb__doc.is-open { border-color: var(--n-border-strong); }
.kb__doc-head {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 14px;
  padding: 16px 20px;
  align-items: flex-start;
}
.kb__doc-icon {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: var(--n-brand-soft);
  color: var(--n-brand);
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.kb__doc-id { display: grid; gap: 6px; min-width: 0; }
.kb__doc-title-row {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.kb__doc-title-row strong {
  font-family: var(--n-font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.005em;
}
.kb__doc-desc {
  margin: 0;
  font-size: 12.5px;
  color: var(--n-text-3);
  line-height: 1.5;
}
.kb__doc-meta {
  display: flex; gap: 14px; flex-wrap: wrap;
  font-size: 11.5px;
  color: var(--n-text-3);
}
.kb__doc-meta span { display: inline-flex; align-items: center; gap: 4px; }
.kb__doc-tags { display: flex; gap: 4px; flex-wrap: wrap; }
.kb__doc-err {
  margin: 4px 0 0;
  font-size: 11.5px;
  color: var(--n-danger-2);
  font-family: var(--n-font-mono);
}
.kb__doc-actions {
  display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end;
}

.kb__doc-chunks {
  padding: 8px 20px 20px;
  background: var(--n-surface);
  border-top: 1px solid var(--n-border-subtle);
}
.kb__doc-chunks-state {
  padding: 16px;
  text-align: center;
  color: var(--n-text-3);
  font-size: 12.5px;
}
.kb__doc-chunks-state.is-error { color: var(--n-danger-2); }
.kb__chunk-list { list-style: none; margin: 0; padding: 8px 0; display: grid; gap: 8px; counter-reset: chunk; }
.kb__chunk {
  padding: 10px 12px;
  background: var(--n-bg);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.kb__chunk header {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-bottom: 4px;
}
.kb__chunk-num {
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  color: var(--n-brand);
  font-weight: 600;
}
.kb__chunk-section {
  font-size: 12px;
  color: var(--n-text-2);
  font-weight: 500;
}
.kb__chunk-meta { font-size: 10.5px; color: var(--n-text-4); letter-spacing: 0.04em; }
.kb__chunk p { margin: 0; font-size: 12.5px; color: var(--n-text-2); line-height: 1.55; white-space: pre-wrap; }
</style>
