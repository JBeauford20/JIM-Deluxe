import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

// Attach auth token from localStorage on every request
api.interceptors.request.use(cfg => {
  const token = localStorage.getItem('jim_token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

// Surface backend error messages clearly
api.interceptors.response.use(
  r => r,
  err => {
    const msg = err.response?.data?.detail || err.message || 'Request failed'
    return Promise.reject(new Error(msg))
  }
)

export default api

// ── Typed API calls ───────────────────────────────────────────

export const storesApi = {
  list:    (params?: object) => api.get('/stores/', { params }).then(r => r.data),
  profile: (id: number)     => api.get(`/stores/${id}`).then(r => r.data),
}

export const availApi = {
  batches: ()         => api.get('/availability/batches').then(r => r.data),
  detail:  (id: string) => api.get(`/availability/batches/${id}`).then(r => r.data),
  upload:  (file: File, week: string) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('shipping_week', week)
    return api.post('/availability/upload', fd).then(r => r.data)
  },
}

export const ordersApi = {
  forRun:   (runId: string)   => api.get(`/orders/runs/${runId}`).then(r => r.data),
  detail:   (orderId: string) => api.get(`/orders/${orderId}`).then(r => r.data),
  approve:  (orderId: string, version: number) =>
    api.patch(`/orders/${orderId}/approve?version=${version}`).then(r => r.data),
  review:   (orderId: string) => api.patch(`/orders/${orderId}/review`).then(r => r.data),
}

export const cartsApi = {
  get:      (cartId: string) => api.get(`/carts/${cartId}`).then(r => r.data),
  addTrays: (cartId: string, payload: object) =>
    api.post(`/carts/${cartId}/shelves`, payload).then(r => r.data),
  removeShelf: (cartId: string, pos: number) =>
    api.delete(`/carts/${cartId}/shelves/${pos}`).then(r => r.data),
}

export const engineApi = {
  generate:  (payload: object) => api.post('/engine/generate', payload).then(r => r.data),
  jobStatus: (jobId: string)   => api.get(`/engine/jobs/${jobId}`).then(r => r.data),
  velocity:  ()                => api.post('/engine/velocity').then(r => r.data),
}

export const exportApi = {
  asterCsv: (runId: string) =>
    api.get(`/exports/runs/${runId}/aster-import`, { responseType: 'blob' }).then(r => r.data),
}
