import { describe, expect, it, vi } from 'vitest'
import type { AutonomousJob, PreflightResult, StorylineOption } from '@renderer/api/autonomous'
import { artifactDownloadPath, screenFor, useAutonomousNovel, type AutonomousApi } from '../useAutonomousNovel'

function preflight(over: Partial<PreflightResult> = {}): PreflightResult {
  const ok = { passed: true, skipped: false, advisory: false }
  return {
    passed: true, llm_config_id: 1, provider: 'openai_compatible', model: 'kimi-test', endpoint_class: 'openai_chat_completions', latency_ms: 120,
    text_check: { name: 'text_generation', ...ok }, structured_check: { name: 'structured_output', ...ok }, usage_reporting: 'reported', fallback_checked: false, fallback: null,
    warnings: [], failure_category: null, diagnostic: null, checks: [{ name: 'static_validation', ...ok }, { name: 'text_generation', ...ok }, { name: 'structured_output', ...ok }], timestamp: '2026-01-01T00:00:00', ...over,
  }
}

function job(over: Partial<AutonomousJob> = {}): AutonomousJob {
  return {
    id: 1, status: 'queued', stage: 'INGEST', mode: 'fully_automatic', llm_config_id: 1, source_filename: 'book.epub', options: { words_per_chapter: 2000 }, chapter_count: 0, chapters_committed: 0,
    progress_percent: 0, progress_message: '', stage_results: {}, warnings: [], model_calls: 0, input_tokens: 0, output_tokens: 0, attempts: [], stages: ['INGEST', 'STORYLINE_SELECTION', 'CHAPTER_GENERATION_LOOP', 'EXPORT', 'DONE'], ...over,
  }
}
function option(id: number, over: Partial<StorylineOption> = {}): StorylineOption {
  return { id, job_id: 1, option_index: id, title: `Option ${id}`, content: {}, originality_score: 0.95, originality_report: {}, similarity_to_others: {}, recommended_chapters_min: 10, recommended_chapters_max: 30, rejected: false, selected: false, ...over }
}
function makeApi(over: Partial<AutonomousApi> = {}): AutonomousApi {
  return {
    createJob: vi.fn(async () => ({ job: job(), active: true })),
    runPreflight: vi.fn(async () => preflight()),
    getJob: vi.fn(async () => ({ job: job(), active: true })),
    listJobs: vi.fn(async () => []),
    listStorylines: vi.fn(async () => [option(1), option(2, { rejected: true, rejection_reason: 'too similar' })]),
    selectStoryline: vi.fn(async () => ({ job: job({ stage: 'NOVEL_ARCHITECTURE', status: 'queued', chapter_count: 20 }), active: true })),
    approveJob: vi.fn(async () => ({ job: job(), active: true })),
    pauseJob: vi.fn(async () => ({ job: job({ status: 'paused' }), active: false })),
    resumeJob: vi.fn(async () => ({ job: job(), active: true })),
    cancelJob: vi.fn(async () => ({ job: job({ status: 'cancelled' }), active: false })),
    listChapters: vi.fn(async () => []),
    listArtifacts: vi.fn(async () => []),
    getReport: vi.fn(async () => ({})),
    fileToBase64: vi.fn(async () => 'QUJD'),
    ...over,
  }
}

describe('screenFor', () => {
  it('derives the five screens from the job', () => {
    expect(screenFor(null)).toBe('upload')
    expect(screenFor(job({ stage: 'SOURCE_ANALYSIS', status: 'running' }))).toBe('analysis')
    expect(screenFor(job({ stage: 'STORYLINE_SELECTION', status: 'waiting_for_user' }))).toBe('choose')
    expect(screenFor(job({ stage: 'CHAPTER_GENERATION_LOOP', status: 'running' }))).toBe('generating')
    expect(screenFor(job({ stage: 'DONE', status: 'completed' }))).toBe('finished')
    expect(screenFor(job({ stage: 'STORYLINE_GENERATION', status: 'waiting_for_user', mode: 'manual' }))).toBe('choose')
  })
})

