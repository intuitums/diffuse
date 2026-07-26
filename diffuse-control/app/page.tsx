"use client";

import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Boxes,
  Check,
  CloudCog,
  Code2,
  Database,
  Gauge,
  GitBranch,
  Github,
  KeyRound,
  Menu,
  Radar,
  Server,
  Settings2,
  ShieldCheck,
  X,
} from "lucide-react";
import { useQuery } from "convex/react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "../convex/_generated/api";

const border = "border-white/[0.07]";
const panel = `${border} overflow-hidden rounded-[10px] bg-[#13121a]/90 shadow-none`;

function relativeTime(value?: number) {
  if (!value) return "Never";
  const minutes = Math.max(1, Math.round((Date.now() - value) / 60_000));
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`;
}

function statusTone(status: string) {
  if (["healthy", "ready", "published", "idle"].includes(status)) {
    return "text-[#72e5a2]";
  }
  if (["failed", "offline", "attention", "error"].includes(status)) {
    return "text-[#f16d67]";
  }
  return "text-[#f6c768]";
}

export default function Home() {
  const data = useQuery(api.controlPlane.dashboard, {});
  const [mobileNav, setMobileNav] = useState(false);

  if (data === undefined) {
    return (
      <main className="grid min-h-screen place-content-center gap-4 bg-[#0c0c11] text-center text-xs tracking-wider text-[#777582]">
        <div className="mx-auto flex h-7 items-end gap-1" aria-hidden="true">
          <i className="h-3 w-2 animate-pulse bg-[#eaff49]" />
          <i className="h-7 w-2 animate-pulse bg-[#eaff49] [animation-delay:150ms]" />
          <i className="h-5 w-2 animate-pulse bg-[#eaff49] [animation-delay:300ms]" />
        </div>
        Opening Diffuse control room…
      </main>
    );
  }

  if (!data.deployment) {
    return (
      <main className="grid min-h-screen place-items-center bg-[radial-gradient(circle_at_50%_35%,rgba(234,255,73,0.08),transparent_30%)] p-6">
        <section className={`w-full max-w-xl ${panel} p-10`}>
          <Brand />
          <p className="mt-14 text-[10px] font-bold uppercase tracking-[0.18em] text-[#eaff49]">
            Control plane ready
          </p>
          <h1 className="mt-3 text-5xl font-bold leading-[0.95] tracking-[-0.06em] text-[#efeee8] sm:text-6xl">
            Code review that earns trust.
          </h1>
          <p className="mt-6 text-sm leading-7 text-[#7c7a87]">
            Start with a safe sample workspace, then point Diffuse at your
            PostgreSQL data plane. Convex receives status projections only—never
            source, diffs, prompts, or finding evidence.
          </p>
          <div className="mt-7 flex items-center gap-3 rounded-lg border border-[#eaff49]/15 bg-[#eaff49]/5 p-4 text-[#eaff49]">
            <Database className="size-4" />
            <span className="text-xs font-semibold">
              Awaiting the first signed data-plane snapshot
            </span>
          </div>
        </section>
      </main>
    );
  }

  const indexed = data.repositories.filter(
    (repository) => repository.indexStatus === "ready",
  ).length;
  const activeReviews = data.reviews.filter((review) =>
    ["queued", "reviewing"].includes(review.status),
  ).length;
  const openFindings = data.repositories.reduce(
    (total, repository) => total + repository.openFindingCount,
    0,
  );

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_80%_-10%,rgba(234,255,73,0.06),transparent_30%)]">
      <aside
        className={`fixed inset-y-0 left-0 z-30 flex w-[252px] flex-col border-r ${border} bg-[#0b0b10]/95 px-[18px] py-7 backdrop-blur-xl transition-transform max-md:-translate-x-full ${mobileNav ? "max-md:translate-x-0" : ""}`}
      >
        <div className="flex items-center justify-between px-2">
          <Brand />
          <button
            className="hidden p-2 text-[#8c8996] max-md:block"
            onClick={() => setMobileNav(false)}
            aria-label="Close navigation"
          >
            <X className="size-4" />
          </button>
        </div>
        <nav className="mt-10 flex flex-col gap-1" aria-label="Product">
          <NavItem icon={Gauge} label="Overview" active />
          <NavItem icon={GitBranch} label="Repositories" count={data.repositories.length} />
          <NavItem icon={Radar} label="Reviews" count={activeReviews} />
          <NavItem icon={Activity} label="Quality" />
          <NavItem icon={Settings2} label="Configuration" />
        </nav>
        <div className={`mt-9 border-t ${border} px-2 pt-6`}>
          <p className="text-[9px] font-extrabold uppercase tracking-[0.16em] text-[#686674]">
            Deployment
          </p>
          <div className="mt-4 grid grid-cols-[31px_1fr_8px] items-center gap-2">
            <span className="grid size-[31px] place-items-center rounded-md border border-[#eaff49]/15 bg-[#eaff49]/5 text-[#eaff49]">
              <Server className="size-3.5" />
            </span>
            <span>
              <strong className="block text-[11px] text-[#d4d2d8]">{data.deployment.name}</strong>
              <small className="mt-0.5 block text-[9px] text-[#6f6d79]">{data.deployment.region}</small>
            </span>
            <i className="size-1.5 rounded-full bg-[#72e5a2] shadow-[0_0_10px_rgba(114,229,162,0.55)]" />
          </div>
        </div>
        <div className={`mt-auto flex gap-2.5 rounded-lg border ${border} bg-white/[0.018] p-3`}>
          <ShieldCheck className="size-4 shrink-0 text-[#72e5a2]" />
          <div>
            <strong className="block text-[10px] text-[#b8b6c0]">Source stays private</strong>
            <p className="mt-1 text-[9px] leading-4 text-[#686673]">
              Only operational metadata crosses the control-plane boundary.
            </p>
          </div>
        </div>
      </aside>

      <main className="min-h-screen pl-[252px] max-md:pl-0">
        <header className={`flex min-h-[90px] items-center border-b ${border} px-[clamp(24px,4vw,58px)]`}>
          <button
            className="mr-3 hidden p-2 text-[#8c8996] max-md:block"
            onClick={() => setMobileNav(true)}
            aria-label="Open navigation"
          >
            <Menu className="size-5" />
          </button>
          <div>
            <p className="text-[9px] font-bold uppercase tracking-[0.08em] text-[#5f5d69]">
              Workspace / Overview
            </p>
            <h1 className="mt-1 text-[17px] font-semibold tracking-tight text-[#efeee8]">
              Good evening, operator.
            </h1>
          </div>
          <div className="ml-auto flex items-center gap-3.5">
            <span className="flex items-center gap-2 text-[9px] font-bold uppercase tracking-wider text-[#777582] max-sm:hidden">
              <i className="size-1.5 rounded-full bg-[#72e5a2] shadow-[0_0_8px_rgba(114,229,162,0.7)]" />
              Live
            </span>
            <Button
              variant="outline"
              className="border-[#eaff49]/30 bg-[#eaff49]/5 text-[#eaff49] hover:bg-[#eaff49]/10 hover:text-[#eaff49]"
            >
              <Github />
              <span className="max-sm:hidden">Connect repository</span>
            </Button>
          </div>
        </header>

        <div className="mx-auto max-w-[1450px] px-[clamp(24px,4vw,58px)] py-11">
          <section className="flex items-end justify-between gap-6 max-sm:block">
            <div>
              <p className="text-[9px] font-extrabold uppercase tracking-[0.16em] text-[#686674]">
                Review intelligence
              </p>
              <h2 className="mt-2 text-[clamp(30px,3vw,42px)] font-bold tracking-[-0.05em] text-[#efeee8]">
                Your codebase, under watch.
              </h2>
              <p className="mt-2.5 max-w-xl text-xs leading-6 text-[#777582]">
                Index health, active reviews, and quality signals across every
                connected repository.
              </p>
            </div>
            <div className={`flex min-w-40 items-center gap-2.5 rounded-lg border ${border} bg-white/[0.02] p-2.5 max-sm:mt-5 max-sm:w-fit`}>
              <CloudCog className="size-4 text-[#eaff49]" />
              <span>
                <small className="block text-[8px] font-bold uppercase tracking-wider text-[#666471]">
                  {data.deployment.kind.replace("_", " ")}
                </small>
                <strong className="mt-0.5 block text-[10px] text-[#b9b7c0]">{data.deployment.version}</strong>
              </span>
            </div>
          </section>

          <section className="mt-9 grid grid-cols-4 gap-3 max-lg:grid-cols-2" aria-label="Workspace metrics">
            <Metric icon={Database} label="Repositories indexed" value={`${indexed}/${data.repositories.length}`} detail={`${data.repositories.length - indexed} need attention`} />
            <Metric icon={Radar} label="Reviews in flight" value={String(activeReviews).padStart(2, "0")} detail="Reactive queue" highlight />
            <Metric icon={AlertTriangle} label="Open findings" value={String(openFindings).padStart(2, "0")} detail={`${data.repositories.reduce((sum, repo) => sum + repo.criticalFindingCount, 0)} critical`} />
            <Metric icon={ShieldCheck} label="Eval precision" value={data.quality ? `${Math.round(data.quality.precision * 100)}%` : "—"} detail={data.quality ? `${data.quality.sampleSize} labeled findings` : "No evaluation yet"} />
          </section>

          <section className="mt-3 grid grid-cols-[minmax(0,1.85fr)_minmax(275px,0.85fr)] gap-3 max-lg:grid-cols-1">
            <Card className={panel}>
              <PanelHeader kicker="Repository fleet" title="Index & review status" action />
              <CardContent className="p-0">
                {data.repositories.map((repository) => (
                  <article className={`grid min-h-[67px] grid-cols-[35px_minmax(140px,1fr)_85px_55px_77px] items-center gap-3 border-b ${border} px-5 py-2.5 last:border-0 max-sm:grid-cols-[31px_minmax(110px,1fr)]`} key={repository._id}>
                    <span className={`grid size-[31px] place-items-center rounded-md border ${border} bg-white/[0.02] text-[#8b8995]`}>
                      {repository.provider === "github" ? <Github className="size-3.5" /> : <Code2 className="size-3.5" />}
                    </span>
                    <span>
                      <strong className="block text-[11px] text-[#c7c5cd]">{repository.name}</strong>
                      <small className="mt-1 block text-[9px] capitalize text-[#5f5d68]">{repository.provider} · {repository.defaultBranch}</small>
                    </span>
                    <StatusPill status={repository.indexStatus} className="max-sm:hidden" />
                    <TinyStat value={repository.openFindingCount} label="findings" className="max-sm:hidden" />
                    <TinyStat value={relativeTime(repository.lastIndexedAt)} label="last index" className="max-sm:hidden" />
                  </article>
                ))}
              </CardContent>
            </Card>

            <Card className={panel}>
              <PanelHeader kicker="Trust signal" title="Review quality" />
              <CardContent className="p-5">
                {data.quality ? (
                  <>
                    <div className="flex items-center gap-5">
                      <div
                        className="grid size-[78px] shrink-0 place-content-center rounded-full text-center"
                        style={{
                          background: `radial-gradient(circle,#13121a 57%,transparent 59%),conic-gradient(#eaff49 ${data.quality.f1 * 360}deg,#282730 0)`,
                        }}
                      >
                        <strong className="text-xl text-[#efeee8]">{Math.round(data.quality.f1 * 100)}</strong>
                        <small className="text-[8px] font-extrabold tracking-widest text-[#686674]">F1</small>
                      </div>
                      <div>
                        <strong className="text-[11px] text-[#c9c7cf]">High-signal baseline</strong>
                        <p className="mt-1 text-[9px] leading-4 text-[#696774]">
                          {data.quality.truePositives} true bugs found with {data.quality.falsePositives} false positives.
                        </p>
                      </div>
                    </div>
                    <div className="mt-6 grid grid-cols-2 gap-4">
                      <QualityStat label="Precision" value={data.quality.precision} />
                      <QualityStat label="Recall" value={data.quality.recall} />
                    </div>
                    <div className={`mt-5 flex items-center gap-2 border-t ${border} pt-4 text-[9px] text-[#666471]`}>
                      <Check className="size-3 text-[#72e5a2]" />
                      <span><strong className="text-[#b5b3bc]">{data.quality.addressedFindings}</strong> findings addressed</span>
                    </div>
                  </>
                ) : null}
              </CardContent>
            </Card>

            <Card className={panel}>
              <PanelHeader kicker="Live queue" title="Recent reviews" live />
              <CardContent className="p-0">
                {data.reviews.map((review) => (
                  <article className={`grid min-h-[69px] grid-cols-[32px_minmax(200px,1fr)_82px_58px_53px] items-center gap-3 border-b ${border} px-5 py-2.5 last:border-0 max-sm:grid-cols-[29px_minmax(130px,1fr)]`} key={review._id}>
                    <span className={`grid size-7 place-items-center rounded-full bg-white/[0.035] ${statusTone(review.status)}`}>
                      {review.status === "published" ? <Check className="size-3" /> : review.status === "failed" ? <AlertTriangle className="size-3" /> : <Radar className="size-3" />}
                    </span>
                    <span className="min-w-0">
                      <span className="flex items-center gap-2">
                        <strong className="truncate text-[10px] text-[#c6c4cc]">{review.title}</strong>
                        <small className="text-[8px] text-[#55535e]">#{review.number}</small>
                      </span>
                      <small className="mt-1 block truncate text-[8px] text-[#5f5d69]">{review.repositoryName} · {review.model}</small>
                    </span>
                    <StatusPill status={review.status} className="max-sm:hidden" />
                    <TinyStat value={review.findingCount} label="findings" className="max-sm:hidden" />
                    <time className="text-right text-[8px] text-[#56545f] max-sm:hidden">{relativeTime(review.updatedAt)}</time>
                  </article>
                ))}
              </CardContent>
            </Card>

            <Card className={panel}>
              <PanelHeader kicker="Inference" title="Review model" />
              <CardContent className="p-5">
                {data.model ? (
                  <>
                    <div className="flex items-center gap-3">
                      <span className="grid size-9 place-items-center rounded-lg border border-[#eaff49]/15 bg-[#eaff49]/5 text-[#eaff49]">
                        <Boxes className="size-4" />
                      </span>
                      <span>
                        <small className="block text-[8px] uppercase text-[#64626e]">{data.model.provider}</small>
                        <strong className="mt-1 block text-[10px] text-[#c1bfc8]">{data.model.model}</strong>
                      </span>
                    </div>
                    <div className={`mt-4 flex gap-2.5 rounded-md border border-current/20 bg-current/[0.04] p-3 ${statusTone(data.model.status)}`}>
                      <KeyRound className="size-3.5 shrink-0" />
                      <div>
                        <strong className="block text-[9px]">
                          {data.model.status === "ready" ? "Model connected" : "Provider key required"}
                        </strong>
                        <p className="mt-1 text-[8px] leading-4 text-[#686672]">
                          The credential remains in the data plane and is never sent to Convex.
                        </p>
                      </div>
                    </div>
                    <div className="mt-4 flex flex-wrap gap-1.5">
                      {data.model.reviewPasses.map((pass) => (
                        <span className={`rounded border ${border} bg-white/[0.02] px-2 py-1 text-[7px] font-bold uppercase text-[#74727e]`} key={pass}>{pass}</span>
                      ))}
                    </div>
                  </>
                ) : null}
              </CardContent>
            </Card>
          </section>
        </div>
      </main>
      {mobileNav ? (
        <button className="fixed inset-0 z-20 bg-black/60 md:hidden" onClick={() => setMobileNav(false)} aria-label="Close navigation" />
      ) : null}
    </div>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-3 text-[15px] font-extrabold tracking-[0.18em] text-[#efeee8]">
      <span className="flex h-6 w-7 skew-x-[-9deg] items-end gap-0.5" aria-hidden="true">
        <i className="h-3 w-[7px] bg-[#eaff49]" />
        <i className="h-6 w-[7px] bg-[#eaff49]" />
        <i className="h-4 w-[7px] bg-[#eaff49]" />
      </span>
      DIFFUSE
    </div>
  );
}

function NavItem({ icon: Icon, label, active, count }: { icon: typeof Gauge; label: string; active?: boolean; count?: number }) {
  return (
    <button className={`flex h-[42px] items-center gap-3 rounded-md px-3 text-[13px] font-semibold transition ${active ? "bg-[#eaff49]/[0.08] text-[#eaff49]" : "text-[#777584] hover:bg-white/[0.035] hover:text-[#efeee8]"}`}>
      <Icon className="size-[17px]" />
      {label}
      {count !== undefined ? <small className="ml-auto min-w-6 rounded-full bg-[#1c1b24] px-2 py-0.5 text-[10px] text-[#8f8d9a]">{count}</small> : null}
    </button>
  );
}

function Metric({ icon: Icon, label, value, detail, highlight }: { icon: typeof Database; label: string; value: string; detail: string; highlight?: boolean }) {
  return (
    <Card className={`${panel} ${highlight ? "border-[#eaff49]/20 bg-[linear-gradient(145deg,rgba(234,255,73,0.07),rgba(19,18,26,0.9)_55%)]" : ""}`}>
      <CardContent className="p-[18px]">
        <div className="flex items-center gap-2 text-[9px] font-bold uppercase tracking-wide text-[#6e6c79]"><Icon className="size-3.5" />{label}</div>
        <strong className={`mt-4 block text-[28px] font-semibold tracking-[-0.05em] ${highlight ? "text-[#eaff49]" : "text-[#efeee8]"}`}>{value}</strong>
        <p className="mt-1 text-[9px] text-[#666471]">{detail}</p>
      </CardContent>
    </Card>
  );
}

function PanelHeader({ kicker, title, action, live }: { kicker: string; title: string; action?: boolean; live?: boolean }) {
  return (
    <CardHeader className={`flex min-h-[71px] flex-row items-center justify-between space-y-0 border-b ${border} px-5 py-4`}>
      <div>
        <p className="text-[9px] font-extrabold uppercase tracking-[0.16em] text-[#686674]">{kicker}</p>
        <CardTitle className="mt-1.5 text-sm text-[#d7d5dc]">{title}</CardTitle>
      </div>
      {action ? <Button variant="ghost" size="sm" className="text-[9px] text-[#6e6c78] hover:text-[#eaff49]">View all <ArrowUpRight /></Button> : null}
      {live ? <span className="flex items-center gap-2 text-[8px] font-extrabold uppercase tracking-wider text-[#65636f]"><i className="size-1.5 rounded-full bg-[#72e5a2]" />streaming</span> : null}
    </CardHeader>
  );
}

function StatusPill({ status, className = "" }: { status: string; className?: string }) {
  return <span className={`inline-flex w-fit items-center gap-1.5 whitespace-nowrap rounded-full border border-current/60 px-2 py-1 text-[8px] font-bold capitalize ${statusTone(status)} ${className}`}><i className="size-1 rounded-full bg-current" />{status.replace("_", " ")}</span>;
}

function TinyStat({ value, label, className = "" }: { value: string | number; label: string; className?: string }) {
  return <span className={`text-right ${className}`}><strong className="block text-[10px] text-[#aaa8b2]">{value}</strong><small className="mt-0.5 block text-[8px] text-[#56545f]">{label}</small></span>;
}

function QualityStat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <span className="flex justify-between text-[9px] text-[#777582]"><small>{label}</small><strong>{Math.round(value * 100)}%</strong></span>
      <i className="mt-2 block h-[3px] overflow-hidden rounded-full bg-[#292832]"><b className="block h-full rounded-full bg-[#eaff49]" style={{ width: `${value * 100}%` }} /></i>
    </div>
  );
}
