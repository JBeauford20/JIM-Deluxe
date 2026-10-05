import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'react-toastify'
import api from '../api/client'

export default function Login() {
  const [email, setEmail]       = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading]   = useState(false)
  const navigate = useNavigate()

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      // Sign in via Supabase Auth (direct, no backend needed)
      const { createClient } = await import('@supabase/supabase-js')
      const sb = createClient(
        import.meta.env.VITE_SUPABASE_URL,
        import.meta.env.VITE_SUPABASE_ANON_KEY
      )
      const { data, error } = await sb.auth.signInWithPassword({ email, password })
      if (error) throw new Error(error.message)

      localStorage.setItem('jim_token', data.session!.access_token)
      toast.success(`Welcome back, ${data.user?.email?.split('@')[0]}!`)
      navigate('/')
    } catch (err: any) {
      toast.error(err.message || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-vh-100 d-flex align-items-center justify-content-center"
         style={{ background: 'var(--tpc-fern-dark)' }}>
      <div style={{ width: 400 }}>

        {/* Logo */}
        <div className="text-center mb-4">
          <img src="/logo.png" alt="TPC Order Writer" style={{ height: 64 }} />
        </div>

        {/* Card */}
        <div className="card border-0 shadow-lg">
          <div className="card-header text-center" style={{ letterSpacing: '.08em', fontSize: 12 }}>
            SIGN IN TO JIM DELUXE
          </div>
          <div className="card-body p-4">
            <form onSubmit={submit}>
              <div className="mb-3">
                <label className="form-label fw-semibold" style={{ fontSize: 13 }}>
                  Email
                </label>
                <input
                  type="email"
                  className="form-control"
                  value={email}
                  onChange={e => setEmail(e.target.value)}
                  placeholder="you@theplantcompany.com"
                  required
                  autoFocus
                />
              </div>
              <div className="mb-4">
                <label className="form-label fw-semibold" style={{ fontSize: 13 }}>
                  Password
                </label>
                <input
                  type="password"
                  className="form-control"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder="••••••••"
                  required
                />
              </div>
              <button
                type="submit"
                className="btn btn-primary w-100 py-2 fw-bold"
                disabled={loading}
              >
                {loading
                  ? <><span className="spinner-border spinner-border-sm me-2" />Signing in…</>
                  : <><i className="bi bi-box-arrow-in-right me-2" />Sign In</>
                }
              </button>
            </form>
          </div>
          <div className="card-footer text-center py-2" style={{ fontSize: 11, color: 'var(--tpc-ink-soft)' }}>
            The Plant Company · Internal Tool · Confidential
          </div>
        </div>

      </div>
    </div>
  )
}
