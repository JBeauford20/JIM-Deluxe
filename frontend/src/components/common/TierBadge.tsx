interface Props {
  tier: string | null
  size?: 'sm' | 'md'
  showLabel?: boolean
}

const TIER_LABELS: Record<string, string> = {
  AA: 'Elite',
  A:  'Strong',
  B:  'Solid',
  C:  'Below Avg',
  D:  'Low',
  P:  'Provisional',
}

export default function TierBadge({ tier, size = 'md', showLabel = false }: Props) {
  if (!tier) return <span className="tier-badge tier-P">—</span>

  return (
    <span title={TIER_LABELS[tier] || tier}>
      <span className={`tier-badge tier-${tier}`} style={size === 'sm' ? { width: 26, height: 18, fontSize: 10 } : {}}>
        {tier}
      </span>
      {showLabel && (
        <span className="ms-1 text-muted" style={{ fontSize: 11 }}>{TIER_LABELS[tier]}</span>
      )}
    </span>
  )
}
