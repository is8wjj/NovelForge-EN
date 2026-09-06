<template>
  <div class="auto-novel" data-testid="autonomous-novel">
    <section class="hero">
      <div>
        <h1>{{ t('autonomous.title') }}</h1>
        <p class="subtitle">{{ t('autonomous.subtitle') }}</p>
      </div>
      <div class="hero-actions">
        <el-select v-if="auto.jobs.value.length" :model-value="auto.job.value?.id" :placeholder="t('autonomous.previousJobs')" size="default" class="job-select" @change="(id: number) => auto.open(id)">
          <el-option v-for="j in auto.jobs.value" :key="j.id" :value="j.id" :label="`#${j.id} · ${j.source_filename} · ${j.status}`" />
        </el-select>
        <el-button v-if="auto.job.value" @click="auto.reset()">{{ t('autonomous.newRun') }}</el-button>
      </div>
    </section>

    <el-steps :active="stepIndex" finish-status="success" align-center class="steps">
      <el-step :title="t('autonomous.steps.upload')" />
      <el-step :title="t('autonomous.steps.analysis')" />
      <el-step :title="t('autonomous.steps.choose')" />
      <el-step :title="t('autonomous.steps.generating')" />
      <el-step :title="t('autonomous.steps.finished')" />
    </el-steps>

    <el-alert v-if="auto.error.value" type="error" :closable="true" show-icon :title="auto.error.value" class="top-alert" @close="auto.error.value = null" />

    <!-- Screen 1: Upload -->
    <div v-if="auto.screen.value === 'upload'" class="screen" data-testid="screen-upload">
      <label class="drop" :class="{ active: dragging }" @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onDrop">
        <input ref="fileInput" type="file" accept=".epub,.txt,.md,.markdown,.docx" class="hidden-input" @change="onPick" />
        <div v-if="!auto.file.value" class="drop-text">{{ t('autonomous.dropFile') }}</div>
        <div v-else class="drop-text"><b>{{ auto.file.value.name }}</b> · {{ (auto.file.value.size / 1024).toFixed(0) }} KB</div>
      </label>
      <el-form label-position="top" size="default" class="form">
        <div class="grid">
          <el-form-item :label="t('autonomous.model')" required>
            <el-select v-model="form.llm_config_id" :placeholder="t('autonomous.modelPlaceholder')" data-testid="model-select">
              <el-option v-for="llm in llmConfigs" :key="llm.id" :label="`${llm.display_name || llm.model_name} (${llm.provider})`" :value="Number(llm.id)" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('autonomous.mode')">
            <el-radio-group v-model="form.mode">
              <el-radio-button value="fully_automatic">{{ t('autonomous.modes.fully_automatic') }}</el-radio-button>
              <el-radio-button value="approval_gates">{{ t('autonomous.modes.approval_gates') }}</el-radio-button>
              <el-radio-button value="manual">{{ t('autonomous.modes.manual') }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="t('autonomous.qualityPreset')">
            <el-radio-group v-model="form.quality_preset">
              <el-radio-button value="economy">{{ t('autonomous.presets.economy') }}</el-radio-button>
              <el-radio-button value="balanced">{{ t('autonomous.presets.balanced') }}</el-radio-button>
              <el-radio-button value="quality">{{ t('autonomous.presets.quality') }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
        </div>
        <el-collapse class="prefs">
          <el-collapse-item :title="t('autonomous.preferences')">
            <div class="grid">
              <el-form-item :label="t('autonomous.pref.genre')"><el-input v-model="form.genre" /></el-form-item>
              <el-form-item :label="t('autonomous.pref.genre_intensity')"><el-select v-model="form.genre_intensity" clearable><el-option v-for="v in ['subtle', 'moderate', 'intense']" :key="v" :value="v" :label="v" /></el-select></el-form-item>
              <el-form-item :label="t('autonomous.pref.content_rating')"><el-select v-model="form.content_rating" clearable><el-option v-for="v in ['all ages', 'teen', 'mature']" :key="v" :value="v" :label="v" /></el-select></el-form-item>
              <el-form-item :label="t('autonomous.pref.ending_preference')"><el-select v-model="form.ending_preference" clearable><el-option v-for="v in ['triumphant', 'bittersweet', 'tragic', 'open', 'no preference']" :key="v" :value="v" :label="v" /></el-select></el-form-item>
              <el-form-item :label="t('autonomous.pref.romance_level')"><el-select v-model="form.romance_level" clearable><el-option v-for="v in ['none', 'subplot', 'central']" :key="v" :value="v" :label="v" /></el-select></el-form-item>
              <el-form-item :label="t('autonomous.pref.words_per_chapter')"><el-input-number v-model="form.words_per_chapter" :min="300" :max="20000" :step="100" /></el-form-item>
              <el-form-item :label="t('autonomous.pref.storyline_count')"><el-input-number v-model="form.storyline_count" :min="5" :max="10" /></el-form-item>
              <el-form-item :label="t('autonomous.pref.title')"><el-input v-model="form.title" /></el-form-item>
              <el-form-item :label="t('autonomous.pref.author')"><el-input v-model="form.author" /></el-form-item>
            </div>
            <el-form-item :label="t('autonomous.pref.notes')"><el-input v-model="form.notes" type="textarea" :rows="2" /></el-form-item>
          </el-collapse-item>
        </el-collapse>
      </el-form>
      <p class="muted">{{ t('autonomous.rightsNotice') }}</p>
      <el-collapse class="prefs">
        <el-collapse-item :title="t('autonomous.budget.title')" data-testid="budget-panel">
          <div class="grid">
            <el-form-item :label="t('autonomous.budget.max_calls')"><el-input-number v-model="budget.max_calls" :min="0" :step="10" /></el-form-item>
            <el-form-item :label="t('autonomous.budget.max_total_tokens')"><el-input-number v-model="budget.max_total_tokens" :min="0" :step="10000" /></el-form-item>
            <el-form-item :label="t('autonomous.budget.max_output_tokens')"><el-input-number v-model="budget.max_output_tokens" :min="0" :step="10000" /></el-form-item>
            <el-form-item :label="t('autonomous.budget.max_input_tokens')"><el-input-number v-model="budget.max_input_tokens" :min="0" :step="10000" /></el-form-item>
            <el-form-item :label="t('autonomous.budget.max_repair_calls')"><el-input-number v-model="budget.max_repair_calls" :min="0" /></el-form-item>
            <el-form-item :label="t('autonomous.budget.max_cost_usd')"><el-input-number v-model="budget.max_cost_usd" :min="0" :step="1" :precision="2" /></el-form-item>
            <el-form-item :label="t('autonomous.budget.price_input')"><el-input-number v-model="budget.price_input" :min="0" :step="0.1" :precision="3" /></el-form-item>
            <el-form-item :label="t('autonomous.budget.price_output')"><el-input-number v-model="budget.price_output" :min="0" :step="0.1" :precision="3" /></el-form-item>
          </div>
          <el-alert v-if="budget.max_cost_usd > 0 && !(budget.price_input > 0 || budget.price_output > 0)" type="warning" :closable="false" show-icon :title="t('autonomous.budget.priceRequired')" />
        </el-collapse-item>
      </el-collapse>
      <el-card shadow="never" class="preflight" data-testid="preflight-panel">
        <template #header>
          <div class="head-row">
            <b>{{ t('autonomous.preflight.title') }}</b>
            <el-button size="small" :disabled="!form.llm_config_id || !!auto.busy.value" :loading="auto.busy.value === 'preflight'" data-testid="preflight-btn" @click="runPreflight">{{ t('autonomous.preflight.run') }}</el-button>
          </div>
        </template>
        <template v-if="auto.preflight.value">
          <el-alert v-if="auto.preflight.value.passed" type="success" :closable="false" show-icon data-testid="preflight-result" :title="t('autonomous.preflight.passed', { provider: auto.preflight.value.provider, model: auto.preflight.value.model, endpoint: auto.preflight.value.endpoint_class, latency: auto.preflight.value.latency_ms })" />
          <el-alert v-else type="error" :closable="false" show-icon data-testid="preflight-result" :title="t('autonomous.preflight.failed', { category: auto.preflight.value.failure_category || 'unknown', diagnostic: auto.preflight.value.diagnostic || '' })" />
          <ul class="kv checks">
            <li v-for="c in auto.preflight.value.checks.filter((x) => x.name !== 'usage_metadata')" :key="c.name">
              <span>{{ t('autonomous.preflight.check.' + c.name, c.name) }}</span>
              <el-tag size="small" effect="plain" :type="c.skipped ? 'info' : c.passed ? 'success' : c.advisory ? 'warning' : 'danger'">{{ c.skipped ? t('autonomous.preflight.skipped') : c.passed ? 'ok' : c.category || 'failed' }}</el-tag>
            </li>
            <li><span>{{ t('autonomous.preflight.check.usage_metadata') }}</span><span class="muted">{{ t('autonomous.preflight.usage.' + auto.preflight.value.usage_reporting) }}</span></li>
            <li v-if="auto.preflight.value.fallback"><span>{{ t('autonomous.preflight.fallback') }}</span><el-tag size="small" effect="plain" :type="auto.preflight.value.fallback.passed ? 'success' : 'danger'">{{ auto.preflight.value.fallback.model }}</el-tag></li>
          </ul>
          <el-alert v-for="(w, i) in auto.preflight.value.warnings" :key="i" type="warning" :closable="false" show-icon :title="w" class="warning" data-testid="preflight-warning" />
        </template>
        <p v-else class="muted">{{ t('autonomous.preflight.required') }}</p>
      </el-card>
      <div class="actions">
        <el-checkbox v-if="!auto.preflight.value?.passed" v-model="preflightAcknowledged" data-testid="preflight-ack">{{ t('autonomous.preflight.acknowledge') }}</el-checkbox>
        <el-button type="primary" size="large" :disabled="!canStart" :loading="auto.busy.value === 'start'" data-testid="start-btn" @click="start">{{ t('autonomous.startAnalysis') }}</el-button>
      </div>
    </div>

    <!-- Screen 2: Analysis progress -->
    <div v-else-if="auto.screen.value === 'analysis'" class="screen" data-testid="screen-analysis">
      <JobProgressCard :job="auto.job.value!" :active="auto.isActive.value" :stages="ANALYSIS_STAGES" :busy="auto.busy.value" @pause="auto.action('pause')" @resume="auto.action('resume')" @cancel="auto.action('cancel')" @refresh="auto.refresh()" />
      <el-alert v-if="ingestion" :type="ingestion.ok ? 'success' : 'warning'" :closable="false" show-icon class="top-alert" :title="t('autonomous.ingestion', { chapters: ingestion.detected_chapter_count, words: Number(ingestion.extracted_word_count).toLocaleString(), fraction: Math.round(ingestion.story_content_fraction * 100), confidence: Math.round(ingestion.chapter_boundary_confidence * 100) })" />
    </div>

    <!-- Screen 3: Choose a story -->
    <div v-else-if="auto.screen.value === 'choose'" class="screen" data-testid="screen-choose">
      <div v-if="auto.job.value?.waiting_for === 'manual_mode'" class="manual-hint">
        <el-alert type="info" :closable="false" show-icon :title="t('autonomous.manualMode')" />
      </div>
      <template v-else>
        <div class="choose-head">
          <h2>{{ t('autonomous.chooseTitle') }}</h2>
          <el-switch v-model="showRejected" :active-text="t('autonomous.showRejected')" />
        </div>
        <div class="cards">
          <el-card v-for="o in visibleOptions" :key="o.id" shadow="hover" class="story-card" :class="{ selected: auto.selectedStorylineId.value === o.id, rejected: o.rejected }" :data-testid="`story-${o.id}`" @click="!o.rejected && (auto.selectedStorylineId.value = o.id)">
            <div class="story-head">
              <h3>{{ o.title }}</h3>
              <div class="badges">
                <el-tag size="small" effect="plain">{{ o.content.genre || '—' }}</el-tag>
                <el-tag size="small" :type="o.originality_score >= 0.9 ? 'success' : 'warning'" effect="dark">{{ t('autonomous.originality', { pct: Math.round(o.originality_score * 100) }) }}</el-tag>
                <el-tag v-if="o.rejected" size="small" type="danger" effect="dark">{{ t('autonomous.rejected') }}</el-tag>
              </div>
            </div>
            <p class="hook">{{ o.content.hook }}</p>
            <p v-if="o.rejected" class="muted">{{ o.rejection_reason }}</p>
            <div class="facts">
              <span><b>{{ t('autonomous.card.protagonist') }}:</b> {{ o.content.protagonist }}</span>
              <span><b>{{ t('autonomous.card.setting') }}:</b> {{ o.content.setting }}</span>
              <span><b>{{ t('autonomous.card.tone') }}:</b> {{ o.content.tone }} · {{ o.content.pov_plan }}</span>
              <span><b>{{ t('autonomous.card.ending') }}:</b> {{ o.content.ending_type }}</span>
              <span><b>{{ t('autonomous.card.chapters') }}:</b> {{ o.recommended_chapters_min }}–{{ o.recommended_chapters_max }}</span>
            </div>
            <el-collapse class="synopsis" @click.stop>
              <el-collapse-item :title="t('autonomous.card.synopsis')">
                <p>{{ o.content.premise }}</p>
                <h4>{{ t('autonomous.card.acts') }}</h4>
                <ol><li v-for="(a, i) in o.content.acts || []" :key="i"><b>{{ a.act }}:</b> {{ a.summary }}</li></ol>
                <h4>{{ t('autonomous.card.cast') }}</h4>
                <ul><li v-for="(c, i) in o.content.main_cast || []" :key="i">{{ c }}</li></ul>
                <p><b>{{ t('autonomous.card.mystery') }}:</b> {{ o.content.central_mystery }}</p>
                <p><b>{{ t('autonomous.card.fingerprint') }}:</b> {{ o.content.fingerprint_usage }}</p>
                <p class="muted">{{ t('autonomous.card.similarity') }}: {{ Object.entries(o.similarity_to_others).map(([k, v]) => `#${k} ${Math.round(Number(v) * 100)}%`).join(' · ') }}</p>
              </el-collapse-item>
            </el-collapse>
          </el-card>
        </div>
        <el-card shadow="never" class="select-bar">
          <div class="select-row">
            <div>
              <div class="muted">{{ t('autonomous.selected') }}</div>
              <b>{{ auto.selectedOption.value?.title || t('autonomous.noneSelected') }}</b>
            </div>
            <el-form-item :label="t('autonomous.chapterCount')" class="count">
              <el-input-number v-model="auto.chapterCount.value" :min="1" :max="400" data-testid="chapter-count" />
            </el-form-item>
            <div class="muted estimate">
              <div v-if="auto.recommendedRange.value">{{ t('autonomous.recommended', { min: auto.recommendedRange.value[0], max: auto.recommendedRange.value[1] }) }}</div>
              <div>{{ t('autonomous.estimatedWords', { words: auto.estimatedWords.value.toLocaleString() }) }}</div>
              <div v-if="auto.chapterCountWarning.value" class="warn">{{ t('autonomous.countWarning.' + auto.chapterCountWarning.value) }}</div>
            </div>
            <el-button type="primary" size="large" :disabled="!auto.canSelect.value" :loading="auto.busy.value === 'select'" data-testid="generate-btn" @click="auto.confirmSelection()">{{ t('autonomous.generateNovel') }}</el-button>
          </div>
        </el-card>
      </template>
    </div>

    <!-- Screen 4: Novel generation -->
    <div v-else-if="auto.screen.value === 'generating'" class="screen" data-testid="screen-generating">
      <JobProgressCard :job="auto.job.value!" :active="auto.isActive.value" :stages="GENERATION_STAGES" :busy="auto.busy.value" @pause="auto.action('pause')" @resume="auto.action('resume')" @cancel="auto.action('cancel')" @approve="auto.action('approve')" @refresh="auto.refresh()" />
      <el-card shadow="never" class="chapters">
        <template #header><b>{{ t('autonomous.chapters', { done: auto.job.value?.chapters_committed || 0, total: auto.job.value?.chapter_count || 0 }) }}</b></template>
        <el-empty v-if="!auto.chapters.value.length" :description="t('autonomous.noChaptersYet')" :image-size="60" />
        <el-collapse v-else>
          <el-collapse-item v-for="c in auto.chapters.value" :key="c.chapter_number" :name="c.chapter_number">
            <template #title>
              <span class="ch-title">{{ c.chapter_number }}. {{ c.title }}</span>
              <el-tag size="small" effect="plain" class="ch-tag">{{ c.words }} w</el-tag>
              <el-tag size="small" :type="c.validation_passed ? 'success' : 'warning'" effect="plain" class="ch-tag">{{ c.sync_status }}</el-tag>
            </template>
            <p class="muted">{{ c.summary }}</p>
            <pre class="preview">{{ c.preview }}</pre>
          </el-collapse-item>
        </el-collapse>
      </el-card>
    </div>

    <!-- Screen 5: Finished -->
    <div v-else class="screen" data-testid="screen-finished">
      <el-result :icon="qualityIcon" :title="t('autonomous.quality.' + (auto.job.value?.quality_status || 'completed'))" :sub-title="t('autonomous.finishedSubtitle', { chapters: auto.job.value?.chapter_count || 0, words: Number(auto.report.value?.audit?.words || 0).toLocaleString() })" data-testid="quality-status" />
      <div class="downloads">
        <a v-for="a in auto.artifacts.value" :key="a.id" :href="artifactDownloadUrl(a.id, auto.job.value?.id)" class="download" :data-testid="`download-${a.kind}`" download>
          <el-button type="primary" plain>{{ t('autonomous.kinds.' + a.kind, a.kind) }} · {{ (a.size_bytes / 1024).toFixed(0) }} KB</el-button>
        </a>
      </div>
      <div class="grid summary" v-if="auto.report.value">
        <el-card shadow="never">
          <template #header><b>{{ t('autonomous.qualitySummary') }}</b></template>
          <ul class="kv">
            <li><span>{{ t('autonomous.q.blocking') }}</span><b>{{ auto.report.value.audit?.blocking ?? 0 }}</b></li>
            <li v-for="(n, k) in auto.report.value.audit?.counts || {}" :key="k"><span>{{ k }}</span><b>{{ n }}</b></li>
            <li><span>{{ t('autonomous.q.repairs') }}</span><b>{{ Object.values(auto.job.value?.stage_results?.CHAPTER_GENERATION_LOOP || {}).reduce((s: number, c: any) => s + Number(c.repair_attempts || 0), 0) }}</b></li>
          </ul>
        </el-card>
        <el-card shadow="never">
          <template #header><b>{{ t('autonomous.originalitySummary') }}</b></template>
          <ul class="kv">
            <li><span>{{ t('autonomous.o.passed') }}</span><b>{{ auto.report.value.audit?.originality?.passed ? t('common.yes', 'yes') : t('common.no', 'no') }}</b></li>
            <li v-for="(v, k) in auto.report.value.audit?.originality?.scores || {}" :key="k"><span>{{ k }}</span><b>{{ v }}</b></li>
          </ul>
        </el-card>
        <el-card shadow="never">
          <template #header><b>{{ t('autonomous.runSummary') }}</b></template>
          <ul class="kv">
            <li><span>{{ t('autonomous.r.calls') }}</span><b>{{ auto.report.value.run?.model_calls }}</b></li>
            <li><span>{{ t('autonomous.r.input') }}</span><b>{{ Number(auto.report.value.run?.input_tokens || 0).toLocaleString() }}</b></li>
            <li><span>{{ t('autonomous.r.output') }}</span><b>{{ Number(auto.report.value.run?.output_tokens || 0).toLocaleString() }}</b></li>
            <li><span>{{ t('autonomous.r.warnings') }}</span><b>{{ (auto.job.value?.warnings || []).length }}</b></li>
          </ul>
        </el-card>
      </div>
      <el-collapse v-if="auto.report.value?.findings?.length" class="findings">
        <el-collapse-item :title="t('autonomous.findings', { n: auto.report.value.findings.length })">
          <div v-for="(f, i) in auto.report.value.findings" :key="i" class="finding">
            <el-tag size="small" :type="f.severity === 'critical' || f.severity === 'high' ? 'danger' : f.severity === 'medium' ? 'warning' : 'info'" effect="plain">{{ f.severity }}</el-tag>
            <span class="finding-kind">{{ f.kind }}</span>
            <span>{{ f.message }}</span>
          </div>
        </el-collapse-item>
      </el-collapse>
      <div class="actions">
        <el-button v-if="auto.job.value?.original_project_id" type="primary" plain @click="emit('open-project', auto.job.value!.original_project_id!)">{{ t('autonomous.openProject') }}</el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import * as api from '@renderer/api/autonomous'
import { artifactDownloadUrl } from '@renderer/api/autonomous'
import { fileToBase64 } from '@renderer/api/lab'
import { listLLMConfigs, type LLMConfigRead } from '@renderer/api/setting'
import { ANALYSIS_STAGES, GENERATION_STAGES, useAutonomousNovel } from '@renderer/composables/useAutonomousNovel'
import JobProgressCard from '@renderer/components/autonomous/JobProgressCard.vue'

const emit = defineEmits<{ (e: 'open-project', projectId: number): void }>()
const { t } = useI18n()
const auto = useAutonomousNovel({ ...api, fileToBase64 })
const llmConfigs = ref<LLMConfigRead[]>([])
const dragging = ref(false)
const showRejected = ref(false)
const fileInput = ref<HTMLInputElement>()
const form = reactive<{ llm_config_id: number | undefined; mode: api.AutonomousMode; quality_preset: 'economy' | 'balanced' | 'quality'; genre: string; genre_intensity: string; content_rating: string; ending_preference: string; romance_level: string; words_per_chapter: number | undefined; storyline_count: number; title: string; author: string; notes: string }>({
  llm_config_id: undefined, mode: 'fully_automatic', quality_preset: 'balanced', genre: '', genre_intensity: '', content_rating: '', ending_preference: '', romance_level: '', words_per_chapter: undefined, storyline_count: 7, title: '', author: '', notes: '',
})

const stepIndex = computed(() => ['upload', 'analysis', 'choose', 'generating', 'finished'].indexOf(auto.screen.value))
const ingestion = computed(() => auto.job.value?.stage_results?.INGEST?.quality)
const visibleOptions = computed(() => (showRejected.value ? auto.storylines.value : auto.acceptedOptions.value))
const budget = reactive({ max_calls: 0, max_total_tokens: 0, max_output_tokens: 0, max_input_tokens: 0, max_repair_calls: 0, max_cost_usd: 0, price_input: 0, price_output: 0 })
const preflightAcknowledged = ref(false)
const canStart = computed(() => !!auto.file.value && !!form.llm_config_id && !auto.busy.value && (auto.preflight.value?.passed === true || preflightAcknowledged.value))
const qualityIcon = computed<'success' | 'warning' | 'error'>(() => {
  const q = auto.job.value?.quality_status
  if (q === 'quality_gate_failed') return 'error'
  if (q === 'completed_with_warnings' || q === 'manual_review_required') return 'warning'
  return 'success'
})

function budgetSpec(): api.BudgetSpec | undefined {
  const spec: api.BudgetSpec = {}
  for (const k of ['max_calls', 'max_total_tokens', 'max_output_tokens', 'max_input_tokens', 'max_repair_calls', 'max_cost_usd'] as const) {
    if (budget[k] > 0) spec[k] = budget[k]
  }
  if (budget.price_input > 0 || budget.price_output > 0) spec.price_per_million = { input: budget.price_input, output: budget.price_output }
  return Object.keys(spec).length ? spec : undefined
}
async function runPreflight(): Promise<void> {
  if (!form.llm_config_id) return
  await auto.runPreflight({ llm_config_id: form.llm_config_id, timeout_seconds: 45 })
}

function onPick(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0]
  if (f) void auto.pickFile(f)
}
function onDrop(e: DragEvent) {
  dragging.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f) void auto.pickFile(f)
}
async function start() {
  if (!form.llm_config_id || !canStart.value) return
  const params: Record<string, unknown> = { llm_config_id: form.llm_config_id, mode: form.mode, quality_preset: form.quality_preset, storyline_count: form.storyline_count, preflight_acknowledged: auto.preflight.value?.passed !== true }
  const spec = budgetSpec()
  if (spec) params.budget = spec
  for (const k of ['genre', 'genre_intensity', 'content_rating', 'ending_preference', 'romance_level', 'words_per_chapter', 'title', 'author', 'notes'] as const) {
    if (form[k]) params[k] = form[k]
  }
  await auto.start(params as Omit<api.CreateJobRequest, 'filename' | 'content_base64'>)
}

