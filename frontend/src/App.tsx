import { Routes, Route, Navigate } from 'react-router-dom'
import Navbar from './components/layout/Navbar'
import Login  from './pages/Login'
import Week   from './pages/Week'
import Plan   from './pages/Plan'
import Stores from './pages/Stores'

function RequireAuth({ children }: { children: React.ReactNode }) {
  const token = localStorage.getItem('jim_token')
  return token ? <>{children}</> : <Navigate to="/login" replace />
}

function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="d-flex flex-column" style={{ minHeight: '100vh' }}>
      <Navbar />
      <main className="flex-grow-1">{children}</main>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={
        <RequireAuth>
          <Layout><Week /></Layout>
        </RequireAuth>
      } />
      <Route path="/plan" element={
        <RequireAuth>
          <Layout><Plan /></Layout>
        </RequireAuth>
      } />
      <Route path="/stores" element={
        <RequireAuth>
          <Layout><Stores /></Layout>
        </RequireAuth>
      } />
      <Route path="/editor" element={
        <RequireAuth>
          <Layout><div className="p-4 text-muted">Order Editor — coming next</div></Layout>
        </RequireAuth>
      } />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
