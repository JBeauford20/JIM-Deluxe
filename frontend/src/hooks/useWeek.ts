import { create } from 'zustand'

interface WeekStore {
  week:      string | null
  batchId:   string | null
  runId:     string | null
  runStatus: string | null
  setWeek:   (w: string) => void
  setBatch:  (id: string) => void
  setRun:    (id: string, status: string) => void
}

// Zustand store — shared week context across all pages
export const useWeek = create<WeekStore>(set => ({
  week:      null,
  batchId:   null,
  runId:     null,
  runStatus: null,
  setWeek:   week     => set({ week }),
  setBatch:  batchId  => set({ batchId }),
  setRun:    (runId, runStatus) => set({ runId, runStatus }),
}))
