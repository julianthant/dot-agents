import { useMemo } from 'react'
import { Counts, FeatureCounts, FeatureList, GridSide, Legend, MilestoneGates, NowTable, Panel, RoadmapGrid, Stamp, StatusBar } from '@/components/roadmap'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { TooltipProvider } from '@/components/ui/tooltip'
import { build, type Model } from '@/lib/model'
import { useRoadmapData } from '@/lib/use-data'

// Now, the grid and the milestone cards at full width; below lg they split into tabs.
function Cockpit({ m }: { m: Model }) {
  const now = (
    <Panel id="h-now" title="Now" meta={`${m.inFlight.length} in flight`} right={m.inFlight.some(m.needsYou) && <span className="inline-flex items-center gap-1.5 text-you"><span className="size-[7px] rounded-full bg-you" />{m.inFlight.filter(m.needsYou).length} waiting on you</span>}>
      <NowTable m={m} />
    </Panel>
  )
  const road = <Panel id="h-road" title="Roadmap" meta="select a square for details" right={<Legend />}><div className="grid grid-cols-[minmax(0,1fr)_340px] gap-5 max-xl:grid-cols-1"><RoadmapGrid m={m} large /><GridSide m={m} /></div></Panel>
  const milestones = (
    <section aria-labelledby="h-ms" className="grid gap-3">
      <h2 id="h-ms" className="sr-only">Milestones</h2>
      <ol className="grid grid-cols-4 gap-4 max-xl:grid-cols-2 max-sm:grid-cols-1">
        {m.milestones.map((ms) => (
          <li key={ms.name} className="grid min-w-0 content-start gap-3 rounded-[10px] border bg-card p-4">
            <div className="flex items-baseline gap-2">
              <h3 className="text-[15px] font-bold">{ms.name}</h3>
              <span className="ml-auto text-sm text-muted-foreground"><b className="font-bold text-foreground">{ms.done}</b>/{ms.total} done</span>
            </div>
            <StatusBar t={ms} />
            <div className="grid gap-1.5 border-t pt-3">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-[11px] font-medium text-muted-foreground"><span>Features · {ms.features.length}</span><FeatureCounts m={m} feats={ms.features} /></div>
              <FeatureList m={m} feats={ms.features} />
            </div>
            <div className="border-t pt-3"><MilestoneGates m={m} ms={ms} /></div>
          </li>
        ))}
      </ol>
    </section>
  )
  return (
    <>
      <div className="grid min-w-0 gap-4 max-lg:hidden">{now}{road}{milestones}</div>
      <Tabs defaultValue="now" className="hidden gap-4 max-lg:flex">
        <TabsList className="w-full">
          <TabsTrigger value="now">Now</TabsTrigger><TabsTrigger value="milestones">Milestones</TabsTrigger><TabsTrigger value="roadmap">Roadmap</TabsTrigger>
        </TabsList>
        <TabsContent value="now" className="grid gap-4">{now}</TabsContent>
        <TabsContent value="milestones" className="grid gap-4">{milestones}</TabsContent>
        <TabsContent value="roadmap" className="grid gap-4">{road}</TabsContent>
      </Tabs>
    </>
  )
}

export default function App() {
  const feed = useRoadmapData()
  const m = useMemo(() => (feed.data ? build(feed.data) : null), [feed.data])
  return (
    <TooltipProvider delayDuration={200}>
      <main className="mx-auto grid max-w-[1440px] grid-cols-[minmax(0,1fr)] gap-4 px-4 pt-6 pb-12 sm:px-6">
        <header className="grid gap-3.5">
          <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
            <h1 className="text-[22px] font-bold tracking-[-0.015em]">hr-relay roadmap</h1>
            <Stamp feed={feed} />
          </div>
          {m && <Counts m={m} />}
        </header>
        {m ? <Cockpit m={m} /> : feed.error
          ? <p role="alert" className="rounded-[10px] border border-blk/50 bg-card p-4 text-sm">Could not load the roadmap: {feed.error}. The page retries every 30 s; check that the render job is writing data.json.</p>
          : <p className="text-sm text-muted-foreground" role="status">Loading the roadmap…</p>}
      </main>
    </TooltipProvider>
  )
}