onMounted(async () => {
  try {
    llmConfigs.value = await listLLMConfigs()
    const kimi = llmConfigs.value.find((c) => /kimi/i.test(c.model_name || '') || /authnd/i.test(c.provider || ''))
    form.llm_config_id = Number((kimi || llmConfigs.value[0])?.id) || undefined
  } catch { /* shown as empty select */ }
  await auto.loadJobs()
})
</script>

<style scoped>
.auto-novel { padding: 24px 32px; max-width: 1200px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
.hero { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.hero h1 { margin: 0 0 4px; font-size: 24px; }
.subtitle, .muted { color: var(--el-text-color-secondary); font-size: 13px; }
.hero-actions { display: flex; gap: 8px; align-items: center; }
.job-select { width: 320px; }
.steps { margin: 4px 0; }
.screen { display: flex; flex-direction: column; gap: 14px; }
.drop { display: flex; align-items: center; justify-content: center; min-height: 120px; border: 2px dashed var(--el-border-color); border-radius: 10px; cursor: pointer; background: var(--el-fill-color-light); }
.drop.active { border-color: var(--el-color-primary); }
.hidden-input { display: none; }
.drop-text { color: var(--el-text-color-regular); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0 16px; }
.actions { display: flex; justify-content: flex-end; gap: 8px; }
.top-alert { margin: 0; }
.choose-head { display: flex; justify-content: space-between; align-items: center; }
.choose-head h2 { margin: 0; font-size: 18px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.story-card { cursor: pointer; border: 2px solid transparent; }
.story-card.selected { border-color: var(--el-color-primary); }
.story-card.rejected { opacity: 0.6; cursor: not-allowed; }
.story-head { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
.story-head h3 { margin: 0; font-size: 15px; }
.badges { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
.hook { margin: 8px 0; font-style: italic; }
.facts { display: flex; flex-direction: column; gap: 2px; font-size: 12px; color: var(--el-text-color-regular); }
.synopsis { margin-top: 8px; }
.select-bar { position: sticky; bottom: 8px; z-index: 2; }
.select-row { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
.select-row > div:first-child { min-width: 180px; max-width: 320px; }
.select-row > div:first-child b { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.count { margin: 0; }
.estimate { flex: 1; }
.warn { color: var(--el-color-warning); }
.ch-title { flex: 1; }
.ch-tag { margin-left: 8px; }
.preview { white-space: pre-wrap; font-family: inherit; font-size: 13px; color: var(--el-text-color-regular); max-height: 220px; overflow: auto; }
.downloads { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; }
.download { text-decoration: none; }
.summary { gap: 12px; }
.kv { list-style: none; padding: 0; margin: 0; }
.kv li { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px dashed var(--el-border-color-lighter); font-size: 13px; }
.finding { display: flex; gap: 8px; align-items: center; font-size: 12px; padding: 2px 0; }
.finding-kind { font-family: monospace; }
</style>
