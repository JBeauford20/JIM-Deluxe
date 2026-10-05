import { useState, useEffect } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { toast } from 'react-toastify'
import { availApi, engineApi } from '../api/client'
import { useWeek } from '../hooks/useWeek'
import WeekBanner from '../components/layout/WeekBanner'

export default function Week() {
  const { week, batchId, runId, runStatus, setWeek, setBatch, setRun } = useWeek()
  const [jobId, setJobId]     = useState<string | null>(null)
  const [jobStatus, setJobStatus] = useState<string | null>(null)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadWeek, setUploadWeek] = useState('')

  const { data: batches = [] } = useQuery({
    queryKey: ['batches'],
    queryFn:  availApi.batches,
  })

  // Poll job status
  useEffect(() => {
    if (!jobId || jobStatus === 'complete' || jobStatus === 'failed') return
    const iv = setInterval(async () => {
      try {
        const job = await engineApi.jobStatus(jobId)
        setJobStatus(job.status)
        if (job.status === 'complete') {
          setRun(job.run_id, 'draft')
          toast.success('Recommendations generated! Head to Review Plan.')
          clearInterval(iv)
        } else if (job.status === 'failed') {
          toast.error(`Engine failed: ${job.error_text}`)
          clearInterval(iv)
        }
      } catch {}
    }, 3000)
    return () => clearInterval(iv)
  }, [jobId, jobStatus])

  const uploadMut = useMutation({
    mutationFn: () => availApi.upload(uploadFile!, uploadWeek),
    onSuccess: (d) => {
      setBatch(d.batch_id)
      setWeek(uploadWeek)
      toast.success(`Availability uploaded — ${d.batch_id.slice(0,8)}…`)
      setUploadFile(null)
    },
    onError: (e: any) => toast.error(e.message),
  })

  const generateMut = useMutation({
    mutationFn: () => engineApi.generate({ batch_id: batchId, shipping_week: week }),
    onSuccess: (d) => {
      setJobId(d.job_id)
      setJobStatus('queued')
      toast.info('Recommendation engine started — this takes ~30 seconds')
    },
    onError: (e: any) => toast.error(e.message),
  })

  const isGenerating = jobStatus === 'queued' || jobStatus === 'running'

  return (
    <div>
      <WeekBanner runStatus={runStatus || undefined} />

      <div className="container-fluid p-4">
        <div className="row g-4">

          {/* ── Card 1: Availability ─────────────────────── */}
          <div className="col-12 col-lg-6">
            <div className="card h-100">
              <div className="card-header">
                <i className="bi bi-box-seam me-2" />Availability
              </div>
              <div className="card-body">

                {/* Upload zone */}
                <div
                  className="border-2 rounded-3 p-4 text-center mb-3"
                  style={{
                    border: '2px dashed var(--tpc-moss-pale)',
                    background: uploadFile ? '#f0f9f0' : '#fafafa',
                    cursor: 'pointer',
                  }}
                  onDragOver={e => e.preventDefault()}
                  onDrop={e => { e.preventDefault(); setUploadFile(e.dataTransfer.files[0]) }}
                  onClick={() => document.getElementById('avail-input')?.click()}
                >
                  <input
                    id="avail-input"
                    type="file"
                    accept=".xlsx,.xls"
                    style={{ display: 'none' }}
                    onChange={e => setUploadFile(e.target.files?.[0] || null)}
                  />
                  {uploadFile
                    ? <><i className="bi bi-file-earmark-excel text-success me-2" style={{ fontSize: 20 }} />
                        <strong>{uploadFile.name}</strong></>
                    : <><i className="bi bi-upload me-2 text-muted" />
                        <span className="text-muted">Drag & drop availability Excel, or click to browse</span></>
                  }
                </div>

                <div className="d-flex gap-2 mb-3">
                  <input
                    type="date"
                    className="form-control form-control-sm"
                    value={uploadWeek}
                    onChange={e => setUploadWeek(e.target.value)}
                    placeholder="Shipping week start"
                  />
                  <button
                    className="btn btn-primary btn-sm flex-shrink-0"
                    disabled={!uploadFile || !uploadWeek || uploadMut.isPending}
                    onClick={() => uploadMut.mutate()}
                  >
                    {uploadMut.isPending
                      ? <span className="spinner-border spinner-border-sm" />
                      : <><i className="bi bi-upload me-1" />Upload</>
                    }
                  </button>
                </div>

                {/* Recent batches */}
                {batches.length > 0 && (
                  <div>
                    <p className="text-muted mb-1" style={{ fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '.05em' }}>
                      Recent batches
                    </p>
                    {batches.slice(0,4).map((b: any) => (
                      <div
                        key={b.id}
                        className="d-flex align-items-center justify-content-between py-2 px-2 rounded mb-1"
                        style={{
                          fontSize: 13,
                          background: batchId === b.id ? '#f0f9f0' : '#fafafa',
                          border: `1px solid ${batchId === b.id ? 'var(--tpc-fern)' : 'var(--tpc-line)'}`,
                          cursor: 'pointer',
                        }}
                        onClick={() => { setBatch(b.id); setWeek(b.shipping_week_start) }}
                      >
                        <span>
                          {batchId === b.id && <i className="bi bi-check-circle-fill text-success me-2" />}
                          <strong>{b.source_filename}</strong>
                        </span>
                        <span className="text-muted">
                          {b.total_units?.toLocaleString()} units · {b.shipping_week_start}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* ── Card 2: Playbook + Generate ──────────────── */}
          <div className="col-12 col-lg-6">
            <div className="playbook-card mb-3">
              <div className="playbook-header">
                <i className="bi bi-book me-2" />Weekly Playbook
              </div>
              <div className="p-3">
                <div className="d-flex gap-2 mb-3">
                  <button className="btn btn-sm btn-outline-primary flex-fill">
                    <i className="bi bi-arrow-repeat me-1" /> Business as Usual
                  </button>
                  <button className="btn btn-sm btn-primary flex-fill">
                    <i className="bi bi-plus-circle me-1" /> Add Scenario
                  </button>
                </div>
                <p className="text-muted mb-0" style={{ fontSize: 12.5 }}>
                  No adjustments active this week. The engine will run with standard velocity + recency weights.
                  Add a scenario to override — e.g. "Collectors push to West" or "H2O sale in Arnie's territory."
                </p>
              </div>
            </div>

            {/* Generate card */}
            <div className="card">
              <div className="card-header light">
                <i className="bi bi-lightning me-2" />Generate Recommendations
              </div>
              <div className="card-body">
                <div className="row g-2 mb-3 text-center">
                  {[
                    ['Velocity Weight', '45%'],
                    ['Recency Weight',  '40%'],
                    ['Breadth Weight',  '15%'],
                    ['Min Carts/Store', '2'],
                    ['Truck Capacity',  '45 carts'],
                  ].map(([label, val]) => (
                    <div key={label} className="col-4">
                      <div style={{ fontSize: 11, color: 'var(--tpc-ink-soft)', textTransform: 'uppercase', letterSpacing: '.04em' }}>{label}</div>
                      <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--tpc-fern-dark)' }}>{val}</div>
                    </div>
                  ))}
                </div>

                {isGenerating && (
                  <div className="alert alert-success py-2 mb-3 d-flex align-items-center gap-2" style={{ fontSize: 13 }}>
                    <span className="spinner-border spinner-border-sm" />
                    Engine running — recommendations will appear in Review Plan when complete…
                  </div>
                )}

                <div className="d-grid gap-2">
                  <button
                    className="btn btn-cta py-2"
                    disabled={!batchId || isGenerating}
                    onClick={() => generateMut.mutate()}
                  >
                    {isGenerating
                      ? <><span className="spinner-border spinner-border-sm me-2" />Generating…</>
                      : <><i className="bi bi-lightning-fill me-2" />Generate Recommended Orders</>
                    }
                  </button>
                  <button className="btn btn-outline-secondary py-2" disabled={!batchId}>
                    <i className="bi bi-pencil-square me-2" />Start Empty Plan
                  </button>
                </div>
              </div>
            </div>
          </div>

        </div>
      </div>
    </div>
  )
}
