import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'react-toastify'
import { ordersApi, exportApi } from '../api/client'
import { useWeek } from '../hooks/useWeek'
import TierBadge from '../components/common/TierBadge'
import ConfirmModal from '../components/common/ConfirmModal'
import WeekBanner from '../components/layout/WeekBanner'
import { formatDistanceToNow } from 'date-fns'

export default function Plan() {
  const { runId, runStatus, batchId } = useWeek()
  const qc = useQueryClient()
  const [selectedOrder, setSelectedOrder] = useState<any>(null)
  const [showApproveAll, setShowApproveAll] = useState(false)
  const [filter, setFilter] = useState('')

  const { data: orders = [], isLoading } = useQuery({
    queryKey:  ['orders', runId],
    queryFn:   () => ordersApi.forRun(runId!),
    enabled:   !!runId,
    refetchInterval: 15_000,
  })

  const approveMut = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) =>
      ordersApi.approve(id, version),
    onSuccess: () => {
      toast.success('Order approved')
      qc.invalidateQueries({ queryKey: ['orders', runId] })
    },
    onError: (e: any) => toast.error(e.message),
  })

  const exportMut = useMutation({
    mutationFn: () => exportApi.asterCsv(runId!),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob)
      const a   = document.createElement('a')
      a.href    = url
      a.download = `JIM_AsterImport_${Date.now()}.csv`
      a.click()
      URL.revokeObjectURL(url)
      toast.success('Aster import file downloaded')
    },
    onError: (e: any) => toast.error(e.message),
  })

  const filtered = orders.filter((o: any) =>
    !filter ||
    String(o.store_id).includes(filter) ||
    o.store_name?.toLowerCase().includes(filter.toLowerCase()) ||
    o.city?.toLowerCase().includes(filter.toLowerCase())
  )

  // Summary metrics
  const totalCarts   = orders.reduce((s: number, o: any) => s + (o.cart_count || 0), 0)
  const totalUnits   = orders.reduce((s: number, o: any) => s + (o.total_units || 0), 0)
  const needsReview  = orders.filter((o: any) => !o.is_reviewed && o.status === 'draft').length
  const flagged      = orders.filter((o: any) => o.has_issues).length
  const approved     = orders.filter((o: any) => o.status === 'approved').length

  return (
    <div>
      <WeekBanner runStatus={runStatus || undefined} />

      <div className="container-fluid p-4">

        {/* Summary cards */}
        <div className="row g-3 mb-4">
          {[
            { label: 'Stores Served',     num: orders.length,  icon: 'bi-shop',              cls: '' },
            { label: 'Total Carts',        num: totalCarts,     icon: 'bi-cart3',             cls: '' },
            { label: 'Units Allocated',    num: totalUnits.toLocaleString(), icon: 'bi-box-seam', cls: '' },
            { label: 'Approved',           num: approved,       icon: 'bi-check-circle',      cls: approved === orders.length ? 'ok' : '' },
            { label: 'Needs Review',       num: needsReview,    icon: 'bi-eye',               cls: needsReview > 0 ? 'warn' : 'ok' },
            { label: 'Flagged Issues',     num: flagged,        icon: 'bi-exclamation-triangle', cls: flagged > 0 ? 'warn' : 'ok' },
          ].map(m => (
            <div key={m.label} className="col-6 col-md-4 col-xl-2">
              <div className={`stat-card ${m.cls}`}>
                <div className="stat-num">
                  <i className={`bi ${m.icon} me-2`} style={{ fontSize: '1rem' }} />
                  {m.num}
                </div>
                <div className="stat-label">{m.label}</div>
              </div>
            </div>
          ))}
        </div>

        {/* Actions bar */}
        <div className="d-flex align-items-center gap-2 mb-3">
          <input
            className="form-control form-control-sm"
            style={{ maxWidth: 260 }}
            placeholder="Search store, city…"
            value={filter}
            onChange={e => setFilter(e.target.value)}
          />
          <span className="text-muted" style={{ fontSize: 13 }}>
            {filtered.length} orders
          </span>
          <div className="ms-auto d-flex gap-2">
            <button
              className="btn btn-sm btn-outline-primary"
              onClick={() => setShowApproveAll(true)}
              disabled={approved === orders.length}
            >
              <i className="bi bi-check-all me-1" /> Approve All
            </button>
            <button
              className="btn btn-sm btn-cta"
              onClick={() => exportMut.mutate()}
              disabled={approved === 0 || exportMut.isPending}
            >
              {exportMut.isPending
                ? <span className="spinner-border spinner-border-sm me-1" />
                : <i className="bi bi-download me-1" />
              }
              Export to Aster
            </button>
          </div>
        </div>

        {/* Orders table */}
        <div className="card">
          <div className="table-responsive">
            <table className="table jim-table mb-0">
              <thead>
                <tr>
                  <th>Store</th>
                  <th>Location</th>
                  <th>Tier</th>
                  <th>Last Delivery</th>
                  <th className="text-center">Carts</th>
                  <th className="text-center">Units</th>
                  <th>Load</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {isLoading && (
                  <tr><td colSpan={9} className="text-center py-4 text-muted">
                    <span className="spinner-border spinner-border-sm me-2" />Loading orders…
                  </td></tr>
                )}
                {!isLoading && filtered.length === 0 && (
                  <tr><td colSpan={9} className="text-center py-4 text-muted">
                    No orders found
                  </td></tr>
                )}
                {filtered.map((o: any) => (
                  <tr
                    key={o.id}
                    className={o.has_issues ? 'flagged' : o.status === 'approved' ? 'approved' : ''}
                    onClick={() => setSelectedOrder(o)}
                  >
                    <td>
                      <strong>{o.store_id}</strong>
                      {o.has_issues && (
                        <i className="bi bi-exclamation-circle text-orange ms-1" title={o.issue_summary} />
                      )}
                    </td>
                    <td className="text-muted" style={{ fontSize: 12 }}>
                      {o.store_name}<br />
                      <span>{o.city}, {o.state}</span>
                    </td>
                    <td>
                      <TierBadge tier={o.dynamic_velocity_tier} />
                      {o.volume_code !== o.dynamic_velocity_tier && (
                        <TierBadge tier={o.volume_code} size="sm" />
                      )}
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {o.days_since_last_delivery != null
                        ? `${o.days_since_last_delivery}d ago`
                        : <span className="text-muted">—</span>
                      }
                    </td>
                    <td className="text-center fw-bold">{o.cart_count}</td>
                    <td className="text-center">{o.total_units?.toLocaleString()}</td>
                    <td style={{ fontSize: 12 }}>{o.load_name || '—'}</td>
                    <td>
                      <span className={`status-badge ${o.status}`}>{o.status}</span>
                      {o.is_reviewed && (
                        <i className="bi bi-eye-fill text-muted ms-1" title="Reviewed" style={{ fontSize: 11 }} />
                      )}
                    </td>
                    <td onClick={e => e.stopPropagation()}>
                      {o.status === 'draft' && (
                        <button
                          className="btn btn-sm btn-cta py-0"
                          style={{ fontSize: 11 }}
                          onClick={() => approveMut.mutate({ id: o.id, version: o.version })}
                          disabled={approveMut.isPending}
                        >
                          Approve
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Approve all confirm */}
      <ConfirmModal
        show={showApproveAll}
        title="Approve All Orders"
        body={<>This will approve all <strong>{orders.length - approved}</strong> remaining draft orders. Are you sure?</>}
        confirmLabel="Yes, Approve All"
        onConfirm={() => { setShowApproveAll(false); toast.info('Bulk approve coming in v1.1') }}
        onCancel={() => setShowApproveAll(false)}
      />
    </div>
  )
}