describe('useAutonomousNovel', () => {
  it('starts a job from the picked file and loads storylines once the job waits for selection', async () => {
    const api = makeApi({ createJob: vi.fn(async () => ({ job: job({ stage: 'STORYLINE_SELECTION', status: 'waiting_for_user' }), active: false })) })
    const auto = useAutonomousNovel(api, { pollMs: 1 })
    await auto.pickFile(new File(['abc'], 'book.epub'))
    expect(auto.file.value?.name).toBe('book.epub')
    await auto.start({ llm_config_id: 1, mode: 'fully_automatic' })
    expect(api.createJob).toHaveBeenCalledWith(expect.objectContaining({ filename: 'book.epub', content_base64: 'QUJD', llm_config_id: 1 }))
    expect(auto.screen.value).toBe('choose')
    expect(api.listStorylines).toHaveBeenCalledWith(1, true)
    expect(auto.acceptedOptions.value.map((o) => o.id)).toEqual([1])
    expect(auto.isActive.value).toBe(false)
    auto.stopPolling()
  })

  it('gates selection on a non-rejected option and a valid chapter count, and warns outside the recommended range', async () => {
    const api = makeApi({ createJob: vi.fn(async () => ({ job: job({ stage: 'STORYLINE_SELECTION', status: 'waiting_for_user' }), active: false })) })
    const auto = useAutonomousNovel(api, { pollMs: 1 })
    await auto.pickFile(new File(['abc'], 'book.epub'))
    await auto.start({ llm_config_id: 1 })
    expect(auto.canSelect.value).toBe(false)
    auto.selectedStorylineId.value = 2 // rejected
    expect(auto.canSelect.value).toBe(false)
    auto.selectedStorylineId.value = 1
    auto.chapterCount.value = 40
    expect(auto.canSelect.value).toBe(true)
    expect(auto.chapterCountWarning.value).toBe('above')
    expect(auto.estimatedWords.value).toBe(80_000)
    auto.chapterCount.value = 20
    expect(auto.chapterCountWarning.value).toBeNull()
    await auto.confirmSelection()
    expect(api.selectStoryline).toHaveBeenCalledWith(1, { storyline_id: 1, chapter_count: 20 })
    expect(auto.screen.value).toBe('generating')
    auto.stopPolling()
  })

  it('polls only while the job is active and surfaces API errors without breaking state', async () => {
    let calls = 0
    const api = makeApi({
      createJob: vi.fn(async () => ({ job: job({ stage: 'SOURCE_ANALYSIS', status: 'running' }), active: true })),
      getJob: vi.fn(async () => {
        calls += 1
        if (calls === 1) throw { response: { data: { detail: 'boom' } } }
        return { job: job({ stage: 'STORYLINE_SELECTION', status: 'waiting_for_user' }), active: false }
      }),
    })
    const auto = useAutonomousNovel(api, { pollMs: 1 })
    await auto.pickFile(new File(['abc'], 'book.epub'))
    await auto.start({ llm_config_id: 1 })
    expect(auto.screen.value).toBe('analysis')
    await new Promise((r) => setTimeout(r, 30))
    expect(auto.error.value).toBe('boom')
    await new Promise((r) => setTimeout(r, 30))
    expect(auto.screen.value).toBe('choose')
    const n = calls
    await new Promise((r) => setTimeout(r, 30))
    expect(calls).toBe(n) // stopped polling once waiting for the user
    auto.stopPolling()
  })

  it('runs the provider preflight and exposes failures and warnings without secrets', async () => {
    const api = makeApi({ runPreflight: vi.fn(async () => preflight({ passed: false, failure_category: 'auth_failed', diagnostic: 'HTTP 401 *** rejected', warnings: ['provider did not report token usage'], usage_reporting: 'missing' })) })
    const auto = useAutonomousNovel(api, { pollMs: 1 })
    const res = await auto.runPreflight({ llm_config_id: 1, timeout_seconds: 45 })
    expect(api.runPreflight).toHaveBeenCalledWith({ llm_config_id: 1, timeout_seconds: 45 })
    expect(res?.passed).toBe(false)
    expect(auto.preflight.value?.failure_category).toBe('auth_failed')
    expect(auto.preflight.value?.warnings).toHaveLength(1)
    expect(auto.busy.value).toBeNull()
    // A transport error clears the result and surfaces the message.
    const failing = makeApi({ runPreflight: vi.fn(async () => { throw { response: { data: { detail: 'gateway down' } } } }) })
    const auto2 = useAutonomousNovel(failing, { pollMs: 1 })
    await auto2.runPreflight({ llm_config_id: 1 })
    expect(auto2.preflight.value).toBeNull()
    expect(auto2.error.value).toBe('gateway down')
  })

  it('prevents duplicate submissions and sends a stable idempotency key with the budget', async () => {
    let resolveCreate: ((v: { job: AutonomousJob; active: boolean }) => void) | null = null
    const api = makeApi({ createJob: vi.fn(() => new Promise<{ job: AutonomousJob; active: boolean }>((r) => { resolveCreate = r })) })
    const auto = useAutonomousNovel(api, { pollMs: 1 })
    await auto.pickFile(new File(['abc'], 'book.epub'))
    const params = { llm_config_id: 1, mode: 'fully_automatic' as const, budget: { max_calls: 50, max_cost_usd: 2, price_per_million: { input: 1, output: 3 } } }
    const first = auto.start(params)
    await auto.start(params) // double click while the first request is in flight
    expect(api.createJob).toHaveBeenCalledTimes(1)
    const body = (api.createJob as ReturnType<typeof vi.fn>).mock.calls[0][0]
    expect(body.idempotency_key).toMatch(/^ui-/)
    expect(body.budget).toEqual(params.budget)
    resolveCreate!({ job: job({ stage: 'SOURCE_ANALYSIS', status: 'running' }), active: true })
    await first
    await auto.start(params) // job exists: no second job
    expect(api.createJob).toHaveBeenCalledTimes(1)
    auto.stopPolling()
  })

  it('keeps the last job during transient polling failures and backs off instead of giving up', async () => {
    let calls = 0
    const api = makeApi({
      createJob: vi.fn(async () => ({ job: job({ stage: 'CHAPTER_GENERATION_LOOP', status: 'running', chapters_committed: 1, chapter_count: 6 }), active: true })),
      getJob: vi.fn(async () => {
        calls += 1
        if (calls <= 3) throw new Error('ECONNRESET')
        return { job: job({ stage: 'DONE', status: 'completed', quality_status: 'completed_with_warnings', chapter_count: 6, chapters_committed: 6 }), active: false }
      }),
      listArtifacts: vi.fn(async () => [{ id: 7, kind: 'epub', filename: 'n.epub', media_type: 'application/epub+zip', size_bytes: 10, content_hash: 'h', created_at: '' }]),
    })
    const auto = useAutonomousNovel(api, { pollMs: 1 })
    await auto.pickFile(new File(['abc'], 'book.epub'))
    await auto.start({ llm_config_id: 1 })
    expect(auto.screen.value).toBe('generating')
    await new Promise((r) => setTimeout(r, 8))
    expect(auto.job.value?.stage).toBe('CHAPTER_GENERATION_LOOP') // last known job retained through failures
    expect(auto.pollFailures.value).toBeGreaterThan(0)
    await new Promise((r) => setTimeout(r, 80))
    expect(auto.screen.value).toBe('finished')
    expect(auto.pollFailures.value).toBe(0)
    expect(auto.job.value?.quality_status).toBe('completed_with_warnings')
    expect(auto.artifacts.value.map((a) => a.kind)).toEqual(['epub'])
    expect(artifactDownloadPath(7, auto.job.value!.id)).toBe('/autonomous/jobs/1/artifacts/7/download')
    auto.stopPolling()
  })
})
