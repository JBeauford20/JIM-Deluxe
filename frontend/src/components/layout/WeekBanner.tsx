import { useWeek } from '../../hooks/useWeek'

interface Props { runStatus?: string; batchName?: string }

export default function WeekBanner({ runStatus, batchName }: Props) {
  const { week } = useWeek()

  return (
    <div className="week-banner">
      <span><i className="bi bi-calendar-week me-2" /><strong>Shipping Week:</strong> {week || '—'}</span>
      {batchName && (
        <span><i className="bi bi-box-seam me-1" />{batchName}</span>
      )}
      {runStatus && (
        <span className={`banner-chip status-${runStatus}`}>
          {runStatus === 'draft'    && <><i className="bi bi-pencil me-1" />Draft Plan</>}
          {runStatus === 'approved' && <><i className="bi bi-check-circle me-1" />Approved</>}
          {runStatus === 'exported' && <><i className="bi bi-send-check me-1" />Exported</>}
        </span>
      )}
      <span className="ms-auto" style={{ fontSize: 11, opacity: .7 }}>
        JIM Deluxe v1.0 · The Plant Company
      </span>
    </div>
  )
}
