import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { storesApi } from '../api/client'
import TierBadge from '../components/common/TierBadge'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

export default function Stores() {
  const [search, setSearch]     = useState('')
  const [tier, setTier]         = useState('')
  const [region, setRegion]     = useState('')
  const [selected, setSelected] = useState<number | null>(null)

  const { data: stores = [], isLoading } = useQuery({
    queryKey: ['stores', search, tier, region],
    queryFn:  () => storesApi.list({ search, tier: tier || undefined, region: region || undefined }),
    staleTime: 60_000,
  })

  const { data: profile } = useQuery({
    queryKey: ['store-profile', selected],
    queryFn:  () => storesApi.profile(selected!),
    enabled:  !!selected,
  })

  return (
    <div className="container-fluid p-4">
      <div className="row g-4">

        {/* ── Left: Store list ─────────────────────── */}
        <div className="col-12 col-xl-5">
          <div className="card">
            <div className="card-header">
              <i className="bi bi-shop me-2" />Store Explorer
            </div>
            <div className="card-body pb-0">

              {/* Filters */}
              <div className="d-flex gap-2 mb-3">
                <input
                  className="form-control form-control-sm"
                  placeholder="Search store, city, state…"
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                />
                <select className="form-select form-select-sm" style={{ width: 80 }}
                  value={tier} onChange={e => setTier(e.target.value)}>
                  <option value="">All</option>
                  {['AA','A','B','C','D'].map(t => <option key={t}>{t}</option>)}
                </select>
                <select className="form-select form-select-sm" style={{ width: 110 }}
                  value={region} onChange={e => setRegion(e.target.value)}>
                  <option value="">All regions</option>
                  <option>Northern</option>
                  <option>Southern</option>
                  <option>Western</option>
                </select>
              </div>
            </div>

            {/* Store list */}
            <div style={{ maxHeight: '70vh', overflowY: 'auto' }}>
              <table className="table jim-table mb-0">
                <thead style={{ position: 'sticky', top: 0, zIndex: 1 }}>
                  <tr>
                    <th>Store</th>
                    <th>Location</th>
                    <th>Tier</th>
                    <th>Score</th>
                    <th>Merchant</th>
                  </tr>
                </thead>
                <tbody>
                  {isLoading && (
                    <tr><td colSpan={5} className="text-center py-3 text-muted">
                      <span className="spinner-border spinner-border-sm me-2" />Loading…
                    </td></tr>
                  )}
                  {stores.map((s: any) => (
                    <tr
                      key={s.store_id}
                      className={selected === s.store_id ? 'table-active' : ''}
                      onClick={() => setSelected(s.store_id)}
                    >
                      <td><strong>{s.store_id}</strong><br />
                        <span style={{ fontSize: 11, color: 'var(--tpc-ink-soft)' }}>{s.store_name}</span>
                      </td>
                      <td style={{ fontSize: 12 }}>{s.city}, {s.state}</td>
                      <td>
                        <TierBadge tier={s.dynamic_velocity_tier} />
                        {s.volume_code !== s.dynamic_velocity_tier && (
                          <span className="ms-1 text-muted" style={{ fontSize: 10 }}>was {s.volume_code}</span>
                        )}
                      </td>
                      <td style={{ fontSize: 12, fontWeight: 600 }}>
                        {s.dynamic_velocity_score ? Number(s.dynamic_velocity_score).toFixed(1) : '—'}
                      </td>
                      <td style={{ fontSize: 11, color: 'var(--tpc-ink-soft)' }}>{s.merchant}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* ── Right: Store profile ──────────────────── */}
        <div className="col-12 col-xl-7">
          {!selected && (
            <div className="card d-flex align-items-center justify-content-center"
                 style={{ minHeight: 400 }}>
              <div className="text-center text-muted p-4">
                <i className="bi bi-shop" style={{ fontSize: 40, opacity: .3 }} />
                <p className="mt-3">Select a store to see its full profile</p>
              </div>
            </div>
          )}

          {selected && profile && (
            <div>
              {/* Header */}
              <div className="card mb-3">
                <div className="card-body py-3">
                  <div className="d-flex align-items-start gap-3">
                    <div>
                      <h5 className="mb-1 text-fern-dark">
                        Store {profile.store.store_id} — {profile.store.store_name}
                      </h5>
                      <p className="mb-1 text-muted" style={{ fontSize: 13 }}>
                        {profile.store.city}, {profile.store.state} &middot; {profile.store.merchant} &middot; {profile.store.merch_partner}
                      </p>
                    </div>
                    <div className="ms-auto text-end">
                      <TierBadge tier={profile.store.dynamic_velocity_tier} showLabel />
                      <div style={{ fontSize: 11, color: 'var(--tpc-ink-soft)', marginTop: 4 }}>
                        Static: {profile.store.volume_code}
                      </div>
                    </div>
                  </div>

                  {/* Category tiers */}
                  <div className="d-flex flex-wrap gap-2 mt-3">
                    {[
                      ['9cm', profile.store.tier_9cm],
                      ['12cm', profile.store.tier_12cm],
                      ['17cm', profile.store.tier_17cm],
                      ['H2O', profile.store.tier_h2o],
                      ['Collectors', profile.store.tier_collectors],
                      ['Boutique', profile.store.tier_boutique],
                    ].map(([label, t]) => t && (
                      <div key={label} className="d-flex align-items-center gap-1 px-2 py-1 rounded"
                           style={{ background: '#f0f9f0', fontSize: 12 }}>
                        <span className="text-muted">{label}</span>
                        <TierBadge tier={t as string} size="sm" />
                      </div>
                    ))}
                  </div>
                </div>
              </div>

              {/* Sales chart */}
              <div className="card mb-3">
                <div className="card-header light">
                  <i className="bi bi-graph-up me-2" />Weekly Sales — Last 26 Weeks
                </div>
                <div className="card-body">
                  <ResponsiveContainer width="100%" height={160}>
                    <LineChart data={[...profile.weekly_sales].reverse()}>
                      <XAxis dataKey="week_start" tick={{ fontSize: 10 }} tickFormatter={d => d.slice(5)} />
                      <YAxis tick={{ fontSize: 10 }} />
                      <Tooltip
                        formatter={(v: any) => [v, 'Units']}
                        labelFormatter={l => `Week of ${l}`}
                      />
                      <Line type="monotone" dataKey="units" stroke="#2F4A3E" strokeWidth={2} dot={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </div>

              {/* Top SKUs */}
              <div className="card">
                <div className="card-header light">
                  <i className="bi bi-bar-chart me-2" />Top SKUs (All Time)
                </div>
                <div className="card-body p-0">
                  <table className="table jim-table mb-0">
                    <thead>
                      <tr>
                        <th>Product</th>
                        <th>Family</th>
                        <th>Tier</th>
                        <th className="text-end">Total Units</th>
                      </tr>
                    </thead>
                    <tbody>
                      {profile.top_skus.slice(0,8).map((s: any) => (
                        <tr key={s.sku_id}>
                          <td style={{ fontSize: 12 }}>{s.description?.replace('PW ', '').replace(' TPC', '')}</td>
                          <td style={{ fontSize: 11 }}>{s.family}</td>
                          <td><TierBadge tier={s.legacy_product_group?.[0]} size="sm" /></td>
                          <td className="text-end fw-bold">{s.total_units?.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}
        </div>

      </div>
    </div>
  )
}
