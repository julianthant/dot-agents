import { useEffect, useState } from 'react'
import type { Data } from './model'

const URL_ = '/data.json'
const EVERY = 15_000

export type Feed = { data: Data | null; error: string | null; fetchedAt: number | null }

// Polls data.json; a failed poll keeps the last good data and reports the error.
export function useRoadmapData(): Feed {
  const [feed, setFeed] = useState<Feed>({ data: null, error: null, fetchedAt: null })
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const r = await fetch(URL_, { cache: 'no-store' })
        if (!r.ok) throw new Error(`data.json answered ${r.status}`)
        const data = (await r.json()) as Data
        if (!Array.isArray(data?.issues)) throw new Error('data.json has no issues list')
        if (alive) setFeed({ data, error: null, fetchedAt: Date.now() })
      } catch (e) {
        if (alive) setFeed((f) => ({ ...f, error: e instanceof Error ? e.message : String(e) }))
      }
    }
    load()
    const id = setInterval(load, EVERY)
    return () => { alive = false; clearInterval(id) }
  }, [])
  return feed
}
