import { NavLink, useNavigate } from 'react-router-dom'
import { useWeek } from '../../hooks/useWeek'

export default function Navbar() {
  const navigate  = useNavigate()
  const { week }  = useWeek()

  const logout = () => {
    localStorage.removeItem('jim_token')
    navigate('/login')
  }

  return (
    <nav className="jim-navbar d-flex align-items-center">

      {/* Logo */}
      <NavLink to="/" className="navbar-brand me-4">
        <img src="/logo.png" alt="TPC Home Depot Order Writer" />
      </NavLink>

      {/* Nav links */}
      <div className="d-flex h-100">
        <NavLink to="/" end className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
          <i className="bi bi-house me-1" /> Week
        </NavLink>
        <NavLink to="/plan" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
          <i className="bi bi-clipboard-check me-1" /> Review Plan
        </NavLink>
        <NavLink to="/editor" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
          <i className="bi bi-cart3 me-1" /> Order Editor
        </NavLink>
        <NavLink to="/stores" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
          <i className="bi bi-shop me-1" /> Stores
        </NavLink>
      </div>

      {/* Right side */}
      <div className="ms-auto d-flex align-items-center gap-3">
        {week && (
          <span className="user-chip">
            <i className="bi bi-calendar3 me-1" />
            Week of {week}
          </span>
        )}
        <button className="btn btn-sm btn-outline-light" onClick={logout}>
          <i className="bi bi-box-arrow-right me-1" /> Sign out
        </button>
      </div>

    </nav>
  )
}
